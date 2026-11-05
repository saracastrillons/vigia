"""Insignias SVG en vivo para los README (estilo shields.io, con los colores de VIGÍA)."""
from __future__ import annotations

from html import escape

COLORS = {"up": "#0fa968", "degraded": "#e8890c", "down": "#e5383b", "unknown": "#8a8a8a"}
STATUS_TXT = {"up": "operativo", "degraded": "degradado", "down": "caído", "unknown": "sin datos"}
_NARROW, _WIDE = set(" .,:;!|ilIjt()[]'"), set("MW%@mw")


def text_width(s: str) -> float:
    """Ancho aproximado en Verdana 11px (sin fuentes en el servidor no hay medida exacta)."""
    return sum(3.9 if c in _NARROW else 9.6 if c in _WIDE else 7.0 for c in s)


def badge_value(summary: dict, metric: str) -> str:
    if metric == "status":
        return STATUS_TXT[summary["status"]]
    if metric == "latency":
        return "—" if summary["p50"] is None else f"{round(summary['p50'])} ms"
    up = summary["uptime"]
    return "—" if up is None else ("100%" if up >= 99.995 else f"{up:.2f}%")


def render(label: str, value: str, status: str) -> str:
    label, value = label.upper(), value.upper()
    lw = round(text_width(label) + 26)
    vw = round(text_width(value) + 16)
    w, color = lw + vw, COLORS.get(status, COLORS["unknown"])
    el, ev = escape(label), escape(value)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="20" role="img" aria-label="{el}: {ev}">
<title>{el}: {ev}</title>
<clipPath id="r"><rect width="{w}" height="20" rx="3"/></clipPath>
<g clip-path="url(#r)">
<rect width="{lw}" height="20" fill="#141414"/>
<rect x="{lw}" width="{vw}" height="20" fill="{color}"/>
</g>
<circle cx="10" cy="10" r="3.5" fill="{color}"/>
<g fill="#fff" font-family="Verdana,DejaVu Sans,Geneva,sans-serif" font-size="11">
<text x="19" y="14" fill="#efece6">{el}</text>
<text x="{lw + vw / 2}" y="14" text-anchor="middle">{ev}</text>
</g>
</svg>"""
