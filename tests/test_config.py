import json

import pytest

from app.alerts import format_alert
from app.config import Target, load_targets


def write(tmp_path, data):
    p = tmp_path / "targets.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_reemplaza_self(tmp_path):
    path = write(tmp_path, [{"id": "vigia", "url": "{self}/healthz"}])
    [t] = load_targets(path, "http://127.0.0.1:8090/")
    assert t.url == "http://127.0.0.1:8090/healthz"
    assert t.name == "VIGIA"


@pytest.mark.parametrize("bad", [
    [],
    [{"id": "Mayus", "url": "https://x.test"}],
    [{"id": "a", "url": "ftp://x.test"}],
    [{"id": "a", "url": "https://x.test"}, {"id": "a", "url": "https://y.test"}],
])
def test_rechaza_targets_invalidos(tmp_path, bad):
    with pytest.raises(ValueError):
        load_targets(write(tmp_path, bad), "http://x")


def test_formato_de_alertas():
    t = Target(id="nexus", name="NEXUS", url="https://nexus.test")
    down = format_alert(t, "up", {"status": "down", "failures": 2, "last": {"error": "ConnectError", "code": None}}, False)
    assert "CAÍDO" in down and "2/3" in down and "ConnectError" in down
    up = format_alert(t, "down", {"status": "up"}, True)
    assert "recuperó" in up and "(simulado)" in up


def test_umbral_de_lentitud_por_servicio(tmp_path):
    path = write(tmp_path, [{"id": "gh", "url": "https://github.com/x", "slow_ms": 2000}, {"id": "b", "url": "https://b.test"}])
    gh, b = load_targets(path, "http://x")
    assert gh.slow_ms == 2000
    assert b.slow_ms is None
