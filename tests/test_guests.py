import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.evaluator import Check, incidents, mttr
from app.main import create_app
from app.monitor import now_ms


async def fake_resolve(host, port):
    if host == "intranet.test":
        return ["10.0.0.9"]
    if host.replace(".", "").isdigit():
        return [host]
    return ["93.184.216.34"]


async def ok_probe(target):
    return Check(t=now_ms(), ok=True, ms=30.0, code=200)


def make_settings(tmp_path, **kw):
    targets = tmp_path / "targets.json"
    targets.write_text(json.dumps([{"id": "alpha", "url": "https://alpha.test"}]), encoding="utf-8")
    base = dict(interval=0.05, recheck=0.02, guest_interval=0.05, db_path=str(tmp_path / "t.db"),
                targets_file=str(targets), self_url="http://vigia.test", guest_max_per_ip=2)
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def client(tmp_path):
    app = create_app(make_settings(tmp_path), probe=ok_probe, guest_probe=ok_probe, resolver=fake_resolve)
    with TestClient(app) as c:
        yield c


def wait_for(fn, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.03)
    raise AssertionError("la condición no se cumplió a tiempo")


# ---------- incidentes ----------
def c(t, ok):
    return Check(t=t * 1000, ok=ok, ms=10 if ok else None, error=None if ok else "ConnectError")


def test_incidentes_desde_el_historial():
    h = [c(0, True), c(1, False), c(2, False), c(3, False), c(4, True), c(5, True), c(6, True),
         c(7, False), c(8, True)]   # el fallo aislado del segundo 7 no es incidente
    incs = incidents(h)
    assert len(incs) == 1
    assert incs[0]["start"] == 1000 and incs[0]["end"] == 5000 and incs[0]["duration_s"] == 4
    assert mttr(incs) == 4


def test_incidente_en_curso():
    incs = incidents([c(0, True), c(1, False), c(2, False)])
    assert incs[0]["end"] is None and incs[0]["duration_s"] is None
    assert mttr(incs) is None


# ---------- monitores de visitantes ----------
def test_crear_monitor_y_ver_su_estado(client):
    r = client.post("/api/monitors", json={"url": "mi-sitio.test", "name": "Mi tienda"})
    assert r.status_code == 201
    m = r.json()
    assert m["id"].startswith("g") and m["token"] and m["status_url"] == f"/s/{m['id']}"
    st = wait_for(lambda: (s := client.get(f"/api/status/{m['id']}").json())["last"] and s)
    assert st["name"] == "Mi tienda" and st["kind"] == "guest" and st["status"] == "up"
    assert st["url"] == "https://mi-sitio.test/"
    assert client.get(m["badge_url"]).status_code == 200
    assert client.get(m["status_url"]).status_code == 200


def test_monitores_de_visitantes_son_privados(client):
    gid = client.post("/api/monitors", json={"url": "privado.test"}).json()["id"]
    services = client.get("/api/services").json()["services"]
    assert gid not in {s["id"] for s in services}
    assert client.get(f"/api/services/{gid}").status_code == 404
    assert client.post(f"/api/services/{gid}/simulate", json={"down": True}).status_code == 404


def test_no_se_puede_vigilar_la_red_interna(client):
    for url in ["http://127.0.0.1", "intranet.test", "http://169.254.169.254/latest/"]:
        r = client.post("/api/monitors", json={"url": url})
        assert r.status_code == 400, url


def test_limite_por_ip(client):
    for i in range(2):
        assert client.post("/api/monitors", json={"url": f"s{i}.test"}).status_code == 201
    r = client.post("/api/monitors", json={"url": "s3.test"})
    assert r.status_code == 429 and "2" in r.json()["detail"]


def test_borrar_requiere_el_token(client):
    m = client.post("/api/monitors", json={"url": "borrar.test"}).json()
    assert client.delete(f"/api/monitors/{m['id']}", headers={"x-monitor-token": "falso"}).status_code == 404
    assert client.delete(f"/api/monitors/{m['id']}", headers={"x-monitor-token": m["token"]}).status_code == 204
    assert client.get(f"/api/status/{m['id']}").status_code == 404
    # los servicios destacados no se pueden borrar
    assert client.delete("/api/monitors/alpha", headers={"x-monitor-token": "x"}).status_code == 404


def test_estado_de_servicio_destacado(client):
    st = wait_for(lambda: (s := client.get("/api/status/alpha").json())["checks"] and s)
    assert st["kind"] == "featured" and st["expires"] is None
    assert st["incidents"] == [] and st["mttr_s"] is None


def test_monitor_expira(tmp_path):
    app = create_app(make_settings(tmp_path, guest_ttl_h=0.5 / 3600), probe=ok_probe,
                     guest_probe=ok_probe, resolver=fake_resolve)
    with TestClient(app) as c:
        gid = c.post("/api/monitors", json={"url": "corto.test"}).json()["id"]
        wait_for(lambda: c.get(f"/api/status/{gid}").status_code == 404)


def test_monitores_sobreviven_un_reinicio(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings, probe=ok_probe, guest_probe=ok_probe, resolver=fake_resolve)) as c:
        gid = c.post("/api/monitors", json={"url": "persistente.test"}).json()["id"]
    with TestClient(create_app(settings, probe=ok_probe, guest_probe=ok_probe, resolver=fake_resolve)) as c:
        assert c.get(f"/api/status/{gid}").json()["url"] == "https://persistente.test/"
