"""El corazón de VIGÍA: un bucle asyncio por servicio, chequeos concurrentes y
difusión de cada resultado a los clientes WebSocket.

Hay dos tipos de servicio:
- destacados: los de targets.json, públicos en el landing y en el WebSocket.
- de visitantes: los crea cualquiera desde la web, duran 24 h y solo los ve
  quien tenga su enlace. Se chequean con el probe protegido contra SSRF.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from collections import deque
from dataclasses import asdict
from typing import Awaitable, Callable

import httpx

from .alerts import Notifier
from .config import Settings, Target
from .evaluator import DOWN, Check, incidents, mttr, summarize
from .inspect import Resolver, safe_check, system_resolve
from .store import Store

log = logging.getLogger("vigia.monitor")
Probe = Callable[[Target], Awaitable[Check]]


def now_ms() -> float:
    return time.time() * 1000


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LimitReached(Exception):
    """No se pueden crear más monitores (por IP o en total). El mensaje va al visitante."""


class Monitor:
    def __init__(self, settings: Settings, targets: list[Target], store: Store,
                 probe: Probe | None = None, notifier: Notifier | None = None,
                 guest_probe: Probe | None = None, resolver: Resolver = system_resolve):
        self.s = settings
        self.store = store
        self.notifier = notifier
        self.resolver = resolver
        self._probe = probe or self._http_probe
        self._guest_probe = guest_probe or self._safe_probe
        self._client: httpx.AsyncClient | None = None
        self._guest_client: httpx.AsyncClient | None = None
        self.featured = [t.id for t in targets]
        self.targets: dict[str, Target] = {}
        self.guests: dict[str, dict] = {}               # id -> {expires, owner, token_hash}
        self.history: dict[str, deque] = {}
        self.status: dict[str, str] = {}
        self.simulated: dict[str, float] = {}           # id -> instante (monotonic) en que expira
        self._wake: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._subs: set[asyncio.Queue] = set()
        self._background: set[asyncio.Task] = set()
        for t in targets:
            self._register(t)

    def _register(self, t: Target) -> None:
        self.targets[t.id] = t
        self.history[t.id] = deque(self.store.recent(t.id, self.s.window), maxlen=self.s.window)
        self.status[t.id] = summarize(list(self.history[t.id]), self.slow_ms(t.id))["status"]
        self._wake[t.id] = asyncio.Event()

    def is_guest(self, tid: str) -> bool:
        return tid in self.guests

    # ---------- ciclo de vida ----------
    async def start(self) -> None:
        headers = {"user-agent": "VIGIA-monitor/1.0 (+https://github.com/saracastrillons/vigia)"}
        if self._probe == self._http_probe:
            self._client = httpx.AsyncClient(timeout=self.s.timeout, follow_redirects=True, headers=headers)
        if self._guest_probe == self._safe_probe:
            # sin redirecciones automáticas: safe_check valida cada salto a mano
            self._guest_client = httpx.AsyncClient(timeout=self.s.timeout, follow_redirects=False, headers=headers)
        for i, tid in enumerate(self.featured):
            # escalonar el arranque para no disparar todos los chequeos a la vez
            self._spawn(tid, delay=i * 0.4)
        for g in self.store.load_guests(now_ms()):   # monitores de visitantes que siguen vigentes
            self._register(Target(id=g["id"], name=g["name"], url=g["url"], desc="Monitor de visitante"))
            self.guests[g["id"]] = {"expires": g["expires"], "owner": g["owner"], "token_hash": g["token_hash"]}
            self._spawn(g["id"], delay=1.0)

    def _spawn(self, tid: str, delay: float) -> None:
        self._tasks[tid] = asyncio.create_task(self._run(tid, delay), name=f"check:{tid}")

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, *self._background, return_exceptions=True)
        for client in (self._client, self._guest_client):
            if client:
                await client.aclose()

    async def _run(self, tid: str, delay: float) -> None:
        await asyncio.sleep(delay)
        interval = self.s.guest_interval if self.is_guest(tid) else self.s.interval
        while True:
            if self.is_guest(tid) and now_ms() > self.guests[tid]["expires"]:
                self._forget(tid)
                return
            check = await self.check_now(tid)
            if check.ok:
                wait = interval
            elif self.status[tid] != DOWN:
                wait = self.s.recheck                    # confirmar rápido antes de declarar caída
            else:
                wait = max(self.s.recheck, interval / 3)  # caído: vigilar la recuperación
            try:
                await asyncio.wait_for(self._wake[tid].wait(), wait)
            except asyncio.TimeoutError:
                pass
            self._wake[tid].clear()

    # ---------- chequeos ----------
    async def _http_probe(self, target: Target) -> Check:
        t, t0 = now_ms(), time.perf_counter()
        try:
            r = await self._client.get(target.url)
        except httpx.HTTPError as e:
            return Check(t=t, ok=False, ms=None, error=type(e).__name__)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        ok = r.status_code == target.expect_status if target.expect_status else r.status_code < 400
        return Check(t=t, ok=ok, ms=ms, code=r.status_code, error=None if ok else f"HTTP {r.status_code}")

    async def _safe_probe(self, target: Target) -> Check:
        return await safe_check(target.url, self._guest_client, self.resolver)

    async def check_now(self, tid: str) -> Check:
        target = self.targets[tid]
        if self.is_simulated(tid):
            check = Check(t=now_ms(), ok=False, ms=None, error="incidente simulado")
        else:
            probe = self._guest_probe if self.is_guest(tid) else self._probe
            try:
                check = await probe(target)
            except Exception as e:  # un probe roto cuenta como fallo, nunca mata el bucle
                log.exception("Probe de %s falló", tid)
                check = Check(t=now_ms(), ok=False, ms=None, error=type(e).__name__)
        if tid not in self.targets:   # se borró mientras se chequeaba
            return check
        self.history[tid].append(check)
        self.store.add(tid, check)

        prev = self.status[tid]
        summary = self.summary(tid)
        self.status[tid] = summary["status"]
        if self.is_guest(tid):
            return check   # los monitores de visitantes son privados: ni alertas ni WebSocket
        if prev != summary["status"] and DOWN in (prev, summary["status"]) and self.notifier:
            task = asyncio.create_task(self.notifier(target, prev, summary))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        self._broadcast({"type": "check", "service": summary, "check": asdict(check)})
        return check

    # ---------- monitores de visitantes ----------
    def add_guest(self, url: str, name: str, owner: str) -> dict:
        active = [g for g in self.guests.values() if g["expires"] > now_ms()]
        if len(active) >= self.s.guest_max_total:
            raise LimitReached("VIGÍA está vigilando el máximo de sitios en este momento. Intenta más tarde.")
        if sum(1 for g in active if g["owner"] == owner) >= self.s.guest_max_per_ip:
            raise LimitReached(f"Puedes tener como máximo {self.s.guest_max_per_ip} monitores a la vez.")
        tid = "g" + secrets.token_hex(5)
        token = secrets.token_urlsafe(18)
        expires = now_ms() + self.s.guest_ttl_h * 3600 * 1000
        self.store.save_guest(tid, name, url, expires, owner, _hash(token))
        self._register(Target(id=tid, name=name, url=url, desc="Monitor de visitante"))
        self.guests[tid] = {"expires": expires, "owner": owner, "token_hash": _hash(token)}
        self._spawn(tid, delay=0)
        return {"id": tid, "token": token, "expires": expires}

    def delete_guest(self, tid: str, token: str) -> bool:
        g = self.guests.get(tid)
        if not g or not secrets.compare_digest(g["token_hash"], _hash(token)):
            return False
        task = self._tasks.get(tid)
        if task:
            task.cancel()
        self._forget(tid)
        return True

    def _forget(self, tid: str) -> None:
        for d in (self.targets, self.guests, self.history, self.status, self._wake, self._tasks, self.simulated):
            d.pop(tid, None)
        self.store.delete_service(tid)

    # ---------- incidentes simulados ----------
    def is_simulated(self, tid: str) -> bool:
        expires = self.simulated.get(tid)
        if expires is not None and time.monotonic() > expires:
            del self.simulated[tid]
            return False
        return expires is not None

    def simulate(self, tid: str, down: bool) -> None:
        if down:
            self.simulated[tid] = time.monotonic() + self.s.simulate_ttl
        else:
            self.simulated.pop(tid, None)
        self._wake[tid].set()   # chequear ya, sin esperar el intervalo

    # ---------- vistas ----------
    def slow_ms(self, tid: str) -> float:
        return self.targets[tid].slow_ms or self.s.slow_ms

    def summary(self, tid: str) -> dict:
        t = self.targets[tid]
        h = list(self.history[tid])
        return {
            "id": t.id, "name": t.name, "url": t.url, "desc": t.desc, "slow_ms": self.slow_ms(tid),
            **summarize(h, self.slow_ms(tid)),
            "simulated": self.is_simulated(tid),
            "history": [{"t": c.t, "ok": c.ok, "ms": c.ms} for c in h],
        }

    def status_page(self, tid: str) -> dict:
        """Resumen + incidentes calculados sobre el historial largo guardado en SQLite."""
        long = self.store.recent(tid, 2880)
        incs = incidents(long)
        return {
            **self.summary(tid),
            "kind": "guest" if self.is_guest(tid) else "featured",
            "expires": self.guests[tid]["expires"] if self.is_guest(tid) else None,
            "interval": self.s.guest_interval if self.is_guest(tid) else self.s.interval,
            "incidents": incs[:20],
            "incident_count": len(incs),
            "mttr_s": mttr(incs),
            "since": long[0].t if long else None,
            "checks": len(long),
        }

    def snapshot(self) -> dict:
        return {
            "type": "hello",
            "interval": self.s.interval, "recheck": self.s.recheck,
            "timeout": self.s.timeout, "slow_ms": self.s.slow_ms, "window": self.s.window,
            "services": [self.summary(tid) for tid in self.featured],
        }

    # ---------- pub/sub para WebSocket ----------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _broadcast(self, msg: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:   # cliente lento: se descarta el mensaje, no se bloquea a nadie
                pass
