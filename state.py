"""SQLite state: last answer per phone, session dedupe, per-phone rate limit.

No Hermes imports and no credentials are stored here.
"""

from __future__ import annotations

import sqlite3
import threading
import time

SESSION_RETENTION_SECONDS = 24 * 60 * 60
_CLEANUP_BATCH = 500

ACCEPTED = "accepted"
DUPLICATE = "duplicate"
RATE_LIMITED = "rate_limited"


class StateStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ussd_last_answers (
                    phone TEXT PRIMARY KEY,
                    answer TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ussd_sessions (
                    session_id TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (session_id, phone)
                );
                CREATE INDEX IF NOT EXISTS ussd_sessions_created
                    ON ussd_sessions (created_at);
                CREATE TABLE IF NOT EXISTS ussd_rate_events (
                    phone TEXT NOT NULL,
                    ts REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ussd_rate_phone_ts
                    ON ussd_rate_events (phone, ts);
                """
            )
            self._conn.commit()

    # -- last answer -----------------------------------------------------
    def get_last_answer(self, phone: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT answer FROM ussd_last_answers WHERE phone = ?", (phone,)
            ).fetchone()
        return row[0] if row else None

    def set_last_answer(self, phone: str, answer: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO ussd_last_answers (phone, answer, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(phone) DO UPDATE SET "
                "answer = excluded.answer, updated_at = excluded.updated_at",
                (phone, answer, time.time()),
            )
            self._conn.commit()

    # -- admission -------------------------------------------------------
    def admit_question(
        self,
        session_id: str,
        phone: str,
        now: float,
        *,
        limit: int = 3,
        window_seconds: float = 60,
    ) -> str:
        """Atomically dedupe by (session_id, phone) and rate-limit per phone.

        Returns ``accepted``, ``duplicate`` or ``rate_limited``. Only accepted
        calls consume rate budget; rate-limited ones are not recorded, so they
        can be retried once the window passes.
        """
        if limit < 1 or window_seconds <= 0:
            raise ValueError("limit must be >= 1 and window_seconds > 0")
        with self._lock:
            owns_tx = not self._conn.in_transaction
            try:
                if owns_tx:
                    self._conn.execute("BEGIN IMMEDIATE")
                self._cleanup(now, window_seconds)
                seen = self._conn.execute(
                    "SELECT 1 FROM ussd_sessions WHERE session_id = ? AND phone = ? "
                    "AND created_at > ?",
                    (session_id, phone, now - SESSION_RETENTION_SECONDS),
                ).fetchone()
                if seen:
                    outcome = DUPLICATE
                else:
                    (count,) = self._conn.execute(
                        "SELECT COUNT(*) FROM ussd_rate_events "
                        "WHERE phone = ? AND ts > ?",
                        (phone, now - window_seconds),
                    ).fetchone()
                    if count >= limit:
                        outcome = RATE_LIMITED
                    else:
                        self._conn.execute(
                            "INSERT OR REPLACE INTO ussd_sessions (session_id, phone, created_at) "
                            "VALUES (?, ?, ?)",
                            (session_id, phone, now),
                        )
                        self._conn.execute(
                            "INSERT INTO ussd_rate_events (phone, ts) VALUES (?, ?)",
                            (phone, now),
                        )
                        outcome = ACCEPTED
                self._conn.commit()
                return outcome
            except BaseException:
                self._conn.rollback()
                raise

    def _cleanup(self, now: float, window_seconds: float) -> None:
        self._conn.execute(
            "DELETE FROM ussd_sessions WHERE rowid IN (SELECT rowid FROM "
            "ussd_sessions WHERE created_at <= ? LIMIT ?)",
            (now - SESSION_RETENTION_SECONDS, _CLEANUP_BATCH),
        )
        self._conn.execute(
            "DELETE FROM ussd_rate_events WHERE rowid IN (SELECT rowid FROM "
            "ussd_rate_events WHERE ts <= ? LIMIT ?)",
            (now - window_seconds, _CLEANUP_BATCH),
        )

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
