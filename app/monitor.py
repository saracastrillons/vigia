"""El corazón de VIGÍA: un bucle asyncio por servicio, chequeos concurrentes y
difusión de cada resultado a los clientes WebSocket."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import asdict
from typing import Awaitable, Callable

import httpx

from .alerts import Notifier
from .config import Settings, Target
from .evaluator import DOWN, Check, summarize
from .store import Store

log = logging.getLogger("vigia.monitor")
Probe = Callable[[Target], Awaitable[Check]]


def now_ms() -> float:
    return time.time() * 1000


class Monitor:
    def __init__(self, settings: Settings, targets: list[Target], store: Store,
                 probe: Probe | None = None, notifier: Notifier | None = None):
        self.s = settings
        self.targets = {t.id: t for t in targets}
        self.store = store
        self.notifier = notifier
        self._probe = probe or self._http_probe
        self._client: httpx.AsyncClient | None = None
        self.history = {t.id: deque(store.recent(t.id, settings.window), maxlen=settings.window) for t in targets}
        self.status = {tid: summarize(list(h), self.slow_ms(tid))["status"] for tid, h in self.history.items()}
        self.simulated: dict[str, float] = {}           # id -> instante (monotonic) en que expira
        self._wake = {tid: asyncio.Event() for tid in self.targets}
        self._subs: set[asyncio.Queue] = set()
        self._tasks: list[asyncio.Task] = []
        self._background: set[asyncio.Task] = set()

    # ---------- ciclo de vida ----------
    async def start(self) -> None:
        if self._probe == self._http_probe:
            self._client = httpx.AsyncClient(
                timeout=self.s.timeout, follow_redirects=True,
                headers={"user-agent": "VIGIA-monitor/1.0 (+https://github.com/saracastrillons)"},
            )
        for i, tid in enumerate(self.targets):
            # escalonar el arranque para no disparar todos los chequeos a la vez
            self._tasks.append(asyncio.create_task(self._run(tid, delay=i * 0.4), name=f"check:{tid}"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, *self._background, return_exceptions=True)
        if self._client:
            await self._client.aclose()

    async def _run(self, tid: str, delay: float) -> None:
        await asyncio.sleep(delay)
        while True:
            check = await self.check_now(tid)
            if check.ok:
                wait = self.s.interval
            elif self.status[tid] != DOWN:
                wait = self.s.recheck                    # confirmar rápido antes de declarar caída
            else:
                wait = max(self.s.recheck, self.s.interval / 3)   # caído: vigilar la recuperación
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

    async def check_now(self, tid: str) -> Check:
        target = self.targets[tid]
        if self.is_simulated(tid):
            check = Check(t=now_ms(), ok=False, ms=None, error="incidente simulado")
        else:
            try:
                check = await self._probe(target)
            except Exception as e:  # un probe roto cuenta como fallo, nunca mata el bucle
                log.exception("Probe de %s falló", tid)
                check = Check(t=now_ms(), ok=False, ms=None, error=type(e).__name__)
        self.history[tid].append(check)
        self.store.add(tid, check)

        prev = self.status[tid]
        summary = self.summary(tid)
        self.status[tid] = summary["status"]
        if prev != summary["status"] and DOWN in (prev, summary["status"]) and self.notifier:
            task = asyncio.create_task(self.notifier(target, prev, summary))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        self._broadcast({"type": "check", "service": summary, "check": asdict(check)})
        return check

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

    def snapshot(self) -> dict:
        return {
            "type": "hello",
            "interval": self.s.interval, "recheck": self.s.recheck,
            "timeout": self.s.timeout, "slow_ms": self.s.slow_ms, "window": self.s.window,
            "services": [self.summary(tid) for tid in self.targets],
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
