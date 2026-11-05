"""API de VIGÍA: REST + WebSocket, y sirve el landing (web/) en la raíz."""
from __future__ import annotations

import asyncio
import math
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import badge
from .alerts import telegram_notifier
from .config import ROOT, Settings, load_targets
from .inspect import Blocked, RateLimiter, Resolver, inspect, normalize, resolve_public, system_resolve
from .monitor import LimitReached, Monitor, Probe
from .store import Store

WEB_DIR = ROOT / "web"


class SimulateIn(BaseModel):
    down: bool


class InspectIn(BaseModel):
    url: str = Field(max_length=2048)


class MonitorIn(BaseModel):
    url: str = Field(max_length=2048)
    name: str | None = Field(default=None, max_length=60)


def create_app(settings: Settings | None = None, probe: Probe | None = None,
               resolver: Resolver = system_resolve, inspect_transport: httpx.AsyncBaseTransport | None = None,
               measure_connect: bool = True, guest_probe: Probe | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    targets = load_targets(settings.targets_file, settings.self_url)
    limiter = RateLimiter(settings.inspect_limit, settings.inspect_per)
    create_limiter = RateLimiter(10, 3600)  # creaciones de monitores por IP y hora
    inspect_slots = asyncio.Semaphore(4)   # como mucho 4 revisiones simultáneas en todo el servidor

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(settings.db_path)
        monitor = Monitor(settings, targets, store, probe=probe, notifier=telegram_notifier(settings),
                          guest_probe=guest_probe, resolver=resolver)
        inspector = httpx.AsyncClient(
            timeout=6.0, follow_redirects=False, transport=inspect_transport,
            headers={"user-agent": "VIGIA-inspect/1.0 (+https://github.com/saracastrillons/vigia)"},
        )
        app.state.monitor, app.state.store, app.state.inspector = monitor, store, inspector
        await monitor.start()
        try:
            yield
        finally:
            await monitor.stop()
            await inspector.aclose()
            store.close()

    app = FastAPI(title="VIGÍA", version="1.2.0", lifespan=lifespan,
                  description="Monitoreo autoalojado de servicios con regla 2 de 3 y eventos en tiempo real.")

    def monitor() -> Monitor:
        return app.state.monitor

    def known(sid: str) -> str:
        """Cualquier servicio: destacado o creado por un visitante."""
        if sid not in monitor().targets:
            raise HTTPException(404, f"Servicio desconocido o expirado: {sid}")
        return sid

    def featured(sid: str) -> str:
        if sid not in monitor().featured:
            raise HTTPException(404, f"Servicio desconocido: {sid}")
        return sid

    def page(name: str, request: Request) -> HTMLResponse:
        origin = str(request.base_url).rstrip("/")
        html = (WEB_DIR / name).read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__ORIGIN__", origin))

    def client_ip(request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for")
        if settings.trust_proxy and forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "?"

    # La página lleva la URL absoluta del sitio en sus metadatos para compartir (og:image necesita una).
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request) -> HTMLResponse:
        return page("index.html", request)

    @app.get("/s/{sid}", response_class=HTMLResponse, include_in_schema=False)
    def status_html(sid: str, request: Request) -> HTMLResponse:
        return page("status.html", request)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/services")
    def services() -> dict:
        return monitor().snapshot()

    @app.get("/api/services/{sid}")
    def service(sid: str) -> dict:
        return monitor().summary(featured(sid))

    @app.get("/api/services/{sid}/checks")
    def checks(sid: str, limit: int = Query(500, ge=1, le=5000)) -> list[dict]:
        rows = app.state.store.recent(featured(sid), limit)
        return [{"t": c.t, "ok": c.ok, "ms": c.ms, "code": c.code, "error": c.error} for c in rows]

    @app.post("/api/services/{sid}/simulate")
    def simulate(sid: str, body: SimulateIn) -> dict:
        if not settings.allow_simulate:
            raise HTTPException(403, "La simulación de incidentes está desactivada")
        monitor().simulate(featured(sid), body.down)
        return {"id": sid, "simulated": body.down, "expires_in": settings.simulate_ttl if body.down else None}

    @app.post("/api/inspect")
    async def inspect_url(body: InspectIn, request: Request) -> dict:
        wait = limiter.check(client_ip(request))
        if wait:
            secs = math.ceil(wait)
            raise HTTPException(429, f"Demasiadas revisiones seguidas. Intenta de nuevo en {secs} s",
                                headers={"Retry-After": str(secs)})
        try:
            async with inspect_slots:
                return await inspect(body.url, app.state.inspector, resolver, measure_connect=measure_connect)
        except Blocked as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/api/status/{sid}")
    def status(sid: str) -> dict:
        """Página de estado: resumen, incidentes y MTTR de cualquier servicio."""
        return monitor().status_page(known(sid))

    @app.post("/api/monitors", status_code=201)
    async def create_monitor(body: MonitorIn, request: Request) -> dict:
        """Cualquier visitante puede vigilar un sitio público durante 24 h."""
        owner = client_ip(request)
        wait = create_limiter.check(owner)
        if wait:
            raise HTTPException(429, f"Demasiados monitores creados. Intenta de nuevo en {math.ceil(wait / 60)} min")
        try:
            url = normalize(body.url)
            host = url.split("://", 1)[1].split("/", 1)[0].rsplit(":", 1)[0].strip("[]")
            await resolve_public(host, 443, resolver)   # falla rápido si apunta a la red interna
            name = (body.name or "").strip() or host
            created = monitor().add_guest(url, name[:60], owner)
        except Blocked as e:
            raise HTTPException(400, str(e)) from None
        except LimitReached as e:
            raise HTTPException(429, str(e)) from None
        return {**created, "name": name[:60], "url": url, "status_url": f"/s/{created['id']}",
                "badge_url": f"/badge/{created['id']}.svg"}

    @app.delete("/api/monitors/{sid}", status_code=204)
    def delete_monitor(sid: str, x_monitor_token: str = Header(default="")) -> Response:
        if not monitor().is_guest(sid) or not monitor().delete_guest(sid, x_monitor_token):
            raise HTTPException(404, "Monitor no encontrado o token inválido")
        return Response(status_code=204)

    @app.get("/badge/{sid}.svg", include_in_schema=False)
    def service_badge(sid: str, metric: Literal["uptime", "latency", "status"] = "uptime") -> Response:
        s = monitor().summary(known(sid))
        svg = badge.render(s["name"], badge.badge_value(s, metric), s["status"])
        return Response(svg, media_type="image/svg+xml",
                        headers={"cache-control": "max-age=60, s-maxage=60"})

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        q = monitor().subscribe()
        try:
            await socket.send_json(monitor().snapshot())
            while True:
                await socket.send_json(await q.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            monitor().unsubscribe(q)

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


app = create_app()
