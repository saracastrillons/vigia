import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.evaluator import Check
from app.inspect import Blocked, RateLimiter, check_ip, inspect, normalize
from app.main import create_app
from app.monitor import now_ms

# Dominios ficticios -> IPs. "rebind.test" simula un DNS malicioso que apunta a la red interna.
DNS = {
    "public.test": ["93.184.216.34"],
    "other.test": ["151.101.1.69"],
    "rebind.test": ["10.0.0.7"],
    "mixed.test": ["93.184.216.34", "127.0.0.1"],
}


async def fake_resolve(host, port):
    if host in DNS:
        return DNS[host]
    if host.replace(".", "").isdigit() or ":" in host:
        return [host]
    raise OSError("NXDOMAIN")


def fake_web(request: httpx.Request) -> httpx.Response:
    host = request.headers["host"]
    if host == "public.test" and request.url.path == "/":
        return httpx.Response(301, headers={"location": "/home"})
    if host == "public.test" and request.url.path == "/home":
        return httpx.Response(200, headers={"server": "nginx", "content-type": "text/html; charset=utf-8"})
    if host == "other.test" and request.url.path == "/leak":
        return httpx.Response(302, headers={"location": "http://rebind.test/admin"})
    if host == "other.test":
        return httpx.Response(503)
    return httpx.Response(404)


def run(coro):
    return asyncio.run(coro)


async def do_inspect(url):
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake_web), follow_redirects=False) as c:
        return await inspect(url, c, fake_resolve, measure_connect=False)


# ---------- normalización ----------
def test_normalize_agrega_https_y_ruta():
    assert normalize("github.com") == "https://github.com/"
    assert normalize("  http://x.test/a?b=1#frag ") == "http://x.test/a?b=1"


@pytest.mark.parametrize("bad", ["", "ftp://x.test", "file:///etc/passwd", "https://user:pw@x.test",
                                 "https://x.test:22", "https://x.test:6379", "https://", "x" * 3000])
def test_normalize_rechaza(bad):
    with pytest.raises(Blocked):
        normalize(bad)


# ---------- IPs ----------
@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254",
                                "0.0.0.0", "100.64.0.1", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1", "224.0.0.1"])
def test_ips_privadas_bloqueadas(ip):
    with pytest.raises(Blocked):
        check_ip(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"])
def test_ips_publicas_permitidas(ip):
    check_ip(ip)


# ---------- inspección ----------
def test_sigue_redirecciones_y_fija_la_ip():
    r = run(do_inspect("public.test"))
    assert r["ok"] is True and r["status"] == 200
    assert r["final_url"] == "https://public.test/home"
    assert r["redirects"] == [{"url": "https://public.test/", "status": 301}]
    assert r["ip"] == "93.184.216.34"
    assert r["server"] == "nginx" and r["content_type"] == "text/html"


def test_error_http_no_es_ok():
    r = run(do_inspect("http://other.test/"))
    assert r["ok"] is False and r["status"] == 503


def test_dns_rebinding_bloqueado():
    with pytest.raises(Blocked, match="SSRF"):
        run(do_inspect("rebind.test"))


def test_si_alguna_ip_es_privada_se_bloquea():
    with pytest.raises(Blocked):
        run(do_inspect("mixed.test"))


def test_redireccion_hacia_red_interna_bloqueada():
    with pytest.raises(Blocked, match="10.0.0.7"):
        run(do_inspect("http://other.test/leak"))


def test_dominio_inexistente():
    with pytest.raises(Blocked, match="resolver"):
        run(do_inspect("nope.test"))


# ---------- límite de peticiones ----------
def test_rate_limiter_ventana_deslizante():
    rl = RateLimiter(2, 60)
    assert rl.check("a", now=0) == 0
    assert rl.check("a", now=1) == 0
    assert rl.check("a", now=2) == pytest.approx(58)
    assert rl.check("b", now=2) == 0          # otra IP no se ve afectada
    assert rl.check("a", now=61) == 0         # la primera petición ya salió de la ventana


# ---------- API ----------
@pytest.fixture
def client(tmp_path):
    targets = tmp_path / "targets.json"
    targets.write_text(json.dumps([{"id": "alpha", "name": "ALPHA", "url": "https://alpha.test"}]), encoding="utf-8")
    settings = Settings(interval=0.05, recheck=0.02, db_path=str(tmp_path / "t.db"), targets_file=str(targets),
                        self_url="http://vigia.test", inspect_limit=3)

    async def fake_probe(target):
        return Check(t=now_ms(), ok=True, ms=42.0, code=200)

    app = create_app(settings, probe=fake_probe, resolver=fake_resolve,
                     inspect_transport=httpx.MockTransport(fake_web), measure_connect=False)
    with TestClient(app) as c:
        yield c


def test_api_inspect_ok(client):
    r = client.post("/api/inspect", json={"url": "public.test"})
    assert r.status_code == 200
    assert r.json()["status"] == 200


def test_api_inspect_bloquea_localhost(client):
    r = client.post("/api/inspect", json={"url": "http://127.0.0.1:8080/admin"})
    assert r.status_code == 400
    assert "SSRF" in r.json()["detail"]


def test_api_inspect_limite_por_ip(client):
    for _ in range(3):
        assert client.post("/api/inspect", json={"url": "http://127.0.0.1"}).status_code == 400
    r = client.post("/api/inspect", json={"url": "public.test"})
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0


def test_badge_svg(client):
    import time
    deadline = time.time() + 3
    while client.get("/api/services/alpha").json()["last"] is None and time.time() < deadline:
        time.sleep(0.03)
    r = client.get("/badge/alpha.svg")
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert "ALPHA" in r.text and "100%" in r.text and "#0fa968" in r.text
    assert "42 MS" in client.get("/badge/alpha.svg?metric=latency").text
    assert "OPERATIVO" in client.get("/badge/alpha.svg?metric=status").text
    assert client.get("/badge/alpha.svg?metric=nope").status_code == 422
    assert client.get("/badge/nope.svg").status_code == 404


def test_metadatos_para_compartir(client):
    html = client.get("/").text
    assert 'property="og:image" content="http://testserver/og.png"' in html
    assert "__ORIGIN__" not in html
