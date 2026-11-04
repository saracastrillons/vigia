"""Reglas de estado de VIGÍA.

El modo demo del frontend (web/index.html, función `summarize`) implementa
exactamente estas mismas reglas, para que demo y backend se comporten igual.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

UP, DEGRADED, DOWN, UNKNOWN = "up", "degraded", "down", "unknown"


@dataclass(frozen=True)
class Check:
    """Resultado de un chequeo. `t` en milisegundos epoch, `ms` = latencia."""

    t: float
    ok: bool
    ms: float | None
    code: int | None = None
    error: str | None = None


def percentile(values: Iterable[float], p: float) -> float | None:
    """Percentil por rango más cercano (nearest-rank)."""
    data = sorted(values)
    if not data:
        return None
    rank = max(0, math.ceil(p / 100 * len(data)) - 1)
    return data[rank]


def evaluate(history: Sequence[Check], slow_ms: float) -> tuple[str, int]:
    """Devuelve (estado, fallos en los últimos 3 chequeos).

    - down:     2 o más de los últimos 3 chequeos fallaron (regla 2 de 3).
    - degraded: el último chequeo falló (aún sin confirmar) o fue más lento que `slow_ms`.
    - up:       todo lo demás.
    La recuperación también exige 2 de 3: un único chequeo bueno no basta.
    """
    if not history:
        return UNKNOWN, 0
    failures = sum(1 for c in history[-3:] if not c.ok)
    last = history[-1]
    if failures >= 2:
        return DOWN, failures
    if not last.ok or (last.ms is not None and last.ms > slow_ms):
        return DEGRADED, failures
    return UP, failures


def summarize(history: Sequence[Check], slow_ms: float) -> dict:
    status, failures = evaluate(history, slow_ms)
    ok = [c for c in history if c.ok]
    latencies = [c.ms for c in ok if c.ms is not None]
    last = history[-1] if history else None
    return {
        "status": status,
        "failures": failures,
        "uptime": round(len(ok) / len(history) * 100, 3) if history else None,
        "p50": percentile(latencies, 50),
        "p95": percentile(latencies, 95),
        "last": None if last is None else {
            "t": last.t, "ok": last.ok, "ms": last.ms, "code": last.code, "error": last.error,
        },
    }
