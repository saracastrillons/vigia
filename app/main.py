"""API de VIGÍA: REST + WebSocket, y sirve el landing (web/) en la raíz."""
from __future__ import annotations

import asyncio
import math
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import badge
from .alerts import telegram_notifier
from .config import ROOT, Settings, load_targets
from .inspect import Blocked, RateLimiter, Resolver, inspect, system_resolve
from .monitor import Monitor, Probe
from .store import Store

WEB_DIR = ROOT / "web"


class SimulateIn(BaseModel):
    down: bool


class InspectIn(BaseModel):
    url: str = Field(max_length=2048)


def create_app(settings: Settings | None = None, probe: Probe | None = None,
               resolver: Resolver = system_resolve, inspect_transport: httpx.AsyncBaseTransport | None = None,
               measure_connect: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    targets = load_targets(settings.targets_file, settings.self_url)
    limiter = RateLimiter(settings.inspect_limit, settings.inspect_per)
    inspect_slots = asyncio.Semaphore(4)   # como mucho 4 revisiones simultáneas en todo el servidor

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(settings.db_path)
        monitor = Monitor(settings, targets, store, probe=probe, notifier=telegram_notifier(settings))
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

    app = FastAPI(title="VIGÍA", version="1.1.0", lifespan=lifespan,
                  description="Monitoreo autoalojado de servicios con regla 2 de 3 y eventos en tiempo real.")

    def monitor() -> Monitor:
        return app.state.monitor

    def known(sid: str) -> str:
        if sid not in monitor().targets:
            raise HTTPException(404, f"Servicio desconocido: {sid}")
        return sid

    def client_ip(request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for")
        if settings.trust_proxy and forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "?"

    # La página lleva la URL absoluta del sitio en sus metadatos para compartir (og:image necesita una).
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request) -> HTMLResponse:
        origin = str(request.base_url).rstrip("/")
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__ORIGIN__", origin))

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/services")
    def services() -> dict:
        return monitor().snapshot()

    @app.get("/api/services/{sid}")
    def service(sid: str) -> dict:
        return monitor().summary(known(sid))

    @app.get("/api/services/{sid}/checks")
    def checks(sid: str, limit: int = Query(500, ge=1, le=5000)) -> list[dict]:
        rows = app.state.store.recent(known(sid), limit)
        return [{"t": c.t, "ok": c.ok, "ms": c.ms, "code": c.code, "error": c.error} for c in rows]

    @app.post("/api/services/{sid}/simulate")
    def simulate(sid: str, body: SimulateIn) -> dict:
        if not settings.allow_simulate:
            raise HTTPException(403, "La simulación de incidentes está desactivada")
        monitor().simulate(known(sid), body.down)
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
