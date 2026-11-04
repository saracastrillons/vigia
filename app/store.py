"""Historial de chequeos en SQLite (una fila por chequeo = serie de tiempo)."""
from __future__ import annotations

import sqlite3
import time

from .evaluator import Check

RETENTION_DAYS = 30


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS checks (
                id         INTEGER PRIMARY KEY,
                service_id TEXT    NOT NULL,
                ts         REAL    NOT NULL,
                ok         INTEGER NOT NULL,
                ms         REAL,
                code       INTEGER,
                error      TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_checks_service_ts ON checks(service_id, ts);
            """
        )
        self._writes = 0

    def add(self, service_id: str, c: Check) -> None:
        self.db.execute(
            "INSERT INTO checks (service_id, ts, ok, ms, code, error) VALUES (?, ?, ?, ?, ?, ?)",
            (service_id, c.t, int(c.ok), c.ms, c.code, c.error),
        )
        self._writes += 1
        if self._writes % 500 == 0:
            cutoff = (time.time() - RETENTION_DAYS * 86400) * 1000
            self.db.execute("DELETE FROM checks WHERE ts < ?", (cutoff,))
        self.db.commit()

    def recent(self, service_id: str, limit: int) -> list[Check]:
        rows = self.db.execute(
            "SELECT ts, ok, ms, code, error FROM checks WHERE service_id = ? ORDER BY ts DESC LIMIT ?",
            (service_id, limit),
        ).fetchall()
        return [Check(t=r[0], ok=bool(r[1]), ms=r[2], code=r[3], error=r[4]) for r in reversed(rows)]

    def close(self) -> None:
        self.db.close()
