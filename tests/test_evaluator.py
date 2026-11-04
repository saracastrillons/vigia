from app.evaluator import Check, evaluate, percentile, summarize

SLOW = 800


def ok(ms=100):
    return Check(t=0, ok=True, ms=ms, code=200)


def fail():
    return Check(t=0, ok=False, ms=None, error="ConnectError")


def test_percentile_nearest_rank():
    data = list(range(1, 101))
    assert percentile(data, 50) == 50
    assert percentile(data, 95) == 95
    assert percentile([42], 95) == 42
    assert percentile([], 50) is None


def test_sin_historial_es_desconocido():
    assert evaluate([], SLOW) == ("unknown", 0)


def test_todo_bien_es_up():
    assert evaluate([ok(), ok(), ok()], SLOW) == ("up", 0)


def test_un_fallo_aislado_no_es_caida():
    # regla 2 de 3: un solo fallo queda "degradado" mientras se confirma
    assert evaluate([ok(), ok(), fail()], SLOW) == ("degraded", 1)


def test_dos_de_tres_fallos_es_caida():
    assert evaluate([ok(), fail(), fail()], SLOW) == ("down", 2)
    assert evaluate([fail(), ok(), fail()], SLOW) == ("down", 2)


def test_recuperacion_tambien_exige_dos_de_tres():
    assert evaluate([fail(), fail(), ok()], SLOW)[0] == "down"
    assert evaluate([fail(), ok(), ok()], SLOW)[0] == "up"


def test_latencia_alta_es_degradado():
    assert evaluate([ok(), ok(), ok(1200)], SLOW) == ("degraded", 0)


def test_solo_cuentan_los_ultimos_tres():
    history = [fail()] * 10 + [ok(), ok(), ok()]
    assert evaluate(history, SLOW)[0] == "up"


def test_summarize_calcula_uptime_y_percentiles():
    history = [ok(100)] * 9 + [fail()]
    s = summarize(history, SLOW)
    assert s["uptime"] == 90.0
    assert s["p50"] == 100
    assert s["last"]["ok"] is False
    assert s["status"] == "degraded"


def test_summarize_ignora_latencias_de_fallos():
    history = [ok(50), Check(t=0, ok=False, ms=3000, code=500), ok(70)]
    s = summarize(history, SLOW)
    assert s["p95"] == 70
