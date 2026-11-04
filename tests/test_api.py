import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.evaluator import Check
from app.main import create_app
from app.monitor import now_ms


@pytest.fixture
def client(tmp_path):
    targets = tmp_path / "targets.json"
    targets.write_text(json.dumps([
        {"id": "alpha", "name": "ALPHA", "url": "https://alpha.test", "desc": "servicio de prueba"},
        {"id": "beta", "name": "BETA", "url": "{self}/healthz"},
    ]), encoding="utf-8")
    settings = Settings(interval=0.05, recheck=0.02, db_path=str(tmp_path / "t.db"),
                        targets_file=str(targets), self_url="http://vigia.test")

    async def fake_probe(target):
        return Check(t=now_ms(), ok=True, ms=42.0, code=200)

    with TestClient(create_app(settings, probe=fake_probe)) as c:
        yield c


def wait_for(fn, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.03)
    raise AssertionError("la condición no se cumplió a tiempo")


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_landing_se_sirve_en_la_raiz(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "VIGÍA" in r.text


def test_servicios_se_chequean(client):
    data = wait_for(lambda: (d := client.get("/api/services").json()) and all(s["last"] for s in d["services"]) and d)
    assert [s["id"] for s in data["services"]] == ["alpha", "beta"]
    assert data["services"][1]["url"] == "http://vigia.test/healthz"
    alpha = data["services"][0]
    assert alpha["status"] == "up"
    assert alpha["p50"] == 42.0
    assert alpha["uptime"] == 100.0


def test_historial_queda_en_sqlite(client):
    rows = wait_for(lambda: client.get("/api/services/alpha/checks?limit=5").json())
    assert rows[0]["ok"] is True and rows[0]["code"] == 200


def test_servicio_desconocido_da_404(client):
    assert client.get("/api/services/nope").status_code == 404
    assert client.post("/api/services/nope/simulate", json={"down": True}).status_code == 404


def test_incidente_simulado_cae_y_se_recupera(client):
    wait_for(lambda: client.get("/api/services/alpha").json()["status"] == "up")
    assert client.post("/api/services/alpha/simulate", json={"down": True}).json()["simulated"] is True
    down = wait_for(lambda: (s := client.get("/api/services/alpha").json())["status"] == "down" and s)
    assert down["simulated"] is True
    assert down["failures"] >= 2
    assert down["last"]["error"] == "incidente simulado"

    client.post("/api/services/alpha/simulate", json={"down": False})
    wait_for(lambda: client.get("/api/services/alpha").json()["status"] == "up")


def test_websocket_envia_estado_y_eventos(client):
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert {s["id"] for s in hello["services"]} == {"alpha", "beta"}
        event = ws.receive_json()
        assert event["type"] == "check"
        assert event["check"]["ok"] is True
        assert event["service"]["id"] in {"alpha", "beta"}


def test_reinicio_con_historial_existente(tmp_path):
    """El monitor arranca bien cuando SQLite ya tiene chequeos de una ejecución anterior."""
    targets = tmp_path / "targets.json"
    targets.write_text(json.dumps([{"id": "alpha", "url": "https://alpha.test"}]), encoding="utf-8")
    settings = Settings(interval=0.05, recheck=0.02, db_path=str(tmp_path / "t.db"),
                        targets_file=str(targets), self_url="http://vigia.test")

    async def fake_probe(target):
        return Check(t=now_ms(), ok=True, ms=10.0, code=200)

    for _ in range(2):
        with TestClient(create_app(settings, probe=fake_probe)) as c:
            # la segunda vuelta carga el historial de la primera desde SQLite
            wait_for(lambda: len(c.get("/api/services/alpha").json()["history"]) >= 3)
