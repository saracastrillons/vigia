"""Configuración por variables de entorno y lista de servicios (targets.json)."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "si", "sí"}


@dataclass(frozen=True)
class Settings:
    interval: float = 30.0       # segundos entre chequeos normales
    recheck: float = 3.0         # segundos hasta re-chequear tras un fallo
    timeout: float = 5.0         # timeout de cada petición HTTP
    slow_ms: float = 800.0       # por encima de esto el servicio está "degradado"
    window: int = 120            # chequeos en memoria por servicio (uptime, p50, p95)
    db_path: str = str(ROOT / "vigia.db")
    targets_file: str = str(ROOT / "targets.json")
    self_url: str = "http://127.0.0.1:8090"
    allow_simulate: bool = True
    simulate_ttl: float = 60.0   # un incidente simulado se restaura solo
    telegram_token: str | None = None
    telegram_chat: str | None = None
    inspect_limit: int = 5       # revisiones "Pruébalo" por IP...
    inspect_per: float = 60.0    # ...en esta ventana de segundos
    trust_proxy: bool = False    # usar X-Forwarded-For (solo detrás de un proxy propio, p. ej. Render)

    @classmethod
    def from_env(cls) -> "Settings":
        port = os.getenv("PORT", "8090")
        return cls(
            interval=float(os.getenv("VIGIA_INTERVAL", 30)),
            recheck=float(os.getenv("VIGIA_RECHECK", 3)),
            timeout=float(os.getenv("VIGIA_TIMEOUT", 5)),
            slow_ms=float(os.getenv("VIGIA_SLOW_MS", 800)),
            window=int(os.getenv("VIGIA_WINDOW", 120)),
            db_path=os.getenv("VIGIA_DB", cls.db_path),
            targets_file=os.getenv("VIGIA_TARGETS", cls.targets_file),
            self_url=os.getenv("VIGIA_SELF_URL", f"http://127.0.0.1:{port}"),
            allow_simulate=_env_bool("VIGIA_ALLOW_SIMULATE", True),
            simulate_ttl=float(os.getenv("VIGIA_SIMULATE_TTL", 60)),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat=os.getenv("TELEGRAM_CHAT_ID") or None,
            inspect_limit=int(os.getenv("VIGIA_INSPECT_LIMIT", 5)),
            inspect_per=float(os.getenv("VIGIA_INSPECT_PER", 60)),
            trust_proxy=_env_bool("VIGIA_TRUST_PROXY", False),
        )


@dataclass(frozen=True)
class Target:
    id: str
    name: str
    url: str
    desc: str = ""
    expect_status: int | None = None   # si se define, solo ese código cuenta como éxito
    slow_ms: float | None = None       # umbral de lentitud propio (si no, el global)


_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def load_targets(path: str, self_url: str) -> list[Target]:
    """Lee targets.json. `{self}` en una URL se reemplaza por la URL de VIGÍA."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    targets: list[Target] = []
    seen: set[str] = set()
    for item in raw:
        t = Target(
            id=item["id"],
            name=item.get("name", item["id"].upper()),
            url=item["url"].replace("{self}", self_url.rstrip("/")),
            desc=item.get("desc", ""),
            expect_status=item.get("expect_status"),
            slow_ms=item.get("slow_ms"),
        )
        if not _ID.match(t.id):
            raise ValueError(f"id inválido en targets.json: {t.id!r}")
        if t.id in seen:
            raise ValueError(f"id repetido en targets.json: {t.id!r}")
        if not t.url.startswith(("http://", "https://")):
            raise ValueError(f"URL inválida para {t.id!r}: {t.url!r}")
        seen.add(t.id)
        targets.append(t)
    if not targets:
        raise ValueError("targets.json no tiene servicios")
    return targets
