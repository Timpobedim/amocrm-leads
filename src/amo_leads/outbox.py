"""Очередь отправки (outbox) в SQLite: заявка сначала сохраняется, потом отправляется в amoCRM.

Если amoCRM недоступен или упёрся в лимит, заявка не теряется — её дошлёт фоновая задача. Ключ
идемпотентности уникален: двойной клик по кнопке «Отправить» не создаст две заявки. Номер сделки
записывается сразу после её создания, поэтому повтор после сбоя на примечании не создаст вторую сделку.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    lead_id INTEGER,
    contact_id INTEGER,
    last_error TEXT,
    created_at TEXT NOT NULL,
    next_attempt_at TEXT NOT NULL
);
"""
RETRY_DELAYS = [timedelta(minutes=minutes) for minutes in (1, 5, 15, 60)]  # дальше — раз в час


@dataclass(frozen=True)
class Submission:
    id: int
    key: str
    payload: str
    status: str  # pending — ждёт отправки, sent — в amoCRM, failed — amoCRM отказал, нужен человек
    attempts: int
    lead_id: int | None
    contact_id: int | None
    last_error: str | None


class Outbox:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:  # транзакция: commit при успехе, rollback при исключении
                yield db
        finally:
            db.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> Submission:
        return Submission(
            row["id"],
            row["key"],
            row["payload"],
            row["status"],
            row["attempts"],
            row["lead_id"],
            row["contact_id"],
            row["last_error"],
        )

    def add(self, key: str, payload: str, now: datetime) -> tuple[Submission, bool]:
        """(заявка, создана ли сейчас): повтор с тем же ключом возвращает первую заявку."""
        with self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO submissions (key, payload, created_at, next_attempt_at) "
                "VALUES (?, ?, ?, ?)",
                (key, payload, now.isoformat(), now.isoformat()),
            )
            row = db.execute("SELECT * FROM submissions WHERE key = ?", (key,)).fetchone()
        return self._row(row), cursor.rowcount == 1

    def get(self, submission_id: int) -> Submission:
        with self._connect() as db:
            return self._row(
                db.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
            )

    def due(self, now: datetime, limit: int = 20) -> list[Submission]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM submissions WHERE status = 'pending' AND next_attempt_at <= ? "
                "ORDER BY id LIMIT ?",
                (now.isoformat(), limit),
            ).fetchall()
        return [self._row(row) for row in rows]

    def set_lead(self, submission_id: int, lead_id: int, contact_id: int) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE submissions SET lead_id = ?, contact_id = ? WHERE id = ?",
                (lead_id, contact_id, submission_id),
            )

    def mark_sent(self, submission_id: int) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE submissions SET status = 'sent', last_error = NULL, attempts = attempts + 1 "
                "WHERE id = ?",
                (submission_id,),
            )

    def mark_retry(self, submission_id: int, error: str, now: datetime) -> None:
        with self._connect() as db:
            attempts = db.execute(
                "SELECT attempts FROM submissions WHERE id = ?", (submission_id,)
            ).fetchone()[0]
            delay = RETRY_DELAYS[min(attempts, len(RETRY_DELAYS) - 1)]
            db.execute(
                "UPDATE submissions SET attempts = attempts + 1, last_error = ?, next_attempt_at = ? "
                "WHERE id = ?",
                (error, (now + delay).isoformat(), submission_id),
            )

    def mark_failed(self, submission_id: int, error: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE submissions SET status = 'failed', last_error = ?, attempts = attempts + 1 "
                "WHERE id = ?",
                (error, submission_id),
            )

    def counts(self) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute("SELECT status, count(*) FROM submissions GROUP BY status").fetchall()
        return dict.fromkeys(("pending", "sent", "failed"), 0) | {row[0]: row[1] for row in rows}
