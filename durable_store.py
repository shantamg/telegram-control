#!/usr/bin/python3
"""SQLite persistence for Telegram Control's durable transport."""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


SCHEMA_VERSION = 12


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
    card: Optional[dict[str, Any]]
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


@dataclass(frozen=True)
class SurfaceCard:
    card_id: int
    binding_id: int
    card_type: str
    callback_action_id: int
    telegram_message_id: Optional[int]
    generation: int
    state: str


@dataclass(frozen=True)
class ManagedAgent:
    agent_id: str
    parent_agent_id: Optional[str]
    role: str
    slug: str
    hierarchical_name: str
    provider: str
    project_path: Optional[str]
    provider_session_id: Optional[str]
    surface_binding_id: Optional[int]
    lifecycle_state: str
    provider_config: dict[str, Any]


@dataclass(frozen=True)
class AgentMailboxJob:
    mailbox_id: int
    agent_id: str
    source_inbox_job_id: int
    input_text: str
    provider_session_id: Optional[str]
    attempts: int


@dataclass(frozen=True)
class RouterMailboxJob:
    mailbox_id: int
    source_inbox_job_id: int
    chat_id: int
    input_text: str
    provider_session_id: Optional[str]
    attempts: int


@dataclass(frozen=True)
class AgentConsole:
    agent_id: str
    tmux_session_name: str
    state: str


@dataclass(frozen=True)
class ManagedProject:
    project_id: str
    slug: str
    display_name: str
    provider: str
    project_path: str
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

MIGRATION_5 = (
    """
    ALTER TABLE outbox_messages
    ADD COLUMN card_json TEXT
    """,
    """
    CREATE TABLE surface_cards (
        card_id INTEGER PRIMARY KEY AUTOINCREMENT,
        binding_id INTEGER NOT NULL
            REFERENCES surface_bindings(binding_id) ON DELETE RESTRICT,
        card_type TEXT NOT NULL CHECK (card_type IN ('status')),
        callback_action_id INTEGER NOT NULL
            REFERENCES callback_actions(action_id) ON DELETE RESTRICT,
        telegram_message_id INTEGER,
        generation INTEGER NOT NULL DEFAULT 1 CHECK (generation >= 1),
        state TEXT NOT NULL
            CHECK (state IN ('pending', 'active', 'stale')),
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE(binding_id, card_type)
    )
    """,
    """
    CREATE INDEX surface_cards_state
    ON surface_cards(binding_id, card_type, state)
    """,
)

MIGRATION_6 = (
    """
    CREATE TABLE agents (
        agent_id TEXT PRIMARY KEY,
        parent_agent_id TEXT
            REFERENCES agents(agent_id) ON DELETE RESTRICT,
        role TEXT NOT NULL CHECK (role IN ('main', 'project', 'worker')),
        slug TEXT NOT NULL,
        hierarchical_name TEXT NOT NULL UNIQUE,
        provider TEXT NOT NULL CHECK (provider IN ('codex', 'claude')),
        project_path TEXT,
        provider_session_id TEXT,
        surface_binding_id INTEGER UNIQUE
            REFERENCES surface_bindings(binding_id) ON DELETE RESTRICT,
        lifecycle_state TEXT NOT NULL
            CHECK (lifecycle_state IN ('registered', 'running', 'stopped', 'failed')),
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE(parent_agent_id, slug)
    )
    """,
    """
    CREATE INDEX agents_lifecycle
    ON agents(role, lifecycle_state, hierarchical_name)
    """,
)

MIGRATION_7 = (
    """
    ALTER TABLE agents
    ADD COLUMN provider_config_json TEXT NOT NULL DEFAULT '{}'
    """,
    """
    CREATE TABLE agent_mailbox (
        mailbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL
            REFERENCES agents(agent_id) ON DELETE RESTRICT,
        source_inbox_job_id INTEGER NOT NULL UNIQUE
            REFERENCES inbox_jobs(job_id) ON DELETE RESTRICT,
        input_text TEXT NOT NULL,
        provider_session_id TEXT,
        state TEXT NOT NULL
            CHECK (state IN ('queued', 'leased', 'succeeded', 'dead')),
        attempts INTEGER NOT NULL DEFAULT 0,
        available_at REAL NOT NULL,
        lease_owner TEXT,
        lease_expires_at REAL,
        last_error TEXT,
        response_text TEXT,
        usage_json TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX agent_mailbox_ready
    ON agent_mailbox(state, available_at, mailbox_id)
    """,
    """
    CREATE INDEX agent_mailbox_agent
    ON agent_mailbox(agent_id, state, mailbox_id)
    """,
)

MIGRATION_8 = (
    """
    CREATE TABLE agent_consoles (
        agent_id TEXT PRIMARY KEY
            REFERENCES agents(agent_id) ON DELETE RESTRICT,
        tmux_session_name TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL
            CHECK (state IN ('starting', 'running', 'stopped')),
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX agent_consoles_state
    ON agent_consoles(state, tmux_session_name)
    """,
)

MIGRATION_9 = (
    """
    CREATE TABLE managed_projects (
        project_id TEXT PRIMARY KEY,
        slug TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        provider TEXT NOT NULL CHECK (provider IN ('codex', 'claude')),
        project_path TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK (state IN ('active', 'revoked')),
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX managed_projects_state
    ON managed_projects(state, slug)
    """,
)

MIGRATION_10 = (
    """
    CREATE TABLE router_mailbox (
        mailbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_inbox_job_id INTEGER NOT NULL UNIQUE
            REFERENCES inbox_jobs(job_id) ON DELETE RESTRICT,
        chat_id INTEGER NOT NULL,
        message_thread_id INTEGER NOT NULL DEFAULT 0,
        input_text TEXT NOT NULL,
        provider_session_id TEXT,
        state TEXT NOT NULL
            CHECK (state IN ('queued', 'leased', 'succeeded', 'dead')),
        attempts INTEGER NOT NULL DEFAULT 0,
        available_at REAL NOT NULL,
        lease_owner TEXT,
        lease_expires_at REAL,
        last_error TEXT,
        raw_output TEXT,
        tool_name TEXT,
        arguments_json TEXT,
        preview_text TEXT,
        usage_json TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX router_mailbox_ready
    ON router_mailbox(state, available_at, mailbox_id)
    """,
)

MIGRATION_11 = (
    """
    ALTER TABLE router_mailbox
    ADD COLUMN authorized_user_id INTEGER
    """,
)

MIGRATION_12 = (
    """
    CREATE TABLE project_aliases (
        alias_key TEXT PRIMARY KEY,
        alias TEXT NOT NULL,
        project_id TEXT NOT NULL
            REFERENCES managed_projects(project_id) ON DELETE RESTRICT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX project_aliases_project
    ON project_aliases(project_id, alias_key)
    """,
)


def normalize_project_alias(alias: str) -> str:
    value = " ".join(alias.strip().casefold().split())
    if (
        len(value) < 2
        or len(value) > 64
        or not re.fullmatch(r"[a-z0-9]+(?:[ -][a-z0-9]+)*", value)
    ):
        raise StoreError(
            "Project alias must use 2 to 64 letters, digits, spaces, or hyphens."
        )
    return value


def validate_provider_config(
    provider: str,
    config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if provider not in {"codex", "claude"}:
        raise StoreError("Agent provider is invalid.")
    value = dict(config or {})
    allowed = (
        {"model", "effort", "sandbox"}
        if provider == "codex"
        else {"model", "effort", "permission_mode"}
    )
    if not set(value).issubset(allowed):
        raise StoreError("Agent provider configuration has unknown fields.")
    model = value.get("model")
    if model is not None and (
        not isinstance(model, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}", model)
    ):
        raise StoreError("Agent model is invalid.")
    effort = value.get("effort")
    efforts = (
        {"low", "medium", "high", "xhigh", "max", "ultra"}
        if provider == "codex"
        else {"low", "medium", "high", "xhigh", "max"}
    )
    if effort is not None and (
        not isinstance(effort, str) or effort not in efforts
    ):
        raise StoreError(f"Agent effort is invalid for {provider}.")
    if provider == "codex":
        sandbox = value.get("sandbox")
        if sandbox is not None and (
            not isinstance(sandbox, str)
            or sandbox
            not in {"read-only", "workspace-write", "danger-full-access"}
        ):
            raise StoreError("Codex sandbox is invalid.")
    else:
        permission_mode = value.get("permission_mode")
        if permission_mode is not None and (
            not isinstance(permission_mode, str)
            or permission_mode
            not in {
                "acceptEdits",
                "auto",
                "bypassPermissions",
                "dontAsk",
                "plan",
            }
        ):
            raise StoreError("Claude permission mode is invalid.")
    return value


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
            if current < 5:
                for statement in MIGRATION_5:
                    self.connection.execute(statement)
                current = 5
                self.connection.execute("PRAGMA user_version = 5")
            if current < 6:
                for statement in MIGRATION_6:
                    self.connection.execute(statement)
                current = 6
                self.connection.execute("PRAGMA user_version = 6")
            if current < 7:
                for statement in MIGRATION_7:
                    self.connection.execute(statement)
                current = 7
                self.connection.execute("PRAGMA user_version = 7")
            if current < 8:
                for statement in MIGRATION_8:
                    self.connection.execute(statement)
                current = 8
                self.connection.execute("PRAGMA user_version = 8")
            if current < 9:
                for statement in MIGRATION_9:
                    self.connection.execute(statement)
                current = 9
                self.connection.execute("PRAGMA user_version = 9")
            if current < 10:
                for statement in MIGRATION_10:
                    self.connection.execute(statement)
                current = 10
                self.connection.execute("PRAGMA user_version = 10")
            if current < 11:
                for statement in MIGRATION_11:
                    self.connection.execute(statement)
                current = 11
                self.connection.execute("PRAGMA user_version = 11")
            if current < 12:
                for statement in MIGRATION_12:
                    self.connection.execute(statement)
                current = 12
                self.connection.execute("PRAGMA user_version = 12")
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
        card: Optional[dict[str, Any]] = None,
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
        card_json = (
            json.dumps(card, separators=(",", ":"), sort_keys=True)
            if card is not None
            else None
        )
        self.connection.execute(
            """
            INSERT INTO outbox_messages(
                operation_id, method, params_json, state, attempts,
                available_at, created_at, updated_at, route_json, card_json
            )
            VALUES (?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?)
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
                card_json,
            ),
        )
        row = self.connection.execute(
            """
            SELECT message_id, method, params_json, route_json, card_json
            FROM outbox_messages
            WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        if (
            row["method"] != method
            or row["params_json"] != params_json
            or row["route_json"] != route_json
            or row["card_json"] != card_json
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
            card=(
                json.loads(claimed["card_json"])
                if claimed["card_json"] is not None
                else None
            ),
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
                SELECT method, params_json, route_json, card_json
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
            if row["card_json"] is not None:
                card_spec = json.loads(row["card_json"])
                if card_spec.get("kind") == "router_turn":
                    try:
                        mode = str(card_spec["mode"])
                    except (KeyError, TypeError, ValueError):
                        raise StoreError(
                            "Outbox router-turn metadata is invalid."
                        ) from None
                    if mode == "receipt":
                        if row["method"] != "sendMessage" or not isinstance(
                            result, dict
                        ):
                            raise StoreError(
                                "Only sendMessage can deliver a router receipt."
                            )
                        try:
                            int(result["message_id"])
                            source_inbox_job_id = int(
                                card_spec["source_inbox_job_id"]
                            )
                        except (KeyError, TypeError, ValueError):
                            raise StoreError(
                                "Telegram result cannot identify its router receipt."
                            ) from None
                        mailbox = self.connection.execute(
                            """
                            SELECT mailbox_id, state, input_text, preview_text
                            FROM router_mailbox
                            WHERE source_inbox_job_id = ?
                            """,
                            (source_inbox_job_id,),
                        ).fetchone()
                        if (
                            mailbox is not None
                            and str(mailbox["state"]) in {"succeeded", "dead"}
                            and mailbox["preview_text"] is not None
                        ):
                            self._enqueue_router_final_edit(
                                int(mailbox["mailbox_id"]),
                                str(mailbox["preview_text"]),
                                timestamp,
                            )
                        elif (
                            mailbox is not None
                            and card_spec.get("input_kind") == "voice"
                        ):
                            stage = (
                                "working"
                                if str(mailbox["state"]) == "leased"
                                else "sending"
                            )
                            self.enqueue_router_voice_status(
                                source_inbox_job_id,
                                stage,
                                str(mailbox["input_text"]),
                                now=timestamp,
                            )
                    elif mode == "final_edit":
                        try:
                            int(card_spec["mailbox_id"])
                        except (KeyError, TypeError, ValueError):
                            raise StoreError(
                                "Outbox router-turn edit metadata is invalid."
                            ) from None
                        if row["method"] != "editMessageText":
                            raise StoreError(
                                "Only editMessageText can finish a router turn card."
                            )
                    elif mode == "status_edit":
                        if row["method"] != "editMessageText":
                            raise StoreError(
                                "Only editMessageText can update router status."
                            )
                    else:
                        raise StoreError("Outbox router-turn mode is invalid.")
                    self.connection.execute("COMMIT")
                    return
                if card_spec.get("kind") == "agent_turn":
                    try:
                        mode = str(card_spec["mode"])
                    except (KeyError, TypeError, ValueError):
                        raise StoreError(
                            "Outbox agent-turn metadata is invalid."
                        ) from None
                    if mode == "receipt":
                        if row["method"] != "sendMessage" or not isinstance(
                            result, dict
                        ):
                            raise StoreError(
                                "Only sendMessage can deliver an agent receipt."
                            )
                        try:
                            telegram_message_id = int(result["message_id"])
                        except (KeyError, TypeError, ValueError):
                            raise StoreError(
                                "Telegram result cannot identify its agent receipt."
                            ) from None
                        if "source_inbox_job_id" in card_spec:
                            mailbox = self.connection.execute(
                                """
                            SELECT mailbox_id, state, input_text, response_text
                                FROM agent_mailbox
                                WHERE source_inbox_job_id = ?
                                """,
                                (int(card_spec["source_inbox_job_id"]),),
                            ).fetchone()
                        else:
                            mailbox = self.connection.execute(
                                """
                                SELECT mailbox_id, state, input_text, response_text
                                FROM agent_mailbox
                                WHERE mailbox_id = ?
                                """,
                                (int(card_spec["mailbox_id"]),),
                            ).fetchone()
                        if (
                            mailbox is not None
                            and str(mailbox["state"]) == "succeeded"
                            and mailbox["response_text"] is not None
                            and len(str(mailbox["response_text"])) <= 3800
                        ):
                            params = json.loads(row["params_json"])
                            mailbox_id = int(mailbox["mailbox_id"])
                            self.enqueue_api_call(
                                operation_id=(
                                    f"agent-mailbox:{mailbox_id}:final-edit"
                                ),
                                method="editMessageText",
                                params={
                                    "chat_id": int(params["chat_id"]),
                                    "message_id": telegram_message_id,
                                    "text": str(mailbox["response_text"]),
                                },
                                card={
                                    "kind": "agent_turn",
                                    "mailbox_id": mailbox_id,
                                    "mode": "final_edit",
                                },
                                now=timestamp,
                            )
                        elif (
                            mailbox is not None
                            and card_spec.get("input_kind") == "voice"
                        ):
                            stage = (
                                "working"
                                if str(mailbox["state"]) == "leased"
                                else "sending"
                            )
                            self.enqueue_agent_voice_status(
                                int(card_spec["source_inbox_job_id"]),
                                stage,
                                str(mailbox["input_text"]),
                                now=timestamp,
                            )
                    elif mode == "final_edit":
                        try:
                            int(card_spec["mailbox_id"])
                        except (KeyError, TypeError, ValueError):
                            raise StoreError(
                                "Outbox agent-turn edit metadata is invalid."
                            ) from None
                        if row["method"] != "editMessageText":
                            raise StoreError(
                                "Only editMessageText can finish an agent turn card."
                            )
                    elif mode == "status_edit":
                        if row["method"] != "editMessageText":
                            raise StoreError(
                                "Only editMessageText can update agent turn status."
                            )
                    else:
                        raise StoreError("Outbox agent-turn mode is invalid.")
                    self.connection.execute("COMMIT")
                    return
                try:
                    card_id = int(card_spec["card_id"])
                    mode = str(card_spec["mode"])
                except (KeyError, TypeError, ValueError):
                    raise StoreError("Outbox surface card metadata is invalid.") from None
                if mode == "activate":
                    if row["method"] != "sendMessage" or not isinstance(result, dict):
                        raise StoreError(
                            "Only sendMessage can activate a surface card."
                        )
                    try:
                        telegram_message_id = int(result["message_id"])
                    except (KeyError, TypeError, ValueError):
                        raise StoreError(
                            "Telegram result cannot activate its surface card."
                        ) from None
                    card_cursor = self.connection.execute(
                        """
                        UPDATE surface_cards
                        SET telegram_message_id = ?, state = 'active', updated_at = ?
                        WHERE card_id = ? AND state = 'pending'
                        """,
                        (telegram_message_id, timestamp, card_id),
                    )
                    if card_cursor.rowcount != 1:
                        raise StoreError(
                            "Surface card is no longer pending activation."
                        )
                elif mode == "edit":
                    if row["method"] != "editMessageText":
                        raise StoreError(
                            "Only editMessageText can update a surface card."
                        )
                    active = self.connection.execute(
                        """
                        SELECT 1 FROM surface_cards
                        WHERE card_id = ? AND state = 'active'
                        """,
                        (card_id,),
                    ).fetchone()
                    if active is None:
                        raise StoreError("Surface card is no longer active.")
                else:
                    raise StoreError("Outbox surface card mode is invalid.")
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

    def resolve_named_surface(
        self,
        chat_id: int,
        display_name: str,
        surface_type: str = "project",
    ) -> Optional[SurfaceBinding]:
        row = self.connection.execute(
            """
            SELECT *
            FROM surface_bindings
            WHERE chat_id = ? AND display_name = ? AND surface_type = ?
                AND state = 'active'
            ORDER BY binding_id
            LIMIT 1
            """,
            (int(chat_id), display_name, surface_type),
        ).fetchone()
        return self._surface_binding_from_row(row) if row is not None else None

    def list_topic_surfaces(self, chat_id: int) -> list[SurfaceBinding]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM surface_bindings
            WHERE chat_id = ? AND message_thread_id != 0 AND state = 'active'
            ORDER BY binding_id
            """,
            (int(chat_id),),
        ).fetchall()
        return [self._surface_binding_from_row(row) for row in rows]

    def resolve_surface_binding_by_id(
        self,
        binding_id: int,
    ) -> Optional[SurfaceBinding]:
        row = self.connection.execute(
            """
            SELECT *
            FROM surface_bindings
            WHERE binding_id = ? AND state = 'active'
            """,
            (int(binding_id),),
        ).fetchone()
        return self._surface_binding_from_row(row) if row is not None else None

    def rename_surface_binding(
        self,
        binding_id: int,
        expected_chat_id: int,
        expected_message_thread_id: int,
        expected_display_name: str,
        new_display_name: str,
        now: Optional[float] = None,
    ) -> SurfaceBinding:
        name = new_display_name.strip()
        if (
            not name
            or len(name) > 128
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in name
            )
        ):
            raise StoreError(
                "Topic name must contain 1 to 128 printable characters."
            )
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                """
                UPDATE surface_bindings
                SET display_name = ?, updated_at = ?
                WHERE binding_id = ? AND chat_id = ?
                    AND message_thread_id = ? AND display_name = ?
                    AND state = 'active' AND message_thread_id != 0
                """,
                (
                    name,
                    timestamp,
                    int(binding_id),
                    int(expected_chat_id),
                    int(expected_message_thread_id),
                    expected_display_name,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError(
                    "Managed topic changed before its rename could be recorded."
                )
            self.connection.execute(
                """
                INSERT INTO events(
                    kind, subject_type, subject_id, details_json, created_at
                )
                VALUES ('surface_renamed', 'surface', ?, ?, ?)
                """,
                (
                    str(int(binding_id)),
                    json.dumps(
                        {
                            "chat_id": int(expected_chat_id),
                            "message_thread_id": int(
                                expected_message_thread_id
                            ),
                            "old_name": expected_display_name,
                            "new_name": name,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    timestamp,
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM surface_bindings WHERE binding_id = ?",
                (int(binding_id),),
            ).fetchone()
            binding = self._surface_binding_from_row(row)
            self.connection.execute("COMMIT")
            return binding
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _managed_agent_from_row(row: sqlite3.Row) -> ManagedAgent:
        return ManagedAgent(
            agent_id=str(row["agent_id"]),
            parent_agent_id=(
                str(row["parent_agent_id"])
                if row["parent_agent_id"] is not None
                else None
            ),
            role=str(row["role"]),
            slug=str(row["slug"]),
            hierarchical_name=str(row["hierarchical_name"]),
            provider=str(row["provider"]),
            project_path=(
                str(row["project_path"])
                if row["project_path"] is not None
                else None
            ),
            provider_session_id=(
                str(row["provider_session_id"])
                if row["provider_session_id"] is not None
                else None
            ),
            surface_binding_id=(
                int(row["surface_binding_id"])
                if row["surface_binding_id"] is not None
                else None
            ),
            lifecycle_state=str(row["lifecycle_state"]),
            provider_config=json.loads(row["provider_config_json"]),
        )

    def resolve_agent(self, agent_id: str) -> Optional[ManagedAgent]:
        row = self.connection.execute(
            "SELECT * FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        return self._managed_agent_from_row(row) if row is not None else None

    def resolve_agent_by_name(self, hierarchical_name: str) -> Optional[ManagedAgent]:
        row = self.connection.execute(
            "SELECT * FROM agents WHERE hierarchical_name = ?",
            (hierarchical_name,),
        ).fetchone()
        return self._managed_agent_from_row(row) if row is not None else None

    @staticmethod
    def _managed_project_from_row(row: sqlite3.Row) -> ManagedProject:
        return ManagedProject(
            project_id=str(row["project_id"]),
            slug=str(row["slug"]),
            display_name=str(row["display_name"]),
            provider=str(row["provider"]),
            project_path=str(row["project_path"]),
            state=str(row["state"]),
        )

    def enroll_project(
        self,
        slug: str,
        display_name: str,
        provider: str,
        project_path: str,
        now: Optional[float] = None,
    ) -> tuple[ManagedProject, bool]:
        if (
            len(slug) > 48
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)
            or "--" in slug
            or slug == "root"
        ):
            raise StoreError(
                "Project slug must use lowercase letters, digits, and single hyphens."
            )
        name = display_name.strip()
        if not name or len(name) > 128:
            raise StoreError("Project display name must contain 1 to 128 characters.")
        if provider not in {"codex", "claude"}:
            raise StoreError("Project provider is invalid.")
        if not project_path or not Path(project_path).is_absolute():
            raise StoreError("Managed project path must be absolute.")
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                """
                SELECT * FROM managed_projects
                WHERE slug = ? OR project_path = ?
                """,
                (slug, project_path),
            ).fetchone()
            if existing is not None:
                project = self._managed_project_from_row(existing)
                expected = (slug, name, provider, project_path, "active")
                actual = (
                    project.slug,
                    project.display_name,
                    project.provider,
                    project.project_path,
                    project.state,
                )
                if actual != expected:
                    raise StoreError(
                        "Project slug or path is already enrolled differently."
                    )
                self.connection.execute("COMMIT")
                return project, False
            alias_collision = self.connection.execute(
                "SELECT project_id FROM project_aliases WHERE alias_key = ?",
                (slug,),
            ).fetchone()
            if alias_collision is not None:
                raise StoreError("Project slug is already used as a project alias.")
            project_id = f"project_{secrets.token_urlsafe(12)}"
            self.connection.execute(
                """
                INSERT INTO managed_projects(
                    project_id, slug, display_name, provider, project_path,
                    state, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    project_id,
                    slug,
                    name,
                    provider,
                    project_path,
                    timestamp,
                    timestamp,
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM managed_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            self.connection.execute("COMMIT")
            return self._managed_project_from_row(row), True
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def resolve_project(self, slug: str) -> Optional[ManagedProject]:
        try:
            alias_key = normalize_project_alias(slug)
        except StoreError:
            alias_key = ""
        row = self.connection.execute(
            """
            SELECT p.*
            FROM managed_projects AS p
            LEFT JOIN project_aliases AS a ON a.project_id = p.project_id
            WHERE p.state = 'active'
                AND (p.slug = ? OR p.slug = ? OR a.alias_key = ?)
            ORDER BY CASE WHEN p.slug = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (slug, alias_key, alias_key, slug),
        ).fetchone()
        return self._managed_project_from_row(row) if row is not None else None

    def list_projects(self) -> list[ManagedProject]:
        rows = self.connection.execute(
            """
            SELECT * FROM managed_projects
            WHERE state = 'active'
            ORDER BY slug
            """
        ).fetchall()
        return [self._managed_project_from_row(row) for row in rows]

    def project_alias_map(self) -> dict[str, list[str]]:
        rows = self.connection.execute(
            """
            SELECT p.slug, a.alias
            FROM managed_projects AS p
            JOIN project_aliases AS a ON a.project_id = p.project_id
            WHERE p.state = 'active'
            ORDER BY p.slug, a.alias_key
            """
        ).fetchall()
        result: dict[str, list[str]] = {}
        for row in rows:
            result.setdefault(str(row["slug"]), []).append(str(row["alias"]))
        return result

    def project_alias_resolution(self) -> dict[str, str]:
        rows = self.connection.execute(
            """
            SELECT a.alias_key, p.slug
            FROM project_aliases AS a
            JOIN managed_projects AS p ON p.project_id = a.project_id
            WHERE p.state = 'active'
            """
        ).fetchall()
        return {
            str(row["alias_key"]): str(row["slug"])
            for row in rows
        }

    def add_project_alias(
        self,
        project_slug: str,
        alias: str,
        now: Optional[float] = None,
    ) -> bool:
        project = self.resolve_project(project_slug)
        if project is None:
            raise StoreError("Project alias target is not enrolled.")
        alias_text = " ".join(alias.strip().split())
        alias_key = normalize_project_alias(alias_text)
        if alias_key == project.slug:
            raise StoreError("That is already the project's canonical slug.")
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            slug_collision = self.connection.execute(
                """
                SELECT project_id FROM managed_projects
                WHERE state = 'active' AND slug = ?
                """,
                (alias_key,),
            ).fetchone()
            if slug_collision is not None:
                raise StoreError("That alias is already a canonical project slug.")
            existing = self.connection.execute(
                "SELECT project_id FROM project_aliases WHERE alias_key = ?",
                (alias_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["project_id"]) != project.project_id:
                    raise StoreError("That alias already belongs to another project.")
                self.connection.execute("COMMIT")
                return False
            self.connection.execute(
                """
                INSERT INTO project_aliases(
                    alias_key, alias, project_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    alias_key,
                    alias_text,
                    project.project_id,
                    timestamp,
                    timestamp,
                ),
            )
            self.connection.execute("COMMIT")
            return True
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def remove_project_alias(
        self,
        alias: str,
    ) -> Optional[ManagedProject]:
        alias_key = normalize_project_alias(alias)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT p.*
                FROM project_aliases AS a
                JOIN managed_projects AS p ON p.project_id = a.project_id
                WHERE a.alias_key = ? AND p.state = 'active'
                """,
                (alias_key,),
            ).fetchone()
            if row is None:
                self.connection.execute("COMMIT")
                return None
            self.connection.execute(
                "DELETE FROM project_aliases WHERE alias_key = ?",
                (alias_key,),
            )
            self.connection.execute("COMMIT")
            return self._managed_project_from_row(row)
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def list_project_agent_states(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT p.slug AS project_slug,
                COALESCE(a.lifecycle_state, 'not_created') AS state,
                a.provider_session_id
            FROM managed_projects AS p
            LEFT JOIN agents AS a
                ON a.role = 'project'
                AND a.slug = p.slug
                AND a.project_path = p.project_path
            WHERE p.state = 'active'
            ORDER BY p.slug
            """
        ).fetchall()
        return [
            {
                "project_slug": str(row["project_slug"]),
                "state": str(row["state"]),
                "session": row["provider_session_id"] is not None,
            }
            for row in rows
        ]

    def resolve_project_agent(self, project_slug: str) -> Optional[ManagedAgent]:
        row = self.connection.execute(
            """
            SELECT a.*
            FROM agents AS a
            JOIN managed_projects AS p
                ON p.slug = a.slug
                AND p.project_path = a.project_path
            WHERE p.slug = ? AND p.state = 'active' AND a.role = 'project'
            ORDER BY a.created_at
            LIMIT 1
            """,
            (project_slug,),
        ).fetchone()
        return self._managed_agent_from_row(row) if row is not None else None

    def _ensure_main_agent(self, timestamp: float) -> ManagedAgent:
        row = self.connection.execute(
            "SELECT * FROM agents WHERE hierarchical_name = 'tc--root'"
        ).fetchone()
        if row is None:
            agent_id = f"agent_{secrets.token_urlsafe(12)}"
            self.connection.execute(
                """
                INSERT INTO agents(
                    agent_id, parent_agent_id, role, slug,
                    hierarchical_name, provider, project_path,
                    provider_session_id, surface_binding_id,
                    lifecycle_state, created_at, updated_at
                )
                VALUES (?, NULL, 'main', 'root', 'tc--root', 'codex',
                    NULL, NULL, NULL, 'registered', ?, ?)
                """,
                (agent_id, timestamp, timestamp),
            )
            row = self.connection.execute(
                "SELECT * FROM agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        return self._managed_agent_from_row(row)

    def resolve_main_agent(self) -> Optional[ManagedAgent]:
        row = self.connection.execute(
            "SELECT * FROM agents WHERE hierarchical_name = 'tc--root'"
        ).fetchone()
        return self._managed_agent_from_row(row) if row is not None else None

    def router_session_metrics(
        self,
        provider_session_id: Optional[str] = None,
    ) -> dict[str, Any]:
        session_id = provider_session_id
        if session_id is None:
            main_agent = self.resolve_main_agent()
            session_id = (
                main_agent.provider_session_id
                if main_agent is not None
                else None
            )
        if session_id is None:
            return {
                "provider_session_id": None,
                "completed_turns": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
            }
        rows = self.connection.execute(
            """
            SELECT usage_json
            FROM router_mailbox
            WHERE state = 'succeeded' AND provider_session_id = ?
            ORDER BY mailbox_id
            """,
            (session_id,),
        ).fetchall()
        usage: dict[str, Any] = {}
        if rows and rows[-1]["usage_json"] is not None:
            candidate = json.loads(rows[-1]["usage_json"])
            if isinstance(candidate, dict):
                usage = candidate
        return {
            "provider_session_id": session_id,
            "completed_turns": len(rows),
            "input_tokens": int(usage.get("input_tokens", 0)),
            "cached_input_tokens": int(usage.get("cached_input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
        }

    def rotate_main_router_session(
        self,
        mailbox_id: int,
        worker_id: str,
        reason: str,
        now: Optional[float] = None,
    ) -> str:
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT m.provider_session_id, a.agent_id
                FROM router_mailbox AS m
                JOIN agents AS a ON a.hierarchical_name = 'tc--root'
                WHERE m.mailbox_id = ? AND m.state = 'leased'
                    AND m.lease_owner = ?
                    AND m.provider_session_id IS NOT NULL
                    AND a.provider_session_id = m.provider_session_id
                """,
                (int(mailbox_id), worker_id),
            ).fetchone()
            if row is None:
                raise LeaseLostError(
                    "Main-router session is no longer eligible for rotation."
                )
            old_session_id = str(row["provider_session_id"])
            metrics = self.router_session_metrics(old_session_id)
            self.connection.execute(
                """
                UPDATE router_mailbox
                SET provider_session_id = NULL, updated_at = ?
                WHERE mailbox_id = ?
                """,
                (timestamp, int(mailbox_id)),
            )
            self.connection.execute(
                """
                UPDATE agents
                SET provider_session_id = NULL, updated_at = ?
                WHERE agent_id = ?
                """,
                (timestamp, str(row["agent_id"])),
            )
            self.connection.execute(
                """
                INSERT INTO events(
                    kind, subject_type, subject_id, details_json, created_at
                )
                VALUES ('router_session_rotated', 'agent', ?, ?, ?)
                """,
                (
                    str(row["agent_id"]),
                    json.dumps(
                        {
                            "old_provider_session_id": old_session_id,
                            "reason": reason,
                            "completed_turns": metrics["completed_turns"],
                            "input_tokens": metrics["input_tokens"],
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    timestamp,
                ),
            )
            self.connection.execute("COMMIT")
            return old_session_id
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def router_rotation_count(self) -> int:
        return int(
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM events
                WHERE kind = 'router_session_rotated'
                """
            ).fetchone()[0]
        )

    def enqueue_router_message_with_receipt(
        self,
        source_inbox_job_id: int,
        input_text: str,
        chat_id: int,
        message_thread_id: Optional[int],
        authorized_user_id: int,
        receipt_text: str,
        receipt_parse_mode: Optional[str] = None,
        now: Optional[float] = None,
    ) -> int:
        text = input_text.strip()
        if not text or len(text) > 8000:
            raise StoreError("Router input must contain 1 to 8000 characters.")
        if receipt_parse_mode not in {None, "HTML"}:
            raise StoreError("Router receipt parse mode is invalid.")
        timestamp = time.time() if now is None else float(now)
        thread_id = int(message_thread_id) if message_thread_id is not None else 0
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            binding = self.connection.execute(
                """
                SELECT 1
                FROM surface_bindings
                WHERE chat_id = ? AND message_thread_id = ?
                    AND surface_type = 'control'
                    AND target_type = 'controller' AND target_id = 'control'
                    AND state = 'active'
                """,
                (int(chat_id), thread_id),
            ).fetchone()
            if binding is None:
                raise StoreError("Main router surface is no longer valid.")
            main_agent = self._ensure_main_agent(timestamp)
            self.connection.execute(
                """
                INSERT INTO router_mailbox(
                    source_inbox_job_id, chat_id, message_thread_id,
                    authorized_user_id, input_text, provider_session_id,
                    state, attempts,
                    available_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?)
                ON CONFLICT(source_inbox_job_id) DO NOTHING
                """,
                (
                    int(source_inbox_job_id),
                    int(chat_id),
                    thread_id,
                    int(authorized_user_id),
                    text,
                    main_agent.provider_session_id,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            row = self.connection.execute(
                """
                SELECT mailbox_id, chat_id, message_thread_id,
                    authorized_user_id, input_text
                FROM router_mailbox
                WHERE source_inbox_job_id = ?
                """,
                (int(source_inbox_job_id),),
            ).fetchone()
            if row is None:
                raise StoreError("Could not enqueue the main-router message.")
            expected = (
                int(chat_id),
                thread_id,
                int(authorized_user_id),
                text,
            )
            actual = (
                int(row["chat_id"]),
                int(row["message_thread_id"]),
                int(row["authorized_user_id"]),
                str(row["input_text"]),
            )
            if actual != expected:
                raise StoreError("Inbox job was reused for a different router message.")
            params: dict[str, Any] = {
                "chat_id": int(chat_id),
                "message_thread_id": (
                    int(message_thread_id)
                    if message_thread_id is not None
                    else None
                ),
                "text": receipt_text,
            }
            if receipt_parse_mode is not None:
                params["parse_mode"] = receipt_parse_mode
            self.enqueue_api_call(
                operation_id=f"router-input:{int(source_inbox_job_id)}:receipt",
                method="sendMessage",
                params=params,
                route={
                    "target_type": "controller",
                    "target_id": "control",
                    "policy": "reply",
                    "ttl_seconds": 30 * 24 * 60 * 60,
                },
                card={
                    "kind": "router_turn",
                    "source_inbox_job_id": int(source_inbox_job_id),
                    "mode": "receipt",
                },
                now=timestamp,
            )
            self.connection.execute("COMMIT")
            return int(row["mailbox_id"])
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def enqueue_router_voice_receipt(
        self,
        source_inbox_job_id: int,
        chat_id: int,
        message_thread_id: Optional[int],
        authorized_user_id: int,
        now: Optional[float] = None,
    ) -> int:
        timestamp = time.time() if now is None else float(now)
        thread_id = int(message_thread_id) if message_thread_id is not None else 0
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            binding = self.connection.execute(
                """
                SELECT 1 FROM surface_bindings
                WHERE chat_id = ? AND message_thread_id = ?
                    AND surface_type = 'control'
                    AND target_type = 'controller' AND target_id = 'control'
                    AND state = 'active'
                """,
                (int(chat_id), thread_id),
            ).fetchone()
            if binding is None:
                raise StoreError("Main router surface is no longer valid.")
            self._ensure_main_agent(timestamp)
            message_id = self.enqueue_api_call(
                operation_id=f"router-input:{int(source_inbox_job_id)}:receipt",
                method="sendMessage",
                params={
                    "chat_id": int(chat_id),
                    "message_thread_id": (
                        int(message_thread_id)
                        if message_thread_id is not None
                        else None
                    ),
                    "text": "🎙️ <b>Transcribing…</b>",
                    "parse_mode": "HTML",
                },
                route={
                    "target_type": "controller",
                    "target_id": "control",
                    "policy": "reply",
                    "ttl_seconds": 30 * 24 * 60 * 60,
                },
                card={
                    "kind": "router_turn",
                    "source_inbox_job_id": int(source_inbox_job_id),
                    "input_kind": "voice",
                    "authorized_user_id": int(authorized_user_id),
                    "mode": "receipt",
                },
                now=timestamp,
            )
            self.connection.execute("COMMIT")
            return message_id
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    @staticmethod
    def router_voice_status_text(stage: str, input_text: str) -> str:
        transcript = input_text.strip()
        if len(transcript) > 3400:
            transcript = transcript[:3397].rstrip() + "…"
        transcript = html.escape(transcript)
        if stage == "sending":
            return f"📤 <b>Sending</b>\n<blockquote>{transcript}</blockquote>"
        if stage == "working":
            return (
                "🧭 <b>Routing…</b>\n"
                f"<blockquote>{transcript}</blockquote>"
            )
        raise StoreError("Router voice status stage is invalid.")

    def enqueue_router_voice_status(
        self,
        source_inbox_job_id: int,
        stage: str,
        input_text: str,
        now: Optional[float] = None,
    ) -> Optional[int]:
        timestamp = time.time() if now is None else float(now)
        receipt = self.connection.execute(
            """
            SELECT params_json, card_json, telegram_result_json
            FROM outbox_messages
            WHERE operation_id = ? AND state = 'sent'
            """,
            (f"router-input:{int(source_inbox_job_id)}:receipt",),
        ).fetchone()
        if receipt is None or receipt["telegram_result_json"] is None:
            return None
        card = json.loads(receipt["card_json"])
        if card.get("input_kind") != "voice":
            return None
        result = json.loads(receipt["telegram_result_json"])
        params = json.loads(receipt["params_json"])
        try:
            message_id = int(result["message_id"])
            chat_id = int(params["chat_id"])
        except (KeyError, TypeError, ValueError):
            raise StoreError("Stored Telegram router voice receipt is invalid.") from None
        return self.enqueue_api_call(
            operation_id=(
                f"router-input:{int(source_inbox_job_id)}:status:{stage}"
            ),
            method="editMessageText",
            params={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": self.router_voice_status_text(stage, input_text),
                "parse_mode": "HTML",
            },
            card={
                "kind": "router_turn",
                "source_inbox_job_id": int(source_inbox_job_id),
                "mode": "status_edit",
            },
            now=timestamp,
        )

    def enqueue_router_voice_message(
        self,
        source_inbox_job_id: int,
        input_text: str,
        chat_id: int,
        message_thread_id: Optional[int],
        authorized_user_id: int,
        now: Optional[float] = None,
    ) -> int:
        text = input_text.strip()
        if not text or len(text) > 8000:
            raise StoreError("Router input must contain 1 to 8000 characters.")
        timestamp = time.time() if now is None else float(now)
        thread_id = int(message_thread_id) if message_thread_id is not None else 0
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            binding = self.connection.execute(
                """
                SELECT 1 FROM surface_bindings
                WHERE chat_id = ? AND message_thread_id = ?
                    AND surface_type = 'control'
                    AND target_type = 'controller' AND target_id = 'control'
                    AND state = 'active'
                """,
                (int(chat_id), thread_id),
            ).fetchone()
            if binding is None:
                raise StoreError("Main router surface is no longer valid.")
            main_agent = self._ensure_main_agent(timestamp)
            self.connection.execute(
                """
                INSERT INTO router_mailbox(
                    source_inbox_job_id, chat_id, message_thread_id,
                    authorized_user_id, input_text, provider_session_id,
                    state, attempts, available_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?)
                ON CONFLICT(source_inbox_job_id) DO NOTHING
                """,
                (
                    int(source_inbox_job_id),
                    int(chat_id),
                    thread_id,
                    int(authorized_user_id),
                    text,
                    main_agent.provider_session_id,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            row = self.connection.execute(
                """
                SELECT mailbox_id, chat_id, message_thread_id,
                    authorized_user_id, input_text
                FROM router_mailbox
                WHERE source_inbox_job_id = ?
                """,
                (int(source_inbox_job_id),),
            ).fetchone()
            if row is None:
                raise StoreError("Could not enqueue the main-router voice message.")
            expected = (
                int(chat_id),
                thread_id,
                int(authorized_user_id),
                text,
            )
            actual = (
                int(row["chat_id"]),
                int(row["message_thread_id"]),
                int(row["authorized_user_id"]),
                str(row["input_text"]),
            )
            if actual != expected:
                raise StoreError("Inbox job was reused for a different router message.")
            self.enqueue_router_voice_status(
                source_inbox_job_id,
                "sending",
                text,
                now=timestamp,
            )
            self.connection.execute("COMMIT")
            return int(row["mailbox_id"])
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def claim_router_mailbox(
        self,
        worker_id: str,
        now: Optional[float] = None,
        lease_seconds: float = 10 * 60,
    ) -> Optional[RouterMailboxJob]:
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                UPDATE router_mailbox
                SET state = 'queued', lease_owner = NULL,
                    lease_expires_at = NULL, available_at = ?, updated_at = ?
                WHERE state = 'leased' AND lease_expires_at <= ?
                """,
                (timestamp, timestamp, timestamp),
            )
            row = self.connection.execute(
                """
                SELECT *
                FROM router_mailbox
                WHERE state = 'queued' AND available_at <= ?
                    AND NOT EXISTS (
                        SELECT 1 FROM router_mailbox
                        WHERE state = 'leased'
                    )
                ORDER BY mailbox_id
                LIMIT 1
                """,
                (timestamp,),
            ).fetchone()
            if row is None:
                self.connection.execute("COMMIT")
                return None
            mailbox_id = int(row["mailbox_id"])
            cursor = self.connection.execute(
                """
                UPDATE router_mailbox
                SET state = 'leased', attempts = attempts + 1,
                    lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE mailbox_id = ? AND state = 'queued'
                """,
                (
                    worker_id,
                    timestamp + float(lease_seconds),
                    timestamp,
                    mailbox_id,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError("Router mailbox claim lost its queue race.")
            claimed = self.connection.execute(
                "SELECT * FROM router_mailbox WHERE mailbox_id = ?",
                (mailbox_id,),
            ).fetchone()
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return RouterMailboxJob(
            mailbox_id=mailbox_id,
            source_inbox_job_id=int(claimed["source_inbox_job_id"]),
            chat_id=int(claimed["chat_id"]),
            input_text=str(claimed["input_text"]),
            provider_session_id=(
                str(claimed["provider_session_id"])
                if claimed["provider_session_id"] is not None
                else None
            ),
            attempts=int(claimed["attempts"]),
        )

    def attach_router_mailbox_session(
        self,
        mailbox_id: int,
        worker_id: str,
        provider_session_id: str,
        now: Optional[float] = None,
    ) -> None:
        if not provider_session_id or len(provider_session_id) > 256:
            raise StoreError("Router provider session ID is invalid.")
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT provider_session_id
                FROM router_mailbox
                WHERE mailbox_id = ? AND state = 'leased'
                    AND lease_owner = ?
                """,
                (int(mailbox_id), worker_id),
            ).fetchone()
            if row is None:
                raise LeaseLostError(
                    f"Router mailbox lease for {mailbox_id} is no longer owned."
                )
            current = row["provider_session_id"]
            if current is not None and str(current) != provider_session_id:
                raise StoreError("Router provider session changed unexpectedly.")
            self.connection.execute(
                """
                UPDATE router_mailbox
                SET provider_session_id = ?, updated_at = ?
                WHERE mailbox_id = ?
                """,
                (provider_session_id, timestamp, int(mailbox_id)),
            )
            main_agent = self._ensure_main_agent(timestamp)
            if (
                main_agent.provider_session_id is not None
                and main_agent.provider_session_id != provider_session_id
            ):
                raise StoreError("Main-router provider session changed unexpectedly.")
            self.connection.execute(
                """
                UPDATE agents
                SET provider_session_id = ?, updated_at = ?
                WHERE agent_id = ?
                """,
                (provider_session_id, timestamp, main_agent.agent_id),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def heartbeat_router_mailbox(
        self,
        mailbox_id: int,
        worker_id: str,
        lease_seconds: float = 10 * 60,
        now: Optional[float] = None,
    ) -> None:
        timestamp = time.time() if now is None else float(now)
        cursor = self.connection.execute(
            """
            UPDATE router_mailbox
            SET lease_expires_at = ?, updated_at = ?
            WHERE mailbox_id = ? AND state = 'leased' AND lease_owner = ?
            """,
            (
                timestamp + float(lease_seconds),
                timestamp,
                int(mailbox_id),
                worker_id,
            ),
        )
        if cursor.rowcount != 1:
            raise LeaseLostError(
                f"Router mailbox lease for {mailbox_id} is no longer owned."
            )

    def _router_reply_markup(
        self,
        mailbox_id: int,
    ) -> Optional[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT token, payload_json
            FROM callback_actions
            WHERE operation_id LIKE ? AND state = 'active'
            ORDER BY action_id
            """,
            (f"router:{int(mailbox_id)}:%",),
        ).fetchall()
        if not rows:
            return None
        return {
            "inline_keyboard": [
                [
                    {
                        "text": str(
                            json.loads(row["payload_json"]).get("choice")
                            or json.loads(row["payload_json"]).get("label")
                        ),
                        "callback_data": f"a:{str(row['token'])}",
                    }
                ]
                for row in rows
            ]
        }

    def _enqueue_router_final_edit(
        self,
        mailbox_id: int,
        preview_text: str,
        timestamp: float,
        operation_suffix: str = "final-edit",
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT r.chat_id, o.telegram_result_json
            FROM router_mailbox AS r
            JOIN outbox_messages AS o
                ON o.operation_id =
                    'router-input:' || r.source_inbox_job_id || ':receipt'
            WHERE r.mailbox_id = ? AND o.state = 'sent'
                AND o.telegram_result_json IS NOT NULL
            """,
            (int(mailbox_id),),
        ).fetchone()
        if row is None:
            return False
        try:
            telegram_message_id = int(
                json.loads(row["telegram_result_json"])["message_id"]
            )
        except (KeyError, TypeError, ValueError):
            raise StoreError("Stored Telegram router receipt is invalid.") from None
        params: dict[str, Any] = {
            "chat_id": int(row["chat_id"]),
            "message_id": telegram_message_id,
            "text": preview_text,
        }
        reply_markup = self._router_reply_markup(mailbox_id)
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        self.enqueue_api_call(
            operation_id=(
                f"router-mailbox:{int(mailbox_id)}:{operation_suffix}"
            ),
            method="editMessageText",
            params=params,
            card={
                "kind": "router_turn",
                "mailbox_id": int(mailbox_id),
                "mode": "final_edit",
            },
            now=timestamp,
        )
        return True

    def complete_router_mailbox(
        self,
        mailbox_id: int,
        worker_id: str,
        provider_session_id: str,
        raw_output: str,
        tool_name: str,
        arguments: dict[str, Any],
        preview_text: str,
        usage: dict[str, Any],
        dispatch_agent_id: Optional[str] = None,
        dispatch_message: Optional[str] = None,
        clarification_options: Optional[list[str]] = None,
        project_creation_plan: Optional[dict[str, Any]] = None,
        topic_rename_plan: Optional[dict[str, Any]] = None,
        now: Optional[float] = None,
    ) -> None:
        if not preview_text or len(preview_text) > 3800:
            raise StoreError("Router preview text is invalid.")
        if (dispatch_agent_id is None) != (dispatch_message is None):
            raise StoreError("Router dispatch arguments are incomplete.")
        if clarification_options is not None and (
            tool_name != "ask_user"
            or not clarification_options
            or len(clarification_options) > 4
        ):
            raise StoreError("Router clarification options are invalid.")
        if project_creation_plan is not None and (
            tool_name != "create_project_agent"
            or set(project_creation_plan)
            != {
                "slug",
                "display_name",
                "provider",
                "project_path",
                "topic_name",
                "provider_config",
            }
        ):
            raise StoreError("Router project-creation plan is invalid.")
        if topic_rename_plan is not None and (
            tool_name != "rename_topic"
            or set(topic_rename_plan)
            != {
                "binding_id",
                "chat_id",
                "message_thread_id",
                "old_name",
                "new_name",
            }
        ):
            raise StoreError("Router topic-rename plan is invalid.")
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT source_inbox_job_id, chat_id, message_thread_id,
                    authorized_user_id
                FROM router_mailbox
                WHERE mailbox_id = ? AND state = 'leased' AND lease_owner = ?
                """,
                (int(mailbox_id), worker_id),
            ).fetchone()
            if row is None:
                raise LeaseLostError(
                    f"Router mailbox lease for {mailbox_id} is no longer owned."
                )
            if dispatch_agent_id is not None and dispatch_message is not None:
                agent = self.resolve_agent(dispatch_agent_id)
                if agent is None or agent.role != "project":
                    raise StoreError("Router dispatch target is not a project agent.")
                self.enqueue_agent_message(
                    agent_id=dispatch_agent_id,
                    source_inbox_job_id=int(row["source_inbox_job_id"]),
                    input_text=dispatch_message,
                    now=timestamp,
                )
            if clarification_options is not None:
                if row["authorized_user_id"] is None:
                    raise StoreError("Router clarification has no authorized user.")
                for index, choice in enumerate(clarification_options):
                    inserted = False
                    for _ in range(5):
                        token = secrets.token_urlsafe(6)
                        try:
                            self.connection.execute(
                                """
                                INSERT INTO callback_actions(
                                    operation_id, token, action_type,
                                    payload_json, chat_id, message_thread_id,
                                    authorized_user_id, one_time, state,
                                    expires_at, created_at, updated_at
                                )
                                VALUES (?, ?, 'router_clarification', ?, ?, ?,
                                    ?, 1, 'active', ?, ?, ?)
                                """,
                                (
                                    (
                                        f"router:{int(mailbox_id)}:"
                                        f"clarify:{index}"
                                    ),
                                    token,
                                    json.dumps(
                                        {
                                            "router_mailbox_id": int(mailbox_id),
                                            "choice": choice,
                                        },
                                        separators=(",", ":"),
                                        sort_keys=True,
                                    ),
                                    int(row["chat_id"]),
                                    (
                                        int(row["message_thread_id"])
                                        if int(row["message_thread_id"]) != 0
                                        else None
                                    ),
                                    int(row["authorized_user_id"]),
                                    timestamp + 24 * 60 * 60,
                                    timestamp,
                                    timestamp,
                                ),
                            )
                        except sqlite3.IntegrityError:
                            continue
                        inserted = True
                        break
                    if not inserted:
                        raise StoreError(
                            "Could not allocate a router clarification token."
                        )
            if project_creation_plan is not None:
                if row["authorized_user_id"] is None:
                    raise StoreError("Router project creation has no authorized user.")
                for index, (action_type, label) in enumerate(
                    (
                        ("router_project_confirm", "Create project agent"),
                        ("router_project_cancel", "Cancel"),
                    )
                ):
                    inserted = False
                    for _ in range(5):
                        token = secrets.token_urlsafe(6)
                        payload = dict(project_creation_plan)
                        payload["label"] = label
                        payload["router_mailbox_id"] = int(mailbox_id)
                        try:
                            self.connection.execute(
                                """
                                INSERT INTO callback_actions(
                                    operation_id, token, action_type,
                                    payload_json, chat_id, message_thread_id,
                                    authorized_user_id, one_time, state,
                                    expires_at, created_at, updated_at
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'active',
                                    ?, ?, ?)
                                """,
                                (
                                    (
                                        f"router:{int(mailbox_id)}:"
                                        f"project:{index}"
                                    ),
                                    token,
                                    action_type,
                                    json.dumps(
                                        payload,
                                        separators=(",", ":"),
                                        sort_keys=True,
                                    ),
                                    int(row["chat_id"]),
                                    (
                                        int(row["message_thread_id"])
                                        if int(row["message_thread_id"]) != 0
                                        else None
                                    ),
                                    int(row["authorized_user_id"]),
                                    timestamp + 10 * 60,
                                    timestamp,
                                    timestamp,
                                ),
                            )
                        except sqlite3.IntegrityError:
                            continue
                        inserted = True
                        break
                    if not inserted:
                        raise StoreError(
                            "Could not allocate a project-confirmation token."
                        )
            if topic_rename_plan is not None:
                if row["authorized_user_id"] is None:
                    raise StoreError("Router topic rename has no authorized user.")
                if int(topic_rename_plan["chat_id"]) != int(row["chat_id"]):
                    raise StoreError("Router topic rename targets another chat.")
                for index, (action_type, label) in enumerate(
                    (
                        ("router_topic_rename_confirm", "Rename topic"),
                        ("router_topic_rename_cancel", "Cancel"),
                    )
                ):
                    inserted = False
                    for _ in range(5):
                        token = secrets.token_urlsafe(6)
                        payload = dict(topic_rename_plan)
                        payload["label"] = label
                        payload["router_mailbox_id"] = int(mailbox_id)
                        try:
                            self.connection.execute(
                                """
                                INSERT INTO callback_actions(
                                    operation_id, token, action_type,
                                    payload_json, chat_id, message_thread_id,
                                    authorized_user_id, one_time, state,
                                    expires_at, created_at, updated_at
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'active',
                                    ?, ?, ?)
                                """,
                                (
                                    (
                                        f"router:{int(mailbox_id)}:"
                                        f"topic-rename:{index}"
                                    ),
                                    token,
                                    action_type,
                                    json.dumps(
                                        payload,
                                        separators=(",", ":"),
                                        sort_keys=True,
                                    ),
                                    int(row["chat_id"]),
                                    (
                                        int(row["message_thread_id"])
                                        if int(row["message_thread_id"]) != 0
                                        else None
                                    ),
                                    int(row["authorized_user_id"]),
                                    timestamp + 10 * 60,
                                    timestamp,
                                    timestamp,
                                ),
                            )
                        except sqlite3.IntegrityError:
                            continue
                        inserted = True
                        break
                    if not inserted:
                        raise StoreError(
                            "Could not allocate a topic-rename confirmation token."
                        )
            self.connection.execute(
                """
                UPDATE router_mailbox
                SET provider_session_id = ?, state = 'succeeded',
                    lease_owner = NULL, lease_expires_at = NULL,
                    raw_output = ?, tool_name = ?, arguments_json = ?,
                    preview_text = ?, usage_json = ?, last_error = NULL,
                    updated_at = ?
                WHERE mailbox_id = ?
                """,
                (
                    provider_session_id,
                    raw_output,
                    tool_name,
                    json.dumps(arguments, separators=(",", ":"), sort_keys=True),
                    preview_text,
                    json.dumps(usage, separators=(",", ":"), sort_keys=True),
                    timestamp,
                    int(mailbox_id),
                ),
            )
            main_agent = self._ensure_main_agent(timestamp)
            self.connection.execute(
                """
                UPDATE agents
                SET provider_session_id = ?, lifecycle_state = 'registered',
                    updated_at = ?
                WHERE agent_id = ?
                """,
                (provider_session_id, timestamp, main_agent.agent_id),
            )
            self._enqueue_router_final_edit(mailbox_id, preview_text, timestamp)
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def fail_router_mailbox(
        self,
        mailbox_id: int,
        worker_id: str,
        error: str,
        now: Optional[float] = None,
        max_attempts: int = 3,
        base_delay: float = 10.0,
    ) -> str:
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT attempts
                FROM router_mailbox
                WHERE mailbox_id = ? AND state = 'leased' AND lease_owner = ?
                """,
                (int(mailbox_id), worker_id),
            ).fetchone()
            if row is None:
                raise LeaseLostError(
                    f"Router mailbox lease for {mailbox_id} is no longer owned."
                )
            attempts = int(row["attempts"])
            state = "dead" if attempts >= max_attempts else "queued"
            delay = base_delay * (2 ** max(0, attempts - 1))
            final_text = (
                "❌ I couldn’t safely interpret that request. Please try "
                "rephrasing it."
            )
            self.connection.execute(
                """
                UPDATE router_mailbox
                SET state = ?, available_at = ?, lease_owner = NULL,
                    lease_expires_at = NULL, last_error = ?,
                    preview_text = CASE WHEN ? = 'dead' THEN ? ELSE preview_text END,
                    updated_at = ?
                WHERE mailbox_id = ?
                """,
                (
                    state,
                    timestamp if state == "dead" else timestamp + delay,
                    str(error)[:2000],
                    state,
                    final_text,
                    timestamp,
                    int(mailbox_id),
                ),
            )
            if state == "dead":
                self._enqueue_router_final_edit(mailbox_id, final_text, timestamp)
            self.connection.execute("COMMIT")
            return state
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def enqueue_router_response_fallback(
        self,
        mailbox_id: int,
        now: Optional[float] = None,
    ) -> int:
        timestamp = time.time() if now is None else float(now)
        row = self.connection.execute(
            """
            SELECT chat_id, message_thread_id, preview_text
            FROM router_mailbox
            WHERE mailbox_id = ? AND state IN ('succeeded', 'dead')
                AND preview_text IS NOT NULL
            """,
            (int(mailbox_id),),
        ).fetchone()
        if row is None:
            raise StoreError("Completed router response is unavailable for fallback.")
        thread_id = int(row["message_thread_id"])
        return self.enqueue_api_call(
            operation_id=f"router-mailbox:{int(mailbox_id)}:final-fallback",
            method="sendMessage",
            params={
                "chat_id": int(row["chat_id"]),
                "message_thread_id": thread_id if thread_id != 0 else None,
                "text": str(row["preview_text"]),
            },
            route={
                "target_type": "controller",
                "target_id": "control",
                "policy": "reply",
                "ttl_seconds": 30 * 24 * 60 * 60,
            },
            now=timestamp,
        )

    def resolve_router_clarification(
        self,
        mailbox_id: int,
        choice: str,
        now: Optional[float] = None,
    ) -> str:
        row = self.connection.execute(
            """
            SELECT input_text, arguments_json
            FROM router_mailbox
            WHERE mailbox_id = ? AND state = 'succeeded'
                AND tool_name = 'ask_user'
            """,
            (int(mailbox_id),),
        ).fetchone()
        if row is None or row["arguments_json"] is None:
            raise StoreError("Router clarification is no longer available.")
        arguments = json.loads(row["arguments_json"])
        options = arguments.get("options")
        question = arguments.get("question")
        if (
            not isinstance(options, list)
            or choice not in options
            or not isinstance(question, str)
        ):
            raise StoreError("Router clarification choice is invalid.")
        timestamp = time.time() if now is None else float(now)
        self.connection.execute(
            """
            UPDATE callback_actions
            SET state = 'expired', updated_at = ?
            WHERE operation_id LIKE ? AND state = 'active'
            """,
            (timestamp, f"router:{int(mailbox_id)}:clarify:%"),
        )
        return (
            "Continue the prior request using the user's clarification.\n\n"
            f"Original request: {str(row['input_text'])}\n"
            f"Question: {question}\n"
            f"User's answer: {choice}"
        )

    def expire_router_project_actions(
        self,
        mailbox_id: int,
        now: Optional[float] = None,
    ) -> None:
        timestamp = time.time() if now is None else float(now)
        self.connection.execute(
            """
            UPDATE callback_actions
            SET state = 'expired', updated_at = ?
            WHERE operation_id LIKE ? AND state = 'active'
            """,
            (timestamp, f"router:{int(mailbox_id)}:project:%"),
        )

    def expire_router_topic_rename_actions(
        self,
        mailbox_id: int,
        now: Optional[float] = None,
    ) -> None:
        timestamp = time.time() if now is None else float(now)
        self.connection.execute(
            """
            UPDATE callback_actions
            SET state = 'expired', updated_at = ?
            WHERE operation_id LIKE ? AND state = 'active'
            """,
            (timestamp, f"router:{int(mailbox_id)}:topic-rename:%"),
        )

    def attach_enrolled_project(
        self,
        chat_id: int,
        message_thread_id: Optional[int],
        project_slug: str,
        provider_config: Optional[dict[str, Any]] = None,
        now: Optional[float] = None,
    ) -> tuple[ManagedAgent, bool]:
        project = self.resolve_project(project_slug)
        if project is None:
            raise StoreError("That project is not enrolled.")
        binding = self.resolve_surface_binding(chat_id, message_thread_id)
        if binding is None or binding.surface_type != "project":
            raise StoreError("Use this command inside a provisioned project topic.")
        if binding.target_type == "agent":
            agent = self.resolve_agent(binding.target_id)
            if agent is None:
                raise StoreError("Project topic references a missing managed agent.")
            expected = (
                project.slug,
                project.provider,
                project.project_path,
                binding.binding_id,
            )
            actual = (
                agent.slug,
                agent.provider,
                agent.project_path,
                agent.surface_binding_id,
            )
            if actual != expected:
                raise StoreError("This topic is attached to a different project.")
            if (
                provider_config is not None
                and agent.provider_config
                != validate_provider_config(agent.provider, provider_config)
            ):
                raise StoreError(
                    "This project agent already uses different model settings."
                )
            return agent, False
        if (binding.target_type, binding.target_id) != ("controller", "control"):
            raise StoreError("Project topic is not eligible for agent creation.")
        return self.register_project_agent(
            chat_id=chat_id,
            surface_name=binding.display_name,
            slug=project.slug,
            provider=project.provider,
            project_path=project.project_path,
            provider_config=provider_config,
            now=now,
        )

    @staticmethod
    def _agent_console_from_row(row: sqlite3.Row) -> AgentConsole:
        return AgentConsole(
            agent_id=str(row["agent_id"]),
            tmux_session_name=str(row["tmux_session_name"]),
            state=str(row["state"]),
        )

    def resolve_agent_console(self, agent_id: str) -> Optional[AgentConsole]:
        row = self.connection.execute(
            "SELECT * FROM agent_consoles WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        return self._agent_console_from_row(row) if row is not None else None

    def latest_agent_usage(self, agent_id: str) -> Optional[dict[str, Any]]:
        row = self.connection.execute(
            """
            SELECT usage_json
            FROM agent_mailbox
            WHERE agent_id = ? AND state = 'succeeded' AND usage_json IS NOT NULL
            ORDER BY mailbox_id DESC
            LIMIT 1
            """,
            (agent_id,),
        ).fetchone()
        if row is None:
            return None
        usage = json.loads(row["usage_json"])
        if not isinstance(usage, dict):
            raise StoreError("Stored agent usage metadata is invalid.")
        return usage

    def pause_agent(self, agent_id: str, now: Optional[float] = None) -> ManagedAgent:
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            active = self.connection.execute(
                """
                SELECT 1 FROM agent_mailbox
                WHERE agent_id = ? AND state = 'leased'
                """,
                (agent_id,),
            ).fetchone()
            console = self.connection.execute(
                """
                SELECT 1 FROM agent_consoles
                WHERE agent_id = ? AND state IN ('starting', 'running')
                """,
                (agent_id,),
            ).fetchone()
            if active is not None or console is not None:
                raise StoreError("Agent must be idle before it can be paused.")
            cursor = self.connection.execute(
                """
                UPDATE agents
                SET lifecycle_state = 'stopped', updated_at = ?
                WHERE agent_id = ? AND role IN ('project', 'worker')
                    AND lifecycle_state != 'stopped'
                """,
                (timestamp, agent_id),
            )
            if cursor.rowcount != 1:
                raise StoreError("Managed agent is already paused or unavailable.")
            row = self.connection.execute(
                "SELECT * FROM agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return self._managed_agent_from_row(row)

    def resume_agent(self, agent_id: str, now: Optional[float] = None) -> ManagedAgent:
        timestamp = time.time() if now is None else float(now)
        cursor = self.connection.execute(
            """
            UPDATE agents
            SET lifecycle_state = 'registered', updated_at = ?
            WHERE agent_id = ? AND role IN ('project', 'worker')
                AND lifecycle_state = 'stopped'
            """,
            (timestamp, agent_id),
        )
        if cursor.rowcount != 1:
            raise StoreError("Managed agent is not paused.")
        return self.resolve_agent(agent_id)

    def reset_agent_session(
        self,
        agent_id: str,
        now: Optional[float] = None,
    ) -> ManagedAgent:
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            busy = self.connection.execute(
                """
                SELECT 1 FROM agent_mailbox
                WHERE agent_id = ? AND state IN ('queued', 'leased')
                """,
                (agent_id,),
            ).fetchone()
            console = self.connection.execute(
                """
                SELECT 1 FROM agent_consoles
                WHERE agent_id = ? AND state IN ('starting', 'running')
                """,
                (agent_id,),
            ).fetchone()
            if busy is not None or console is not None:
                raise StoreError(
                    "Agent must have an idle mailbox and stopped console "
                    "before starting a new session."
                )
            cursor = self.connection.execute(
                """
                UPDATE agents
                SET provider_session_id = NULL, lifecycle_state = 'registered',
                    updated_at = ?
                WHERE agent_id = ? AND role IN ('project', 'worker')
                    AND provider_session_id IS NOT NULL
                    AND lifecycle_state != 'running'
                """,
                (timestamp, agent_id),
            )
            if cursor.rowcount != 1:
                raise StoreError("Managed agent has no persisted session to replace.")
            self.connection.execute(
                """
                INSERT INTO events(
                    kind, subject_type, subject_id, details_json, created_at
                )
                VALUES ('agent_session_reset', 'agent', ?, '{}', ?)
                """,
                (agent_id, timestamp),
            )
            row = self.connection.execute(
                "SELECT * FROM agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return self._managed_agent_from_row(row)

    def reserve_agent_console(
        self,
        agent_id: str,
        tmux_session_name: str,
        now: Optional[float] = None,
    ) -> AgentConsole:
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            agent = self.connection.execute(
                """
                SELECT provider_session_id
                FROM agents
                WHERE agent_id = ? AND role IN ('project', 'worker')
                """,
                (agent_id,),
            ).fetchone()
            if agent is None:
                raise StoreError("Managed agent was not found.")
            if not agent["provider_session_id"]:
                raise StoreError(
                    "Managed agent has no persisted provider session to resume."
                )
            busy = self.connection.execute(
                """
                SELECT 1
                FROM agent_mailbox
                WHERE agent_id = ? AND state IN ('queued', 'leased')
                LIMIT 1
                """,
                (agent_id,),
            ).fetchone()
            if busy is not None:
                raise StoreError(
                    "Managed agent mailbox must be idle before opening a console."
                )
            existing = self.connection.execute(
                "SELECT * FROM agent_consoles WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            if existing is not None and str(existing["state"]) != "stopped":
                raise StoreError("Managed agent console is already reserved.")
            self.connection.execute(
                """
                INSERT INTO agent_consoles(
                    agent_id, tmux_session_name, state, created_at, updated_at
                )
                VALUES (?, ?, 'starting', ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    tmux_session_name = excluded.tmux_session_name,
                    state = 'starting',
                    updated_at = excluded.updated_at
                """,
                (agent_id, tmux_session_name, timestamp, timestamp),
            )
            row = self.connection.execute(
                "SELECT * FROM agent_consoles WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return self._agent_console_from_row(row)

    def set_agent_console_state(
        self,
        agent_id: str,
        expected_state: str,
        state: str,
        now: Optional[float] = None,
    ) -> AgentConsole:
        if state not in {"starting", "running", "stopped"}:
            raise StoreError("Managed agent console state is invalid.")
        timestamp = time.time() if now is None else float(now)
        cursor = self.connection.execute(
            """
            UPDATE agent_consoles
            SET state = ?, updated_at = ?
            WHERE agent_id = ? AND state = ?
            """,
            (state, timestamp, agent_id, expected_state),
        )
        if cursor.rowcount != 1:
            raise StoreError("Managed agent console state changed unexpectedly.")
        row = self.connection.execute(
            "SELECT * FROM agent_consoles WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        return self._agent_console_from_row(row)

    def resolve_agent_for_surface(
        self,
        chat_id: int,
        message_thread_id: Optional[int] = None,
    ) -> Optional[ManagedAgent]:
        thread_id = int(message_thread_id) if message_thread_id is not None else 0
        row = self.connection.execute(
            """
            SELECT a.*
            FROM surface_bindings AS b
            JOIN agents AS a
                ON b.target_type = 'agent' AND b.target_id = a.agent_id
            WHERE b.chat_id = ? AND b.message_thread_id = ?
                AND b.state = 'active'
            """,
            (int(chat_id), thread_id),
        ).fetchone()
        return self._managed_agent_from_row(row) if row is not None else None

    def register_project_agent(
        self,
        chat_id: int,
        surface_name: str,
        slug: str,
        provider: str,
        project_path: str,
        provider_config: Optional[dict[str, Any]] = None,
        now: Optional[float] = None,
    ) -> tuple[ManagedAgent, bool]:
        if (
            len(slug) > 48
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)
            or "--" in slug
            or slug == "root"
        ):
            raise StoreError(
                "Agent slug must use lowercase letters, digits, and single hyphens."
            )
        if provider not in {"codex", "claude"}:
            raise StoreError("Agent provider is invalid.")
        if not project_path or not Path(project_path).is_absolute():
            raise StoreError("Agent project path must be absolute.")
        normalized_config = validate_provider_config(provider, provider_config)
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            binding_row = self.connection.execute(
                """
                SELECT *
                FROM surface_bindings
                WHERE chat_id = ? AND display_name = ?
                    AND surface_type = 'project' AND state = 'active'
                ORDER BY binding_id
                LIMIT 1
                """,
                (int(chat_id), surface_name),
            ).fetchone()
            if binding_row is None:
                raise StoreError("Managed project surface was not found.")
            binding = self._surface_binding_from_row(binding_row)
            if binding.message_thread_id is None:
                raise StoreError("A project agent requires a Telegram topic.")
            if binding.target_type == "agent":
                existing_row = self.connection.execute(
                    "SELECT * FROM agents WHERE agent_id = ?",
                    (binding.target_id,),
                ).fetchone()
                if existing_row is None:
                    raise StoreError("Surface references a missing managed agent.")
                existing = self._managed_agent_from_row(existing_row)
                expected = (slug, provider, project_path, binding.binding_id)
                actual = (
                    existing.slug,
                    existing.provider,
                    existing.project_path,
                    existing.surface_binding_id,
                )
                if actual != expected:
                    raise StoreError(
                        "Project surface is already registered to another agent."
                    )
                if (
                    provider_config is not None
                    and existing.provider_config != normalized_config
                ):
                    raise StoreError(
                        "Project agent already uses different model settings."
                    )
                self.connection.execute("COMMIT")
                return existing, False
            if (binding.target_type, binding.target_id) != ("controller", "control"):
                raise StoreError(
                    "Project surface target is not eligible for agent registration."
                )

            root_row = self.connection.execute(
                "SELECT * FROM agents WHERE hierarchical_name = 'tc--root'"
            ).fetchone()
            if root_row is None:
                root_id = f"agent_{secrets.token_urlsafe(12)}"
                self.connection.execute(
                    """
                    INSERT INTO agents(
                        agent_id, parent_agent_id, role, slug,
                        hierarchical_name, provider, project_path,
                        provider_session_id, surface_binding_id,
                        lifecycle_state, created_at, updated_at
                    )
                    VALUES (?, NULL, 'main', 'root', 'tc--root', 'codex',
                        NULL, NULL, NULL, 'registered', ?, ?)
                    """,
                    (root_id, timestamp, timestamp),
                )
            else:
                root_id = str(root_row["agent_id"])

            hierarchical_name = f"tc--root--{slug}"
            if len(hierarchical_name) > 128:
                raise StoreError("Derived agent name is too long.")
            sibling = self.connection.execute(
                """
                SELECT 1 FROM agents
                WHERE parent_agent_id = ? AND slug = ?
                """,
                (root_id, slug),
            ).fetchone()
            if sibling is not None:
                raise StoreError("An active sibling already uses this agent slug.")

            agent_id = f"agent_{secrets.token_urlsafe(12)}"
            self.connection.execute(
                """
                INSERT INTO agents(
                    agent_id, parent_agent_id, role, slug,
                    hierarchical_name, provider, project_path,
                    provider_session_id, surface_binding_id,
                    lifecycle_state, provider_config_json, created_at, updated_at
                )
                VALUES (?, ?, 'project', ?, ?, ?, ?, NULL, ?,
                    'registered', ?, ?, ?)
                """,
                (
                    agent_id,
                    root_id,
                    slug,
                    hierarchical_name,
                    provider,
                    project_path,
                    binding.binding_id,
                    json.dumps(
                        normalized_config,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    timestamp,
                    timestamp,
                ),
            )
            updated = self.connection.execute(
                """
                UPDATE surface_bindings
                SET target_type = 'agent', target_id = ?, updated_at = ?
                WHERE binding_id = ? AND target_type = 'controller'
                    AND target_id = 'control' AND state = 'active'
                """,
                (agent_id, timestamp, binding.binding_id),
            )
            if updated.rowcount != 1:
                raise StoreError("Project surface changed during registration.")
            self.connection.execute(
                """
                INSERT INTO events(
                    kind, subject_type, subject_id, details_json, created_at
                )
                VALUES ('agent_registered', 'agent', ?, ?, ?)
                """,
                (
                    agent_id,
                    json.dumps(
                        {
                            "hierarchical_name": hierarchical_name,
                            "provider": provider,
                            "surface_binding_id": binding.binding_id,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    timestamp,
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            agent = self._managed_agent_from_row(row)
            self.connection.execute("COMMIT")
            return agent, True
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def configure_agent_provider(
        self,
        agent_id: str,
        updates: dict[str, Optional[str]],
        now: Optional[float] = None,
    ) -> ManagedAgent:
        if not updates or not set(updates).issubset({"model", "effort"}):
            raise StoreError("Agent model configuration update is invalid.")
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            if row is None or str(row["role"]) not in {"project", "worker"}:
                raise StoreError("Managed project or worker agent was not found.")
            agent = self._managed_agent_from_row(row)
            busy = self.connection.execute(
                """
                SELECT 1 FROM agent_mailbox
                WHERE agent_id = ? AND state = 'leased'
                UNION ALL
                SELECT 1 FROM agent_consoles
                WHERE agent_id = ? AND state IN ('starting', 'running')
                LIMIT 1
                """,
                (agent_id, agent_id),
            ).fetchone()
            if busy is not None:
                raise StoreError(
                    "Wait for the active agent turn or console before reconfiguring it."
                )
            config = dict(agent.provider_config)
            for key, value in updates.items():
                if value is None:
                    config.pop(key, None)
                else:
                    config[key] = value
            config = validate_provider_config(agent.provider, config)
            self.connection.execute(
                """
                UPDATE agents
                SET provider_config_json = ?, updated_at = ?
                WHERE agent_id = ?
                """,
                (
                    json.dumps(config, separators=(",", ":"), sort_keys=True),
                    timestamp,
                    agent_id,
                ),
            )
            updated = self.connection.execute(
                "SELECT * FROM agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            self.connection.execute("COMMIT")
            return self._managed_agent_from_row(updated)
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def enqueue_agent_message(
        self,
        agent_id: str,
        source_inbox_job_id: int,
        input_text: str,
        now: Optional[float] = None,
    ) -> int:
        text = input_text.strip()
        if not text:
            raise StoreError("Agent mailbox input cannot be empty.")
        timestamp = time.time() if now is None else float(now)
        self.connection.execute(
            """
            INSERT INTO agent_mailbox(
                agent_id, source_inbox_job_id, input_text,
                provider_session_id, state, attempts, available_at,
                created_at, updated_at
            )
            VALUES (?, ?, ?, NULL, 'queued', 0, ?, ?, ?)
            ON CONFLICT(source_inbox_job_id) DO NOTHING
            """,
            (
                agent_id,
                int(source_inbox_job_id),
                text,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        row = self.connection.execute(
            """
            SELECT mailbox_id, agent_id, input_text
            FROM agent_mailbox
            WHERE source_inbox_job_id = ?
            """,
            (int(source_inbox_job_id),),
        ).fetchone()
        if row is None:
            raise StoreError("Could not enqueue the managed agent message.")
        if str(row["agent_id"]) != agent_id or str(row["input_text"]) != text:
            raise StoreError(
                "Inbox job was reused for a different managed agent message."
            )
        return int(row["mailbox_id"])

    def enqueue_agent_message_with_receipt(
        self,
        agent_id: str,
        source_inbox_job_id: int,
        input_text: str,
        chat_id: int,
        message_thread_id: Optional[int],
        receipt_text: str,
        receipt_parse_mode: Optional[str] = None,
        now: Optional[float] = None,
    ) -> int:
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            mailbox_id = self.enqueue_agent_message(
                agent_id=agent_id,
                source_inbox_job_id=source_inbox_job_id,
                input_text=input_text,
                now=timestamp,
            )
            self.enqueue_agent_receipt(
                agent_id,
                source_inbox_job_id,
                chat_id,
                message_thread_id,
                receipt_text,
                input_kind="text",
                parse_mode=receipt_parse_mode,
                now=timestamp,
            )
            self.connection.execute("COMMIT")
            return mailbox_id
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def enqueue_agent_receipt(
        self,
        agent_id: str,
        source_inbox_job_id: int,
        chat_id: int,
        message_thread_id: Optional[int],
        receipt_text: str,
        input_kind: str = "text",
        parse_mode: Optional[str] = None,
        now: Optional[float] = None,
    ) -> int:
        if input_kind not in {"text", "voice"}:
            raise StoreError("Managed agent receipt input kind is invalid.")
        timestamp = time.time() if now is None else float(now)
        thread_id = int(message_thread_id) if message_thread_id is not None else 0
        owns_transaction = not self.connection.in_transaction
        if owns_transaction:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            binding = self.connection.execute(
                """
                SELECT 1
                FROM surface_bindings
                WHERE chat_id = ? AND message_thread_id = ?
                    AND target_type = 'agent' AND target_id = ?
                    AND state = 'active'
                """,
                (int(chat_id), thread_id, agent_id),
            ).fetchone()
            if binding is None:
                raise StoreError("Managed agent receipt route is no longer valid.")
            params = {
                "chat_id": int(chat_id),
                "message_thread_id": (
                    int(message_thread_id)
                    if message_thread_id is not None
                    else None
                ),
                "text": receipt_text,
            }
            if parse_mode is not None:
                if parse_mode != "HTML":
                    raise StoreError("Managed agent receipt parse mode is invalid.")
                params["parse_mode"] = parse_mode
            message_id = self.enqueue_api_call(
                operation_id=f"agent-input:{int(source_inbox_job_id)}:receipt",
                method="sendMessage",
                params=params,
                route={
                    "target_type": "agent",
                    "target_id": agent_id,
                    "policy": "reply",
                    "ttl_seconds": 30 * 24 * 60 * 60,
                },
                card={
                    "kind": "agent_turn",
                    "source_inbox_job_id": int(source_inbox_job_id),
                    "input_kind": input_kind,
                    "mode": "receipt",
                },
                now=timestamp,
            )
            if owns_transaction:
                self.connection.execute("COMMIT")
            return message_id
        except BaseException:
            if owns_transaction:
                self.connection.execute("ROLLBACK")
            raise

    @staticmethod
    def agent_voice_status_text(
        stage: str,
        input_text: str,
        provider: str = "codex",
    ) -> str:
        transcript = input_text.strip()
        if len(transcript) > 3400:
            transcript = transcript[:3397].rstrip() + "…"
        transcript = html.escape(transcript)
        if stage == "sending":
            return f"📤 <b>Sending</b>\n<blockquote>{transcript}</blockquote>"
        if stage == "working":
            provider_name = "Claude" if provider == "claude" else "Codex"
            return (
                f"🧠 <b>{provider_name} is working…</b>\n"
                f"<blockquote>{transcript}</blockquote>"
            )
        raise StoreError("Managed voice status stage is invalid.")

    def enqueue_agent_voice_status(
        self,
        source_inbox_job_id: int,
        stage: str,
        input_text: str,
        now: Optional[float] = None,
    ) -> Optional[int]:
        timestamp = time.time() if now is None else float(now)
        receipt = self.connection.execute(
            """
            SELECT params_json, card_json, telegram_result_json
            FROM outbox_messages
            WHERE operation_id = ? AND state = 'sent'
            """,
            (f"agent-input:{int(source_inbox_job_id)}:receipt",),
        ).fetchone()
        if receipt is None or receipt["telegram_result_json"] is None:
            return None
        card = json.loads(receipt["card_json"])
        if card.get("input_kind") != "voice":
            return None
        provider_row = self.connection.execute(
            """
            SELECT a.provider
            FROM agent_mailbox AS m
            JOIN agents AS a ON a.agent_id = m.agent_id
            WHERE m.source_inbox_job_id = ?
            """,
            (int(source_inbox_job_id),),
        ).fetchone()
        if provider_row is None:
            raise StoreError("Managed voice receipt agent is unavailable.")
        provider = str(provider_row["provider"])
        result = json.loads(receipt["telegram_result_json"])
        params = json.loads(receipt["params_json"])
        try:
            message_id = int(result["message_id"])
            chat_id = int(params["chat_id"])
        except (KeyError, TypeError, ValueError):
            raise StoreError("Stored Telegram voice receipt is invalid.") from None
        return self.enqueue_api_call(
            operation_id=(
                f"agent-input:{int(source_inbox_job_id)}:status:{stage}"
            ),
            method="editMessageText",
            params={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": self.agent_voice_status_text(
                    stage,
                    input_text,
                    provider,
                ),
                "parse_mode": "HTML",
            },
            card={
                "kind": "agent_turn",
                "source_inbox_job_id": int(source_inbox_job_id),
                "mode": "status_edit",
            },
            now=timestamp,
        )

    def enqueue_agent_voice_message(
        self,
        agent_id: str,
        source_inbox_job_id: int,
        input_text: str,
        now: Optional[float] = None,
    ) -> int:
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            mailbox_id = self.enqueue_agent_message(
                agent_id,
                source_inbox_job_id,
                input_text,
                now=timestamp,
            )
            self.enqueue_agent_voice_status(
                source_inbox_job_id,
                "sending",
                input_text,
                now=timestamp,
            )
            self.connection.execute("COMMIT")
            return mailbox_id
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def claim_agent_mailbox(
        self,
        worker_id: str,
        now: Optional[float] = None,
        lease_seconds: float = 2 * 60 * 60,
    ) -> Optional[AgentMailboxJob]:
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            expired = self.connection.execute(
                """
                SELECT DISTINCT agent_id
                FROM agent_mailbox
                WHERE state = 'leased' AND lease_expires_at <= ?
                """,
                (timestamp,),
            ).fetchall()
            self.connection.execute(
                """
                UPDATE agent_mailbox
                SET state = 'queued', lease_owner = NULL,
                    lease_expires_at = NULL, available_at = ?, updated_at = ?
                WHERE state = 'leased' AND lease_expires_at <= ?
                """,
                (timestamp, timestamp, timestamp),
            )
            for row in expired:
                self.connection.execute(
                    """
                    UPDATE agents
                    SET lifecycle_state = 'registered', updated_at = ?
                    WHERE agent_id = ? AND lifecycle_state = 'running'
                    """,
                    (timestamp, str(row["agent_id"])),
                )
            row = self.connection.execute(
                """
                SELECT m.*
                FROM agent_mailbox AS m
                JOIN agents AS a ON a.agent_id = m.agent_id
                WHERE m.state = 'queued' AND m.available_at <= ?
                    AND a.lifecycle_state != 'stopped'
                    AND NOT EXISTS (
                        SELECT 1
                        FROM agent_consoles AS console
                        WHERE console.agent_id = m.agent_id
                            AND console.state IN ('starting', 'running')
                    )
                    AND NOT EXISTS (
                        SELECT 1
                        FROM agent_mailbox AS active
                        WHERE active.agent_id = m.agent_id
                            AND active.state = 'leased'
                    )
                ORDER BY m.mailbox_id
                LIMIT 1
                """,
                (timestamp,),
            ).fetchone()
            if row is None:
                self.connection.execute("COMMIT")
                return None
            mailbox_id = int(row["mailbox_id"])
            cursor = self.connection.execute(
                """
                UPDATE agent_mailbox
                SET state = 'leased', attempts = attempts + 1,
                    lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE mailbox_id = ? AND state = 'queued'
                """,
                (
                    worker_id,
                    timestamp + float(lease_seconds),
                    timestamp,
                    mailbox_id,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError("Agent mailbox claim lost its queue race.")
            self.connection.execute(
                """
                UPDATE agents
                SET lifecycle_state = 'running', updated_at = ?
                WHERE agent_id = ?
                    AND lifecycle_state IN ('registered', 'stopped', 'failed')
                """,
                (timestamp, str(row["agent_id"])),
            )
            claimed = self.connection.execute(
                "SELECT * FROM agent_mailbox WHERE mailbox_id = ?",
                (mailbox_id,),
            ).fetchone()
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return AgentMailboxJob(
            mailbox_id=mailbox_id,
            agent_id=str(claimed["agent_id"]),
            source_inbox_job_id=int(claimed["source_inbox_job_id"]),
            input_text=str(claimed["input_text"]),
            provider_session_id=(
                str(claimed["provider_session_id"])
                if claimed["provider_session_id"] is not None
                else None
            ),
            attempts=int(claimed["attempts"]),
        )

    def attach_agent_mailbox_session(
        self,
        mailbox_id: int,
        worker_id: str,
        provider_session_id: str,
        now: Optional[float] = None,
    ) -> None:
        if not provider_session_id:
            raise StoreError("Provider session ID cannot be empty.")
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT agent_id, provider_session_id
                FROM agent_mailbox
                WHERE mailbox_id = ? AND state = 'leased'
                    AND lease_owner = ?
                """,
                (int(mailbox_id), worker_id),
            ).fetchone()
            if row is None:
                raise LeaseLostError(
                    f"Agent mailbox lease for {mailbox_id} is no longer owned."
                )
            existing = row["provider_session_id"]
            if existing is not None and str(existing) != provider_session_id:
                raise StoreError("Agent mailbox provider session changed unexpectedly.")
            self.connection.execute(
                """
                UPDATE agent_mailbox
                SET provider_session_id = ?, updated_at = ?
                WHERE mailbox_id = ?
                """,
                (provider_session_id, timestamp, int(mailbox_id)),
            )
            agent = self.connection.execute(
                """
                UPDATE agents
                SET provider_session_id = ?, updated_at = ?
                WHERE agent_id = ?
                    AND (provider_session_id IS NULL OR provider_session_id = ?)
                """,
                (
                    provider_session_id,
                    timestamp,
                    str(row["agent_id"]),
                    provider_session_id,
                ),
            )
            if agent.rowcount != 1:
                raise StoreError("Managed agent provider session changed unexpectedly.")
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def heartbeat_agent_mailbox(
        self,
        mailbox_id: int,
        worker_id: str,
        lease_seconds: float = 2 * 60 * 60,
        now: Optional[float] = None,
    ) -> None:
        timestamp = time.time() if now is None else float(now)
        cursor = self.connection.execute(
            """
            UPDATE agent_mailbox
            SET lease_expires_at = ?, updated_at = ?
            WHERE mailbox_id = ? AND state = 'leased'
                AND lease_owner = ?
            """,
            (
                timestamp + float(lease_seconds),
                timestamp,
                int(mailbox_id),
                worker_id,
            ),
        )
        if cursor.rowcount != 1:
            raise LeaseLostError(
                f"Agent mailbox lease for {mailbox_id} is no longer owned."
            )

    def complete_agent_mailbox(
        self,
        mailbox_id: int,
        worker_id: str,
        provider_session_id: str,
        response_text: str,
        response_chunks: list[str],
        usage: dict[str, Any],
        now: Optional[float] = None,
    ) -> None:
        if not response_chunks or any(not chunk for chunk in response_chunks):
            raise StoreError("Agent response chunks are invalid.")
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT m.agent_id, m.source_inbox_job_id, a.surface_binding_id
                FROM agent_mailbox AS m
                JOIN agents AS a ON a.agent_id = m.agent_id
                WHERE m.mailbox_id = ? AND m.state = 'leased'
                    AND m.lease_owner = ?
                """,
                (int(mailbox_id), worker_id),
            ).fetchone()
            if row is None:
                raise LeaseLostError(
                    f"Agent mailbox lease for {mailbox_id} is no longer owned."
                )
            binding = self.connection.execute(
                """
                SELECT *
                FROM surface_bindings
                WHERE binding_id = ? AND target_type = 'agent'
                    AND target_id = ? AND state = 'active'
                """,
                (int(row["surface_binding_id"]), str(row["agent_id"])),
            ).fetchone()
            if binding is None:
                raise StoreError("Managed agent surface binding is no longer valid.")
            self.connection.execute(
                """
                UPDATE agent_mailbox
                SET provider_session_id = ?, state = 'succeeded',
                    lease_owner = NULL, lease_expires_at = NULL,
                    response_text = ?, usage_json = ?, updated_at = ?
                WHERE mailbox_id = ? AND state = 'leased'
                    AND lease_owner = ?
                """,
                (
                    provider_session_id,
                    response_text,
                    json.dumps(usage, separators=(",", ":"), sort_keys=True),
                    timestamp,
                    int(mailbox_id),
                    worker_id,
                ),
            )
            self.connection.execute(
                """
                UPDATE agents
                SET provider_session_id = ?, lifecycle_state = 'registered',
                    updated_at = ?
                WHERE agent_id = ?
                """,
                (provider_session_id, timestamp, str(row["agent_id"])),
            )
            thread_id = int(binding["message_thread_id"])
            router_origin = self.connection.execute(
                """
                SELECT mailbox_id, chat_id, message_thread_id
                FROM router_mailbox
                WHERE source_inbox_job_id = ?
                    AND state = 'succeeded'
                    AND tool_name = 'send_to_agent'
                """,
                (int(row["source_inbox_job_id"]),),
            ).fetchone()
            if router_origin is not None:
                router_mailbox_id = int(router_origin["mailbox_id"])
                self.connection.execute(
                    """
                    UPDATE router_mailbox
                    SET preview_text = ?, updated_at = ?
                    WHERE mailbox_id = ?
                    """,
                    (
                        response_chunks[0],
                        timestamp,
                        router_mailbox_id,
                    ),
                )
                self._enqueue_router_final_edit(
                    router_mailbox_id,
                    response_chunks[0],
                    timestamp,
                    operation_suffix="agent-final-edit",
                )
                router_thread_id = int(router_origin["message_thread_id"])
                for index, chunk in enumerate(response_chunks[1:], start=2):
                    self.enqueue_api_call(
                        operation_id=(
                            f"router-mailbox:{router_mailbox_id}:"
                            f"agent-response:{index}"
                        ),
                        method="sendMessage",
                        params={
                            "chat_id": int(router_origin["chat_id"]),
                            "message_thread_id": (
                                router_thread_id
                                if router_thread_id != 0
                                else None
                            ),
                            "text": chunk,
                        },
                        route={
                            "target_type": "controller",
                            "target_id": "control",
                            "policy": "reply",
                            "ttl_seconds": 30 * 24 * 60 * 60,
                        },
                        now=timestamp,
                    )
                self.connection.execute("COMMIT")
                return
            receipt = self.connection.execute(
                """
                SELECT state, params_json, telegram_result_json
                FROM outbox_messages
                WHERE operation_id = ?
                """,
                (f"agent-input:{int(row['source_inbox_job_id'])}:receipt",),
            ).fetchone()
            if receipt is None:
                receipt = self.connection.execute(
                    """
                    SELECT state, params_json, telegram_result_json
                    FROM outbox_messages
                    WHERE operation_id = ?
                    """,
                    (f"agent-mailbox:{mailbox_id}:receipt",),
                ).fetchone()
            replaces_receipt = receipt is not None and len(response_chunks) == 1
            if (
                replaces_receipt
                and str(receipt["state"]) == "sent"
                and receipt["telegram_result_json"] is not None
            ):
                receipt_result = json.loads(receipt["telegram_result_json"])
                try:
                    receipt_message_id = int(receipt_result["message_id"])
                except (KeyError, TypeError, ValueError):
                    raise StoreError(
                        "Stored Telegram receipt result is invalid."
                    ) from None
                self.enqueue_api_call(
                    operation_id=f"agent-mailbox:{mailbox_id}:final-edit",
                    method="editMessageText",
                    params={
                        "chat_id": int(binding["chat_id"]),
                        "message_id": receipt_message_id,
                        "text": response_chunks[0],
                    },
                    card={
                        "kind": "agent_turn",
                        "mailbox_id": mailbox_id,
                        "mode": "final_edit",
                    },
                    now=timestamp,
                )
            chunks_to_send = [] if replaces_receipt else response_chunks
            for index, chunk in enumerate(chunks_to_send, start=1):
                self.enqueue_api_call(
                    operation_id=f"agent-mailbox:{mailbox_id}:response:{index}",
                    method="sendMessage",
                    params={
                        "chat_id": int(binding["chat_id"]),
                        "message_thread_id": thread_id if thread_id != 0 else None,
                        "text": chunk,
                    },
                    route={
                        "target_type": "agent",
                        "target_id": str(row["agent_id"]),
                        "policy": "reply",
                        "ttl_seconds": 30 * 24 * 60 * 60,
                    },
                    now=timestamp,
                )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def fail_agent_mailbox(
        self,
        mailbox_id: int,
        worker_id: str,
        error: str,
        now: Optional[float] = None,
        max_attempts: int = 3,
        base_delay: float = 10.0,
    ) -> str:
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT agent_id, source_inbox_job_id, attempts
                FROM agent_mailbox
                WHERE mailbox_id = ? AND state = 'leased'
                    AND lease_owner = ?
                """,
                (int(mailbox_id), worker_id),
            ).fetchone()
            if row is None:
                raise LeaseLostError(
                    f"Agent mailbox lease for {mailbox_id} is no longer owned."
                )
            attempts = int(row["attempts"])
            state = "dead" if attempts >= max_attempts else "queued"
            delay = base_delay * (2 ** max(0, attempts - 1))
            self.connection.execute(
                """
                UPDATE agent_mailbox
                SET state = ?, available_at=?, lease_owner = NULL,
                    lease_expires_at = NULL, last_error = ?, updated_at = ?
                WHERE mailbox_id = ?
                """,
                (
                    state,
                    timestamp if state == "dead" else timestamp + delay,
                    str(error)[:2000],
                    timestamp,
                    int(mailbox_id),
                ),
            )
            self.connection.execute(
                """
                UPDATE agents
                SET lifecycle_state = ?, updated_at = ?
                WHERE agent_id = ?
                """,
                (
                    "failed" if state == "dead" else "registered",
                    timestamp,
                    str(row["agent_id"]),
                ),
            )
            if state == "dead":
                router_origin = self.connection.execute(
                    """
                    SELECT mailbox_id
                    FROM router_mailbox
                    WHERE source_inbox_job_id = ?
                        AND state = 'succeeded'
                        AND tool_name = 'send_to_agent'
                    """,
                    (int(row["source_inbox_job_id"]),),
                ).fetchone()
                if router_origin is not None:
                    router_mailbox_id = int(router_origin["mailbox_id"])
                    failure_text = (
                        "❌ The project agent could not complete this request. "
                        "You can retry or rephrase it."
                    )
                    self.connection.execute(
                        """
                        UPDATE router_mailbox
                        SET preview_text = ?, updated_at = ?
                        WHERE mailbox_id = ?
                        """,
                        (failure_text, timestamp, router_mailbox_id),
                    )
                    self._enqueue_router_final_edit(
                        router_mailbox_id,
                        failure_text,
                        timestamp,
                        operation_suffix="agent-failed-edit",
                    )
            self.connection.execute("COMMIT")
            return state
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def enqueue_agent_response_fallback(
        self,
        mailbox_id: int,
        now: Optional[float] = None,
    ) -> int:
        timestamp = time.time() if now is None else float(now)
        row = self.connection.execute(
            """
            SELECT m.agent_id, m.response_text, b.chat_id, b.message_thread_id
            FROM agent_mailbox AS m
            JOIN agents AS a ON a.agent_id = m.agent_id
            JOIN surface_bindings AS b ON b.binding_id = a.surface_binding_id
            WHERE m.mailbox_id = ? AND m.state = 'succeeded'
                AND b.target_type = 'agent' AND b.target_id = m.agent_id
                AND b.state = 'active'
            """,
            (int(mailbox_id),),
        ).fetchone()
        if row is None or row["response_text"] is None:
            raise StoreError("Completed agent response is unavailable for fallback.")
        thread_id = int(row["message_thread_id"])
        return self.enqueue_api_call(
            operation_id=f"agent-mailbox:{mailbox_id}:final-fallback",
            method="sendMessage",
            params={
                "chat_id": int(row["chat_id"]),
                "message_thread_id": thread_id if thread_id != 0 else None,
                "text": str(row["response_text"]),
            },
            route={
                "target_type": "agent",
                "target_id": str(row["agent_id"]),
                "policy": "reply",
                "ttl_seconds": 30 * 24 * 60 * 60,
            },
            now=timestamp,
        )

    @staticmethod
    def _surface_card_from_row(row: sqlite3.Row) -> SurfaceCard:
        return SurfaceCard(
            card_id=int(row["card_id"]),
            binding_id=int(row["binding_id"]),
            card_type=str(row["card_type"]),
            callback_action_id=int(row["callback_action_id"]),
            telegram_message_id=(
                int(row["telegram_message_id"])
                if row["telegram_message_id"] is not None
                else None
            ),
            generation=int(row["generation"]),
            state=str(row["state"]),
        )

    def ensure_surface_card(
        self,
        binding_id: int,
        card_type: str,
        callback_action_id: int,
        now: Optional[float] = None,
    ) -> tuple[SurfaceCard, bool]:
        if card_type != "status":
            raise StoreError("Surface card type is invalid.")
        timestamp = time.time() if now is None else float(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT *
                FROM surface_cards
                WHERE binding_id = ? AND card_type = ?
                """,
                (int(binding_id), card_type),
            ).fetchone()
            created = False
            if row is None:
                cursor = self.connection.execute(
                    """
                    INSERT INTO surface_cards(
                        binding_id, card_type, callback_action_id,
                        telegram_message_id, generation, state,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, NULL, 1, 'pending', ?, ?)
                    """,
                    (
                        int(binding_id),
                        card_type,
                        int(callback_action_id),
                        timestamp,
                        timestamp,
                    ),
                )
                row = self.connection.execute(
                    "SELECT * FROM surface_cards WHERE card_id = ?",
                    (int(cursor.lastrowid),),
                ).fetchone()
                created = True
            elif row["state"] == "stale":
                self.connection.execute(
                    """
                    UPDATE surface_cards
                    SET callback_action_id = ?, telegram_message_id = NULL,
                        generation = generation + 1, state = 'pending',
                        updated_at = ?
                    WHERE card_id = ? AND state = 'stale'
                    """,
                    (
                        int(callback_action_id),
                        timestamp,
                        int(row["card_id"]),
                    ),
                )
                row = self.connection.execute(
                    "SELECT * FROM surface_cards WHERE card_id = ?",
                    (int(row["card_id"]),),
                ).fetchone()
                created = True
            elif int(row["callback_action_id"]) != int(callback_action_id):
                raise StoreError("Surface card callback action changed unexpectedly.")
            card = self._surface_card_from_row(row)
            self.connection.execute("COMMIT")
            return card, created
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def resolve_surface_card(
        self,
        binding_id: int,
        card_type: str = "status",
    ) -> Optional[SurfaceCard]:
        row = self.connection.execute(
            """
            SELECT *
            FROM surface_cards
            WHERE binding_id = ? AND card_type = ?
            """,
            (int(binding_id), card_type),
        ).fetchone()
        return self._surface_card_from_row(row) if row is not None else None

    def mark_surface_card_stale(
        self,
        card_id: int,
        now: Optional[float] = None,
    ) -> None:
        timestamp = time.time() if now is None else float(now)
        self.connection.execute(
            """
            UPDATE surface_cards
            SET state = 'stale', updated_at = ?
            WHERE card_id = ? AND state IN ('pending', 'active')
            """,
            (timestamp, int(card_id)),
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
        elif queue == "agent":
            cursor = self.connection.execute(
                """
                UPDATE agent_mailbox
                SET state = 'queued', attempts = 0, available_at = ?,
                    last_error = NULL, updated_at = ?
                WHERE state = 'dead'
                """,
                (timestamp, timestamp),
            )
        elif queue == "router":
            cursor = self.connection.execute(
                """
                UPDATE router_mailbox
                SET state = 'queued', attempts = 0, available_at = ?,
                    last_error = NULL, updated_at = ?
                WHERE state = 'dead'
                """,
                (timestamp, timestamp),
            )
        else:
            raise StoreError(
                "Queue must be 'inbox', 'outbox', 'agent', or 'router'."
            )
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
            ("cards", "surface_cards"),
            ("agents", "agents"),
            ("agent_mailbox", "agent_mailbox"),
            ("agent_consoles", "agent_consoles"),
            ("projects", "managed_projects"),
            ("router_mailbox", "router_mailbox"),
        ):
            if label == "updates":
                state_column = "ingest_state"
            elif label == "agents":
                state_column = "lifecycle_state"
            else:
                state_column = "state"
            rows = self.connection.execute(
                f"SELECT {state_column} AS state, COUNT(*) AS count "
                f"FROM {table} GROUP BY {state_column}"
            ).fetchall()
            result[label] = {
                str(row["state"]): int(row["count"])
                for row in rows
            }
        return result
