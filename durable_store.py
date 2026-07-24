#!/usr/bin/python3
"""SQLite persistence for Telegram Control's durable transport."""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


SCHEMA_VERSION = 4


class StoreError(RuntimeError):
    """Base error for durable-store failures."""


class IncompatibleSchemaError(StoreError):
    """Raised when a database was created by a newer controller."""


class LeaseLostError(StoreError):
    """Raised when a worker attempts to mutate a lease it no longer owns."""


class CallbackActionError(StoreError):
    """Raised when an opaque callback token cannot be safely executed."""

    def __init__(self, code: str, user_message: str):
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message


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


@dataclass(frozen=True)
class CallbackAction:
    action_id: int
    token: str
    action_type: str
    payload: dict[str, Any]
    chat_id: int
    message_thread_id: Optional[int]
    authorized_user_id: int
    one_time: bool
    expires_at: float


@dataclass(frozen=True)
class MessageRoute:
    route_id: int
    chat_id: int
    message_thread_id: Optional[int]
    telegram_message_id: int
    target_type: str
    target_id: str
    policy: str
    expires_at: float


@dataclass(frozen=True)
class SurfaceBinding:
    binding_id: int
    chat_id: int
    message_thread_id: Optional[int]
    surface_type: str
    display_name: str
    target_type: str
    target_id: str
    state: str


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

MIGRATION_2 = (
    """
    CREATE TABLE callback_actions (
        action_id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL UNIQUE,
        token TEXT NOT NULL UNIQUE,
        action_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        message_thread_id INTEGER,
        authorized_user_id INTEGER NOT NULL,
        one_time INTEGER NOT NULL CHECK (one_time IN (0, 1)),
        state TEXT NOT NULL
            CHECK (state IN ('active', 'consumed', 'expired', 'revoked')),
        expires_at REAL NOT NULL,
        consumed_at REAL,
        consumed_by_update_id INTEGER,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX callback_actions_lookup
    ON callback_actions(token, state, expires_at)
    """,
)

MIGRATION_3 = (
    """
    ALTER TABLE outbox_messages
    ADD COLUMN route_json TEXT
    """,
    """
    CREATE TABLE telegram_message_routes (
        route_id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_outbox_message_id INTEGER NOT NULL UNIQUE
            REFERENCES outbox_messages(message_id) ON DELETE RESTRICT,
        chat_id INTEGER NOT NULL,
        message_thread_id INTEGER NOT NULL,
        telegram_message_id INTEGER NOT NULL,
        target_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        policy TEXT NOT NULL CHECK (policy IN ('reply')),
        state TEXT NOT NULL
            CHECK (state IN ('active', 'expired', 'revoked')),
        expires_at REAL NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE(chat_id, message_thread_id, telegram_message_id)
    )
    """,
    """
    CREATE INDEX telegram_message_routes_lookup
    ON telegram_message_routes(
        chat_id, message_thread_id, telegram_message_id, state, expires_at
    )
    """,
)

MIGRATION_4 = (
    """
    CREATE TABLE surface_bindings (
        binding_id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        message_thread_id INTEGER NOT NULL,
        surface_type TEXT NOT NULL
            CHECK (surface_type IN ('control', 'project', 'task')),
        display_name TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('active', 'revoked')),
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE(chat_id, message_thread_id)
    )
    """,
    """
    CREATE INDEX surface_bindings_target
    ON surface_bindings(target_type, target_id, state)
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
            if current < 1:
                for statement in MIGRATION_1:
                    self.connection.execute(statement)
                current = 1
                self.connection.execute("PRAGMA user_version = 1")
            if current < 2:
                for statement in MIGRATION_2:
                    self.connection.execute(statement)
                current = 2
                self.connection.execute("PRAGMA user_version = 2")
            if current < 3:
                for statement in MIGRATION_3:
                    self.connection.execute(statement)
                current = 3
                self.connection.execute("PRAGMA user_version = 3")
            if current < 4:
                for statement in MIGRATION_4:
                    self.connection.execute(statement)
                current = 4
                self.connection.execute("PRAGMA user_version = 4")
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
        route: Optional[dict[str, Any]] = None,
        now: Optional[float] = None,
    ) -> int:
        if not operation_id or not method:
            raise StoreError("Outbox operation_id and method are required.")
        timestamp = time.time() if now is None else float(now)
        params_json = json.dumps(params, separators=(",", ":"), sort_keys=True)
        route_json = (
            json.dumps(route, separators=(",", ":"), sort_keys=True)
            if route is not None
            else None
        )
        self.connection.execute(
            """
            INSERT INTO outbox_messages(
                operation_id, method, params_json, state, attempts,
                available_at, created_at, updated_at, route_json
            )
            VALUES (?, ?, ?, 'queued', 0, ?, ?, ?, ?)
            ON CONFLICT(operation_id) DO NOTHING
            """,
            (
                operation_id,
                method,
                params_json,
                timestamp,
                timestamp,
                timestamp,
                route_json,
            ),
        )
        row = self.connection.execute(
            """
            SELECT message_id, method, params_json, route_json
            FROM outbox_messages
            WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        if (
            row["method"] != method
            or row["params_json"] != params_json
            or row["route_json"] != route_json
        ):
            raise StoreError(
                f"Outbox operation {operation_id!r} was reused with a different payload."
            )
        return int(row["message_id"])

    @staticmethod
    def _callback_from_row(row: sqlite3.Row) -> CallbackAction:
        return CallbackAction(
            action_id=int(row["action_id"]),
            token=str(row["token"]),
            action_type=str(row["action_type"]),
            payload=json.loads(row["payload_json"]),
            chat_id=int(row["chat_id"]),
            message_thread_id=(
                int(row["message_thread_id"])
                if row["message_thread_id"] is not None
                else None
            ),
            authorized_user_id=int(row["authorized_user_id"]),
            one_time=bool(row["one_time"]),
            expires_at=float(row["expires_at"]),
        )

    def create_callback_action(
        self,
        operation_id: str,
        action_type: str,
        payload: dict[str, Any],
        chat_id: int,
        authorized_user_id: int,
        message_thread_id: Optional[int] = None,
        one_time: bool = True,
        ttl_seconds: float = 24 * 60 * 60,
        now: Optional[float] = None,
    ) -> CallbackAction:
        if not operation_id:
            raise StoreError("Callback operation_id is required.")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", action_type):
            raise StoreError("Callback action_type is invalid.")
        if ttl_seconds <= 0:
            raise StoreError("Callback ttl_seconds must be positive.")
        timestamp = time.time() if now is None else float(now)
        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        expected = (
            action_type,
            payload_json,
            int(chat_id),
            int(message_thread_id) if message_thread_id is not None else None,
            int(authorized_user_id),
            int(bool(one_time)),
        )

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT * FROM callback_actions WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                actual = (
                    str(existing["action_type"]),
                    str(existing["payload_json"]),
                    int(existing["chat_id"]),
                    (
                        int(existing["message_thread_id"])
                        if existing["message_thread_id"] is not None
                        else None
                    ),
                    int(existing["authorized_user_id"]),
                    int(existing["one_time"]),
                )
                if actual != expected:
                    raise StoreError(
                        f"Callback operation {operation_id!r} was reused "
                        "with a different action."
                    )
                action = self._callback_from_row(existing)
                self.connection.execute("COMMIT")
                return action

            action = None
            for _ in range(5):
                token = secrets.token_urlsafe(6)
                try:
                    cursor = self.connection.execute(
                        """
                        INSERT INTO callback_actions(
                            operation_id, token, action_type, payload_json,
                            chat_id, message_thread_id, authorized_user_id,
                            one_time, state, expires_at, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                        """,
                        (
                            operation_id,
                            token,
                            action_type,
                            payload_json,
                            int(chat_id),
                            (
                                int(message_thread_id)
                                if message_thread_id is not None
                                else None
                            ),
                            int(authorized_user_id),
                            int(bool(one_time)),
                            timestamp + float(ttl_seconds),
                            timestamp,
                            timestamp,
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                row = self.connection.execute(
                    "SELECT * FROM callback_actions WHERE action_id = ?",
                    (int(cursor.lastrowid),),
                ).fetchone()
                action = self._callback_from_row(row)
                break
            if action is None:
                raise StoreError("Could not allocate a unique callback token.")
            self.connection.execute("COMMIT")
            return action
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def consume_callback_action(
        self,
        callback_data: str,
        chat_id: int,
        authorized_user_id: int,
        update_id: int,
        message_thread_id: Optional[int] = None,
        now: Optional[float] = None,
    ) -> CallbackAction:
        match = re.fullmatch(r"a:([A-Za-z0-9_-]{6,32})", callback_data)
        if not match:
            raise CallbackActionError(
                "invalid",
                "This button is invalid.",
            )
        token = match.group(1)
        timestamp = time.time() if now is None else float(now)

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM callback_actions WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                raise CallbackActionError(
                    "unknown",
                    "This button is no longer available.",
                )
            if (
                int(row["chat_id"]) != int(chat_id)
                or int(row["authorized_user_id"]) != int(authorized_user_id)
                or (
                    int(row["message_thread_id"])
                    if row["message_thread_id"] is not None
                    else None
                )
                != (
                    int(message_thread_id)
                    if message_thread_id is not None
                    else None
                )
            ):
                raise CallbackActionError(
                    "unauthorized",
                    "This button is not authorized here.",
                )
            if float(row["expires_at"]) <= timestamp:
                if row["state"] == "active":
                    self.connection.execute(
                        """
                        UPDATE callback_actions
                        SET state = 'expired', updated_at = ?
                        WHERE action_id = ? AND state = 'active'
                        """,
                        (timestamp, int(row["action_id"])),
                    )
                self.connection.execute("COMMIT")
                raise CallbackActionError(
                    "expired",
                    "This button has expired.",
                )
            if row["state"] == "consumed":
                if int(row["consumed_by_update_id"] or -1) == int(update_id):
                    action = self._callback_from_row(row)
                    self.connection.execute("COMMIT")
                    return action
                raise CallbackActionError(
                    "consumed",
                    "This button was already used.",
                )
            if row["state"] != "active":
                raise CallbackActionError(
                    str(row["state"]),
                    "This button is no longer available.",
                )

            if bool(row["one_time"]):
                self.connection.execute(
                    """
                    UPDATE callback_actions
                    SET state = 'consumed', consumed_at = ?,
                        consumed_by_update_id = ?, updated_at = ?
                    WHERE action_id = ? AND state = 'active'
                    """,
                    (
                        timestamp,
                        int(update_id),
                        timestamp,
                        int(row["action_id"]),
                    ),
                )
            action = self._callback_from_row(row)
            self.connection.execute("COMMIT")
            return action
        except CallbackActionError:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

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
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT method, params_json, route_json
                FROM outbox_messages
                WHERE message_id = ? AND state = 'leased' AND lease_owner = ?
                """,
                (int(message_id), worker_id),
            ).fetchone()
            if row is None:
                raise LeaseLostError(
                    f"Outbox lease for message {message_id} is no longer owned."
                )
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

            if row["route_json"] is not None:
                if row["method"] != "sendMessage" or not isinstance(result, dict):
                    raise StoreError(
                        "Only a successful sendMessage result can create a reply route."
                    )
                params = json.loads(row["params_json"])
                route = json.loads(row["route_json"])
                target_type = str(route.get("target_type", ""))
                target_id = str(route.get("target_id", ""))
                policy = str(route.get("policy", ""))
                ttl_seconds = float(route.get("ttl_seconds", 0))
                if (
                    not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", target_type)
                    or not target_id
                    or len(target_id) > 128
                    or policy != "reply"
                    or ttl_seconds <= 0
                    or ttl_seconds > 365 * 24 * 60 * 60
                ):
                    raise StoreError("Outbox reply route is invalid.")
                try:
                    telegram_message_id = int(result["message_id"])
                    chat_id = int(params["chat_id"])
                except (KeyError, TypeError, ValueError):
                    raise StoreError(
                        "Telegram sendMessage result cannot be routed."
                    ) from None
                result_chat = result.get("chat")
                if isinstance(result_chat, dict) and "id" in result_chat:
                    if int(result_chat["id"]) != chat_id:
                        raise StoreError(
                            "Telegram sendMessage result has an unexpected chat."
                        )
                thread_id = int(params.get("message_thread_id") or 0)
                self.connection.execute(
                    """
                    INSERT INTO telegram_message_routes(
                        source_outbox_message_id, chat_id, message_thread_id,
                        telegram_message_id, target_type, target_id, policy,
                        state, expires_at, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        int(message_id),
                        chat_id,
                        thread_id,
                        telegram_message_id,
                        target_type,
                        target_id,
                        policy,
                        timestamp + ttl_seconds,
                        timestamp,
                        timestamp,
                    ),
                )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _message_route_from_row(row: sqlite3.Row) -> MessageRoute:
        thread_id = int(row["message_thread_id"])
        return MessageRoute(
            route_id=int(row["route_id"]),
            chat_id=int(row["chat_id"]),
            message_thread_id=thread_id if thread_id != 0 else None,
            telegram_message_id=int(row["telegram_message_id"]),
            target_type=str(row["target_type"]),
            target_id=str(row["target_id"]),
            policy=str(row["policy"]),
            expires_at=float(row["expires_at"]),
        )

    def resolve_message_route(
        self,
        chat_id: int,
        telegram_message_id: int,
        message_thread_id: Optional[int] = None,
        now: Optional[float] = None,
    ) -> Optional[MessageRoute]:
        timestamp = time.time() if now is None else float(now)
        thread_id = int(message_thread_id) if message_thread_id is not None else 0
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT *
                FROM telegram_message_routes
                WHERE chat_id = ? AND message_thread_id = ?
                    AND telegram_message_id = ?
                """,
                (int(chat_id), thread_id, int(telegram_message_id)),
            ).fetchone()
            if row is None or row["state"] != "active":
                self.connection.execute("COMMIT")
                return None
            if float(row["expires_at"]) <= timestamp:
                self.connection.execute(
                    """
                    UPDATE telegram_message_routes
                    SET state = 'expired', updated_at = ?
                    WHERE route_id = ? AND state = 'active'
                    """,
                    (timestamp, int(row["route_id"])),
                )
                self.connection.execute("COMMIT")
                return None
            route = self._message_route_from_row(row)
            self.connection.execute("COMMIT")
            return route
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _surface_binding_from_row(row: sqlite3.Row) -> SurfaceBinding:
        thread_id = int(row["message_thread_id"])
        return SurfaceBinding(
            binding_id=int(row["binding_id"]),
            chat_id=int(row["chat_id"]),
            message_thread_id=thread_id if thread_id != 0 else None,
            surface_type=str(row["surface_type"]),
            display_name=str(row["display_name"]),
            target_type=str(row["target_type"]),
            target_id=str(row["target_id"]),
            state=str(row["state"]),
        )

    def ensure_surface_binding(
        self,
        chat_id: int,
        surface_type: str,
        display_name: str,
        target_type: str,
        target_id: str,
        message_thread_id: Optional[int] = None,
        now: Optional[float] = None,
    ) -> SurfaceBinding:
        if surface_type not in {"control", "project", "task"}:
            raise StoreError("Surface type is invalid.")
        if not display_name or len(display_name) > 128:
            raise StoreError("Surface display name is invalid.")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", target_type):
            raise StoreError("Surface target type is invalid.")
        if not target_id or len(target_id) > 128:
            raise StoreError("Surface target ID is invalid.")
        timestamp = time.time() if now is None else float(now)
        thread_id = int(message_thread_id) if message_thread_id is not None else 0
        expected = (
            surface_type,
            display_name,
            target_type,
            target_id,
            "active",
        )

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT *
                FROM surface_bindings
                WHERE chat_id = ? AND message_thread_id = ?
                """,
                (int(chat_id), thread_id),
            ).fetchone()
            if row is None:
                cursor = self.connection.execute(
                    """
                    INSERT INTO surface_bindings(
                        chat_id, message_thread_id, surface_type, display_name,
                        target_type, target_id, state, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        int(chat_id),
                        thread_id,
                        surface_type,
                        display_name,
                        target_type,
                        target_id,
                        timestamp,
                        timestamp,
                    ),
                )
                row = self.connection.execute(
                    "SELECT * FROM surface_bindings WHERE binding_id = ?",
                    (int(cursor.lastrowid),),
                ).fetchone()
            else:
                actual = (
                    str(row["surface_type"]),
                    str(row["display_name"]),
                    str(row["target_type"]),
                    str(row["target_id"]),
                    str(row["state"]),
                )
                if actual != expected:
                    raise StoreError(
                        "Telegram surface is already bound to a different target."
                    )
            binding = self._surface_binding_from_row(row)
            self.connection.execute("COMMIT")
            return binding
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def resolve_surface_binding(
        self,
        chat_id: int,
        message_thread_id: Optional[int] = None,
    ) -> Optional[SurfaceBinding]:
        thread_id = int(message_thread_id) if message_thread_id is not None else 0
        row = self.connection.execute(
            """
            SELECT *
            FROM surface_bindings
            WHERE chat_id = ? AND message_thread_id = ? AND state = 'active'
            """,
            (int(chat_id), thread_id),
        ).fetchone()
        return self._surface_binding_from_row(row) if row is not None else None

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
            ("callbacks", "callback_actions"),
            ("routes", "telegram_message_routes"),
            ("surfaces", "surface_bindings"),
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
