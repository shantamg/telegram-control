#!/usr/bin/python3
"""SQLite persistence for Telegram Control's durable transport."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


SCHEMA_VERSION = 1


class StoreError(RuntimeError):
    """Base error for durable-store failures."""


class IncompatibleSchemaError(StoreError):
    """Raised when a database was created by a newer controller."""


class LeaseLostError(StoreError):
    """Raised when a worker attempts to mutate a lease it no longer owns."""


@dataclass(frozen=True)
class InboxJob:
    job_id: int
    update_id: int
    kind: str
    payload: dict[str, Any]
    attempts: int


@dataclass(frozen=True)
class OutboxMessage:
    message_id: int
    operation_id: str
    method: str
    params: dict[str, Any]
    attempts: int


MIGRATION_1 = (
    """
    CREATE TABLE telegram_updates (
        update_id INTEGER PRIMARY KEY,
        raw_json TEXT NOT NULL,
        received_at REAL NOT NULL,
        ingest_state TEXT NOT NULL
            CHECK (ingest_state IN ('accepted', 'ignored'))
    )
    """,
    """
    CREATE TABLE inbox_jobs (
        job_id INTEGER PRIMARY KEY AUTOINCREMENT,
        update_id INTEGER NOT NULL UNIQUE
            REFERENCES telegram_updates(update_id) ON DELETE RESTRICT,
        kind TEXT NOT NULL
            CHECK (kind IN ('message', 'callback_query')),
        payload_json TEXT NOT NULL,
        state TEXT NOT NULL
            CHECK (state IN ('queued', 'leased', 'succeeded', 'dead')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        available_at REAL NOT NULL,
        lease_owner TEXT,
        lease_expires_at REAL,
        last_error TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX inbox_jobs_ready
    ON inbox_jobs(state, available_at, job_id)
    """,
    """
    CREATE TABLE outbox_messages (
        message_id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL UNIQUE,
        method TEXT NOT NULL,
        params_json TEXT NOT NULL,
        state TEXT NOT NULL
            CHECK (state IN ('queued', 'leased', 'sent', 'dead')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        available_at REAL NOT NULL,
        lease_owner TEXT,
        lease_expires_at REAL,
        last_error TEXT,
        telegram_result_json TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX outbox_messages_ready
    ON outbox_messages(state, available_at, message_id)
    """,
    """
    CREATE TABLE controller_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        subject_type TEXT,
        subject_id TEXT,
        details_json TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
)


class DurableStore:
    """Small transactional repository used by collector, worker, and sender."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(
            str(self.path),
            timeout=5,
            isolation_level=None,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.execute("PRAGMA foreign_keys = ON")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        try:
            self._migrate()
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.execute("PRAGMA synchronous = FULL")
        except BaseException:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "DurableStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _migrate(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise IncompatibleSchemaError(
                    f"Database schema {current} is newer than supported schema "
                    f"{SCHEMA_VERSION}."
                )
            if current == 0:
                for statement in MIGRATION_1:
                    self.connection.execute(statement)
                self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def quick_check(self) -> str:
        return str(self.connection.execute("PRAGMA quick_check").fetchone()[0])

    def initialize_poll_offset(self, offset: Optional[int]) -> None:
        """Seed the DB once from Stage 0's offset without moving it backwards."""
        if offset is None:
            return
        now = time.time()
        self.connection.execute(
            """
            INSERT INTO controller_state(key, value, updated_at)
            VALUES ('telegram_offset', ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = CASE
                    WHEN CAST(excluded.value AS INTEGER) >
                         CAST(controller_state.value AS INTEGER)
                    THEN excluded.value
                    ELSE controller_state.value
                END,
                updated_at = CASE
                    WHEN CAST(excluded.value AS INTEGER) >
                         CAST(controller_state.value AS INTEGER)
                    THEN excluded.updated_at
                    ELSE controller_state.updated_at
                END
            """,
            (str(int(offset)), now),
        )

    def poll_offset(self) -> Optional[int]:
        row = self.connection.execute(
            "SELECT value FROM controller_state WHERE key = 'telegram_offset'"
        ).fetchone()
        return int(row["value"]) if row else None

    @staticmethod
    def _update_kind(update: dict[str, Any]) -> Optional[str]:
        if isinstance(update.get("message"), dict):
            return "message"
        if isinstance(update.get("callback_query"), dict):
            return "callback_query"
        return None

    def ingest_update(self, update: dict[str, Any], now: Optional[float] = None) -> bool:
        """Persist one update and its job, then advance the polling offset atomically."""
        try:
            update_id = int(update["update_id"])
        except (KeyError, TypeError, ValueError):
            raise StoreError("Telegram update has no valid integer update_id.") from None
        if update_id < 0:
            raise StoreError("Telegram update_id cannot be negative.")

        timestamp = time.time() if now is None else float(now)
        kind = self._update_kind(update)
        raw_json = json.dumps(update, separators=(",", ":"), sort_keys=True)
        inserted = False

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                """
                INSERT INTO telegram_updates(update_id, raw_json, received_at, ingest_state)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(update_id) DO NOTHING
                """,
                (update_id, raw_json, timestamp, "accepted" if kind else "ignored"),
            )
            inserted = cursor.rowcount == 1
            if inserted and kind:
                self.connection.execute(
                    """
                    INSERT INTO inbox_jobs(
                        update_id, kind, payload_json, state, attempts,
                        available_at, created_at, updated_at
                    )
                    VALUES (?, ?, ?, 'queued', 0, ?, ?, ?)
                    """,
                    (update_id, kind, raw_json, timestamp, timestamp, timestamp),
                )

            existing = self.connection.execute(
                "SELECT value FROM controller_state WHERE key = 'telegram_offset'"
            ).fetchone()
            next_offset = update_id + 1
            if existing:
                next_offset = max(next_offset, int(existing["value"]))
            self.connection.execute(
                """
                INSERT INTO controller_state(key, value, updated_at)
                VALUES ('telegram_offset', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (str(next_offset), timestamp),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return inserted

    def claim_job(
        self,
        worker_id: str,
        now: Optional[float] = None,
        lease_seconds: float = 60.0,
    ) -> Optional[InboxJob]:
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                UPDATE inbox_jobs
                SET state = 'queued', lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE state = 'leased' AND lease_expires_at <= ?
                """,
                (timestamp, timestamp),
            )
            row = self.connection.execute(
                """
                SELECT job_id
                FROM inbox_jobs
                WHERE state = 'queued' AND available_at <= ?
                ORDER BY job_id
                LIMIT 1
                """,
                (timestamp,),
            ).fetchone()
            if row is None:
                self.connection.execute("COMMIT")
                return None
            job_id = int(row["job_id"])
            self.connection.execute(
                """
                UPDATE inbox_jobs
                SET state = 'leased', attempts = attempts + 1,
                    lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND state = 'queued'
                """,
                (worker_id, timestamp + lease_seconds, timestamp, job_id),
            )
            claimed = self.connection.execute(
                "SELECT * FROM inbox_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return InboxJob(
            job_id=int(claimed["job_id"]),
            update_id=int(claimed["update_id"]),
            kind=str(claimed["kind"]),
            payload=json.loads(claimed["payload_json"]),
            attempts=int(claimed["attempts"]),
        )

    def complete_job(
        self, job_id: int, worker_id: str, now: Optional[float] = None
    ) -> None:
        timestamp = time.time() if now is None else float(now)
        cursor = self.connection.execute(
            """
            UPDATE inbox_jobs
            SET state = 'succeeded', lease_owner = NULL, lease_expires_at = NULL,
                last_error = NULL, updated_at = ?
            WHERE job_id = ? AND state = 'leased' AND lease_owner = ?
            """,
            (timestamp, int(job_id), worker_id),
        )
        if cursor.rowcount != 1:
            raise LeaseLostError(f"Inbox lease for job {job_id} is no longer owned.")

    def fail_job(
        self,
        job_id: int,
        worker_id: str,
        error: str,
        now: Optional[float] = None,
        max_attempts: int = 5,
        base_delay: float = 2.0,
        max_delay: float = 300.0,
    ) -> str:
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT attempts
                FROM inbox_jobs
                WHERE job_id = ? AND state = 'leased' AND lease_owner = ?
                """,
                (int(job_id), worker_id),
            ).fetchone()
            if row is None:
                raise LeaseLostError(f"Inbox lease for job {job_id} is no longer owned.")
            attempts = int(row["attempts"])
            new_state = "dead" if attempts >= max_attempts else "queued"
            delay = min(max_delay, base_delay * (2 ** max(0, attempts - 1)))
            cursor = self.connection.execute(
                """
                UPDATE inbox_jobs
                SET state = ?, available_at = ?, lease_owner = NULL,
                    lease_expires_at = NULL, last_error = ?, updated_at = ?
                WHERE job_id = ? AND state = 'leased' AND lease_owner = ?
                """,
                (
                    new_state,
                    timestamp if new_state == "dead" else timestamp + delay,
                    str(error)[:2000],
                    timestamp,
                    int(job_id),
                    worker_id,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(
                    f"Inbox lease for job {job_id} is no longer owned."
                )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return new_state

    def enqueue_api_call(
        self,
        operation_id: str,
        method: str,
        params: dict[str, Any],
        now: Optional[float] = None,
    ) -> int:
        if not operation_id or not method:
            raise StoreError("Outbox operation_id and method are required.")
        timestamp = time.time() if now is None else float(now)
        params_json = json.dumps(params, separators=(",", ":"), sort_keys=True)
        self.connection.execute(
            """
            INSERT INTO outbox_messages(
                operation_id, method, params_json, state, attempts,
                available_at, created_at, updated_at
            )
            VALUES (?, ?, ?, 'queued', 0, ?, ?, ?)
            ON CONFLICT(operation_id) DO NOTHING
            """,
            (operation_id, method, params_json, timestamp, timestamp, timestamp),
        )
        row = self.connection.execute(
            """
            SELECT message_id, method, params_json
            FROM outbox_messages
            WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        if row["method"] != method or row["params_json"] != params_json:
            raise StoreError(
                f"Outbox operation {operation_id!r} was reused with a different payload."
            )
        return int(row["message_id"])

    def claim_outbox(
        self,
        worker_id: str,
        now: Optional[float] = None,
        lease_seconds: float = 60.0,
    ) -> Optional[OutboxMessage]:
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                UPDATE outbox_messages
                SET state = 'queued', lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE state = 'leased' AND lease_expires_at <= ?
                """,
                (timestamp, timestamp),
            )
            row = self.connection.execute(
                """
                SELECT message_id
                FROM outbox_messages
                WHERE state = 'queued' AND available_at <= ?
                ORDER BY message_id
                LIMIT 1
                """,
                (timestamp,),
            ).fetchone()
            if row is None:
                self.connection.execute("COMMIT")
                return None
            message_id = int(row["message_id"])
            self.connection.execute(
                """
                UPDATE outbox_messages
                SET state = 'leased', attempts = attempts + 1,
                    lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE message_id = ? AND state = 'queued'
                """,
                (worker_id, timestamp + lease_seconds, timestamp, message_id),
            )
            claimed = self.connection.execute(
                "SELECT * FROM outbox_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return OutboxMessage(
            message_id=int(claimed["message_id"]),
            operation_id=str(claimed["operation_id"]),
            method=str(claimed["method"]),
            params=json.loads(claimed["params_json"]),
            attempts=int(claimed["attempts"]),
        )

    def complete_outbox(
        self,
        message_id: int,
        worker_id: str,
        result: Any,
        now: Optional[float] = None,
    ) -> None:
        timestamp = time.time() if now is None else float(now)
        result_json = json.dumps(result, separators=(",", ":"), sort_keys=True)
        cursor = self.connection.execute(
            """
            UPDATE outbox_messages
            SET state = 'sent', lease_owner = NULL, lease_expires_at = NULL,
                last_error = NULL, telegram_result_json = ?, updated_at = ?
            WHERE message_id = ? AND state = 'leased' AND lease_owner = ?
            """,
            (result_json, timestamp, int(message_id), worker_id),
        )
        if cursor.rowcount != 1:
            raise LeaseLostError(
                f"Outbox lease for message {message_id} is no longer owned."
            )

    def fail_outbox(
        self,
        message_id: int,
        worker_id: str,
        error: str,
        now: Optional[float] = None,
        max_attempts: int = 8,
        base_delay: float = 2.0,
        max_delay: float = 300.0,
    ) -> str:
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT attempts
                FROM outbox_messages
                WHERE message_id = ? AND state = 'leased' AND lease_owner = ?
                """,
                (int(message_id), worker_id),
            ).fetchone()
            if row is None:
                raise LeaseLostError(
                    f"Outbox lease for message {message_id} is no longer owned."
                )
            attempts = int(row["attempts"])
            new_state = "dead" if attempts >= max_attempts else "queued"
            delay = min(max_delay, base_delay * (2 ** max(0, attempts - 1)))
            cursor = self.connection.execute(
                """
                UPDATE outbox_messages
                SET state = ?, available_at = ?, lease_owner = NULL,
                    lease_expires_at = NULL, last_error = ?, updated_at = ?
                WHERE message_id = ? AND state = 'leased' AND lease_owner = ?
                """,
                (
                    new_state,
                    timestamp if new_state == "dead" else timestamp + delay,
                    str(error)[:2000],
                    timestamp,
                    int(message_id),
                    worker_id,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(
                    f"Outbox lease for message {message_id} is no longer owned."
                )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return new_state

    def retry_dead(self, queue: str, now: Optional[float] = None) -> int:
        timestamp = time.time() if now is None else float(now)
        if queue == "inbox":
            cursor = self.connection.execute(
                """
                UPDATE inbox_jobs
                SET state = 'queued', attempts = 0, available_at = ?,
                    last_error = NULL, updated_at = ?
                WHERE state = 'dead'
                """,
                (timestamp, timestamp),
            )
        elif queue == "outbox":
            cursor = self.connection.execute(
                """
                UPDATE outbox_messages
                SET state = 'queued', attempts = 0, available_at = ?,
                    last_error = NULL, updated_at = ?
                WHERE state = 'dead'
                """,
                (timestamp, timestamp),
            )
        else:
            raise StoreError("Queue must be 'inbox' or 'outbox'.")
        return int(cursor.rowcount)

    def status_counts(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for label, table in (
            ("updates", "telegram_updates"),
            ("inbox", "inbox_jobs"),
            ("outbox", "outbox_messages"),
        ):
            state_column = "ingest_state" if label == "updates" else "state"
            rows = self.connection.execute(
                f"SELECT {state_column} AS state, COUNT(*) AS count "
                f"FROM {table} GROUP BY {state_column}"
            ).fetchall()
            result[label] = {
                str(row["state"]): int(row["count"])
                for row in rows
            }
        return result
