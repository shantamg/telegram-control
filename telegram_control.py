#!/usr/bin/python3
"""Stage 1 durable Telegram collector, inbox worker, and outbox sender."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import telegram_bridge as bridge
from durable_store import (
    SCHEMA_VERSION,
    DurableStore,
    InboxJob,
    OutboxMessage,
    StoreError,
)


DATABASE_PATH = bridge.CONFIG_DIR / "controller.sqlite3"
SCRIPT_PATH = Path(__file__).resolve()


def log_event(kind: str, **details: Any) -> None:
    record = {"time": time.time(), "kind": kind}
    record.update(details)
    print(json.dumps(record, separators=(",", ":"), sort_keys=True), flush=True)


def process_name(role: str) -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{role}:{uuid.uuid4().hex[:8]}"


def open_store(path: Path) -> DurableStore:
    store = DurableStore(path)
    try:
        store.initialize_poll_offset(bridge.read_offset())
        check = store.quick_check()
        if check != "ok":
            raise StoreError(f"SQLite quick_check returned: {check}")
        return store
    except BaseException:
        store.close()
        raise


def collect_once(
    store: DurableStore,
    token: str,
    timeout: int = 50,
) -> int:
    updates = bridge.api_call(
        token,
        "getUpdates",
        offset=store.poll_offset(),
        timeout=timeout,
        allowed_updates=["message", "callback_query"],
    )
    for update in updates:
        inserted = store.ingest_update(update)
        committed_offset = store.poll_offset()
        if committed_offset is not None:
            # Keep Stage 0's fallback cursor current, but only after the durable
            # transaction has committed. A failed mirror can cause a harmless
            # duplicate fetch; it cannot lose the durable job.
            bridge.save_offset(committed_offset)
        log_event(
            "update_ingested" if inserted else "update_duplicate",
            update_id=int(update["update_id"]),
            offset=committed_offset,
        )
    return len(updates)


def collect_command(args: argparse.Namespace) -> None:
    token = bridge.read_token()
    with open_store(args.db) as store:
        if args.once:
            collect_once(store, token, timeout=0)
            return

        delay = 1.0
        while True:
            try:
                collect_once(store, token)
                delay = 1.0
            except bridge.BridgeError as exc:
                log_event("collector_error", error=str(exc), retry_seconds=delay)
                time.sleep(delay)
                delay = min(60.0, delay * 2)


def process_inbox_job(
    store: DurableStore,
    config: dict[str, Any],
    job: InboxJob,
    worker_id: str,
) -> None:
    try:
        bridge.process_update(
            config,
            job.payload,
            extra_environment={
                "TELEGRAM_CONTROL_DB": str(store.path),
                "TELEGRAM_CONTROL_JOB_ID": str(job.job_id),
                "TELEGRAM_CONTROL_JOB_ATTEMPT": str(job.attempts),
            },
        )
        store.complete_job(job.job_id, worker_id)
        log_event(
            "inbox_succeeded",
            job_id=job.job_id,
            update_id=job.update_id,
            attempts=job.attempts,
        )
    except (bridge.BridgeError, OSError) as exc:
        state = store.fail_job(job.job_id, worker_id, str(exc))
        log_event(
            "inbox_failed",
            job_id=job.job_id,
            update_id=job.update_id,
            attempts=job.attempts,
            state=state,
            error=str(exc),
        )


def work_command(args: argparse.Namespace) -> None:
    config = bridge.load_config()
    worker_id = process_name("inbox")
    with open_store(args.db) as store:
        while True:
            job = store.claim_job(
                worker_id,
                lease_seconds=args.lease_seconds,
            )
            if job is None:
                if args.once:
                    return
                time.sleep(args.idle_sleep)
                continue
            process_inbox_job(store, config, job, worker_id)
            if args.once:
                return


def send_outbox_message(
    store: DurableStore,
    token: str,
    message: OutboxMessage,
    worker_id: str,
) -> None:
    try:
        result = bridge.api_call(token, message.method, **message.params)
        store.complete_outbox(message.message_id, worker_id, result)
        log_event(
            "outbox_sent",
            message_id=message.message_id,
            operation_id=message.operation_id,
            attempts=message.attempts,
        )
    except bridge.BridgeError as exc:
        state = store.fail_outbox(
            message.message_id,
            worker_id,
            str(exc),
        )
        log_event(
            "outbox_failed",
            message_id=message.message_id,
            operation_id=message.operation_id,
            attempts=message.attempts,
            state=state,
            error=str(exc),
        )


def send_outbox_command(args: argparse.Namespace) -> None:
    token = bridge.read_token()
    worker_id = process_name("outbox")
    with open_store(args.db) as store:
        while True:
            message = store.claim_outbox(
                worker_id,
                lease_seconds=args.lease_seconds,
            )
            if message is None:
                if args.once:
                    return
                time.sleep(args.idle_sleep)
                continue
            send_outbox_message(store, token, message, worker_id)
            if args.once:
                return


def run_command(args: argparse.Namespace) -> None:
    """Run the three independently restartable loops under a small supervisor."""
    commands = [
        [sys.executable, str(SCRIPT_PATH), "--db", str(args.db), "collect"],
        [sys.executable, str(SCRIPT_PATH), "--db", str(args.db), "work"],
        [sys.executable, str(SCRIPT_PATH), "--db", str(args.db), "send-outbox"],
    ]
    processes = [subprocess.Popen(command) for command in commands]
    log_event("supervisor_started", child_pids=[process.pid for process in processes])
    try:
        while True:
            for process in processes:
                return_code = process.poll()
                if return_code is not None:
                    raise StoreError(
                        f"Controller child {process.pid} exited with status {return_code}."
                    )
            time.sleep(1)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


def init_command(args: argparse.Namespace) -> None:
    with open_store(args.db) as store:
        print(f"Initialized schema {SCHEMA_VERSION} at {store.path}")
        print(f"Database check: {store.quick_check()}")
        print(f"Polling offset: {store.poll_offset()}")


def status_command(args: argparse.Namespace) -> None:
    with open_store(args.db) as store:
        status = {
            "database": str(store.path),
            "quick_check": store.quick_check(),
            "poll_offset": store.poll_offset(),
            "queues": store.status_counts(),
        }
    print(json.dumps(status, indent=2, sort_keys=True))


def doctor_command(args: argparse.Namespace) -> None:
    problems = []
    try:
        config = bridge.load_config()
        print(f"Pairing: chat {config['chat_id']}")
    except bridge.BridgeError as exc:
        problems.append(str(exc))
    try:
        bridge.read_token()
        print("Keychain token: available")
    except bridge.BridgeError as exc:
        problems.append(str(exc))
    try:
        with open_store(args.db) as store:
            check = store.quick_check()
            if check != "ok":
                problems.append(f"SQLite quick_check returned: {check}")
            else:
                print(f"Database: ok ({store.path})")
    except (OSError, sqlite3.Error, StoreError) as exc:
        problems.append(str(exc))
    if problems:
        for problem in problems:
            print(f"Problem: {problem}", file=sys.stderr)
        raise StoreError(f"Doctor found {len(problems)} problem(s).")
    print("Doctor: all Stage 1 prerequisites passed.")


def retry_command(args: argparse.Namespace) -> None:
    with open_store(args.db) as store:
        count = store.retry_dead(args.queue)
    print(f"Requeued {count} dead {args.queue} item(s).")


def add_loop_arguments(parser: argparse.ArgumentParser, lease_seconds: float) -> None:
    parser.add_argument("--once", action="store_true", help="Process at most one item.")
    parser.add_argument(
        "--lease-seconds",
        type=float,
        default=lease_seconds,
        help="Seconds before a crashed worker's lease can be recovered.",
    )
    parser.add_argument("--idle-sleep", type=float, default=0.5)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Durable Telegram transport controller.")
    parser.add_argument(
        "--db",
        type=Path,
        default=DATABASE_PATH,
        help=f"SQLite database path (default: {DATABASE_PATH})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize and verify the database.")
    init_parser.set_defaults(function=init_command)

    collect_parser = subparsers.add_parser(
        "collect", help="Durably collect Telegram updates."
    )
    collect_parser.add_argument(
        "--once", action="store_true", help="Make one non-blocking Telegram request."
    )
    collect_parser.set_defaults(function=collect_command)

    work_parser = subparsers.add_parser("work", help="Process durable inbox jobs.")
    add_loop_arguments(work_parser, lease_seconds=20 * 60)
    work_parser.set_defaults(function=work_command)

    outbox_parser = subparsers.add_parser(
        "send-outbox", help="Deliver durable Telegram API calls."
    )
    add_loop_arguments(outbox_parser, lease_seconds=90)
    outbox_parser.set_defaults(function=send_outbox_command)

    run_parser = subparsers.add_parser(
        "run", help="Run collector, inbox worker, and outbox sender."
    )
    run_parser.set_defaults(function=run_command)

    status_parser = subparsers.add_parser("status", help="Show durable queue status.")
    status_parser.set_defaults(function=status_command)

    doctor_parser = subparsers.add_parser("doctor", help="Check Stage 1 prerequisites.")
    doctor_parser.set_defaults(function=doctor_command)

    retry_parser = subparsers.add_parser("retry", help="Requeue dead items.")
    retry_parser.add_argument("queue", choices=("inbox", "outbox"))
    retry_parser.set_defaults(function=retry_command)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.function(args)
        return 0
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    except (bridge.BridgeError, sqlite3.Error, StoreError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
