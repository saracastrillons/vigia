"""API de VIGÍA: REST + WebSocket, y sirve el landing (web/) en la raíz."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .alerts import telegram_notifier
from .config import ROOT, Settings, load_targets
from .monitor import Monitor, Probe
from .store import Store

WEB_DIR = ROOT / "web"


class SimulateIn(BaseModel):
    down: bool


def create_app(settings: Settings | None = None, probe: Probe | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    targets = load_targets(settings.targets_file, settings.self_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(settings.db_path)
        monitor = Monitor(settings, targets, store, probe=probe, notifier=telegram_notifier(settings))
        app.state.monitor, app.state.store = monitor, store
        await monitor.start()
        try:
            yield
        finally:
            await monitor.stop()
            store.close()

    app = FastAPI(title="VIGÍA", version="1.0.0", lifespan=lifespan,
                  description="Monitoreo autoalojado de servicios con regla 2 de 3 y eventos en tiempo real.")

    def monitor() -> Monitor:
        return app.state.monitor

    def known(sid: str) -> str:
        if sid not in monitor().targets:
            raise HTTPException(404, f"Servicio desconocido: {sid}")
        return sid

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
