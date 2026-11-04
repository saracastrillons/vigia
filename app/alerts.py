"""Alertas por Telegram. Solo se activan si TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID existen."""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

import httpx

from .config import Settings, Target

log = logging.getLogger("vigia.alerts")
Notifier = Callable[[Target, str, dict], Awaitable[None]]


def format_alert(target: Target, prev: str, summary: dict, simulated: bool) -> str:
    tag = " (simulado)" if simulated else ""
    if summary["status"] == "down":
        last = summary.get("last") or {}
        why = last.get("error") or (f"HTTP {last['code']}" if last.get("code") else "sin respuesta")
        return f"🔴 {target.name} está CAÍDO{tag}\n{summary['failures']}/3 chequeos fallidos · {why}\n{target.url}"
    return f"🟢 {target.name} se recuperó{tag}\nEstado anterior: {prev}\n{target.url}"


def telegram_notifier(settings: Settings) -> Notifier | None:
    token, chat = settings.telegram_token, settings.telegram_chat
    if not (token and chat):
        return None

    async def notify(target: Target, prev: str, summary: dict) -> None:
        text = format_alert(target, prev, summary, summary.get("simulated", False))
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat, "text": text},
                )
                r.raise_for_status()
        except httpx.HTTPError as e:  # una alerta fallida nunca debe tumbar el monitor
            log.warning("No se pudo enviar la alerta de %s: %s", target.id, type(e).__name__)

    return notify
