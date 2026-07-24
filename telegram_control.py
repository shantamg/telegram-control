#!/usr/bin/python3
"""Stage 1 durable Telegram collector, inbox worker, and outbox sender."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import telegram_bridge as bridge
import provider_adapters
import tmux_console
from durable_store import (
    SCHEMA_VERSION,
    AgentMailboxJob,
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


def chunk_telegram_text(text: str, limit: int = 3800) -> list[str]:
    remaining = text.strip() or "[empty agent response]"
    chunks = []
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks


def process_agent_mailbox_job(
    store: DurableStore,
    job: AgentMailboxJob,
    worker_id: str,
) -> None:
    try:
        agent = store.resolve_agent(job.agent_id)
        if agent is None:
            raise StoreError("Managed agent no longer exists.")
        adapter = provider_adapters.adapter_for(agent)
        result = adapter.run_turn(
            agent,
            job.input_text,
            job.provider_session_id,
            on_session=lambda session_id: store.attach_agent_mailbox_session(
                job.mailbox_id,
                worker_id,
                session_id,
            ),
            heartbeat=lambda: store.heartbeat_agent_mailbox(
                job.mailbox_id,
                worker_id,
            ),
        )
        store.complete_agent_mailbox(
            job.mailbox_id,
            worker_id,
            result.provider_session_id,
            result.final_text,
            chunk_telegram_text(result.final_text),
            result.usage,
        )
        log_event(
            "agent_turn_succeeded",
            mailbox_id=job.mailbox_id,
            agent_id=job.agent_id,
            attempts=job.attempts,
            provider_session_id=result.provider_session_id,
        )
    except (provider_adapters.ProviderAdapterError, OSError, StoreError) as exc:
        state = store.fail_agent_mailbox(
            job.mailbox_id,
            worker_id,
            str(exc),
        )
        log_event(
            "agent_turn_failed",
            mailbox_id=job.mailbox_id,
            agent_id=job.agent_id,
            attempts=job.attempts,
            state=state,
            error=str(exc),
        )


def work_agents_command(args: argparse.Namespace) -> None:
    worker_id = process_name("agent")
    with open_store(args.db) as store:
        while True:
            job = store.claim_agent_mailbox(
                worker_id,
                lease_seconds=args.lease_seconds,
            )
            if job is None:
                if args.once:
                    return
                time.sleep(args.idle_sleep)
                continue
            process_agent_mailbox_job(store, job, worker_id)
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
        error = str(exc)
        permanent_card_edit_failure = (
            message.method == "editMessageText"
            and message.card is not None
            and message.card.get("mode") == "edit"
            and any(
                marker in error.lower()
                for marker in (
                    "message to edit not found",
                    "message can't be edited",
                    "message_id_invalid",
                )
            )
        )
        state = store.fail_outbox(
            message.message_id,
            worker_id,
            error,
            max_attempts=message.attempts if permanent_card_edit_failure else 8,
        )
        if permanent_card_edit_failure and state == "dead":
            store.mark_surface_card_stale(int(message.card["card_id"]))
        log_event(
            "outbox_failed",
            message_id=message.message_id,
            operation_id=message.operation_id,
            attempts=message.attempts,
            state=state,
            error=error,
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
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def stop_on_sigterm(_: int, __: Any) -> None:
        # launchd uses SIGTERM for controlled restarts. SystemExit still runs
        # the cleanup block but does not write a false alarm to the error log.
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_on_sigterm)
    commands = [
        [sys.executable, str(SCRIPT_PATH), "--db", str(args.db), "collect"],
        [sys.executable, str(SCRIPT_PATH), "--db", str(args.db), "work"],
        [sys.executable, str(SCRIPT_PATH), "--db", str(args.db), "work-agents"],
        [sys.executable, str(SCRIPT_PATH), "--db", str(args.db), "send-outbox"],
    ]
    processes: list[subprocess.Popen[Any]] = []
    try:
        for command in commands:
            processes.append(subprocess.Popen(command))
        log_event(
            "supervisor_started",
            child_pids=[process.pid for process in processes],
        )
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
        signal.signal(signal.SIGTERM, previous_sigterm)


def launch_agent_plist(database_path: Path = DATABASE_PATH) -> dict[str, Any]:
    return {
        "Label": bridge.LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            sys.executable,
            str(SCRIPT_PATH),
            "--db",
            str(database_path),
            "run",
        ],
        "WorkingDirectory": str(SCRIPT_PATH.parent),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "ProcessType": "Background",
        "StandardOutPath": str(bridge.LOG_DIR / "telegram-control.log"),
        "StandardErrorPath": str(bridge.LOG_DIR / "telegram-control.error.log"),
    }


def write_plist(path: Path, value: dict[str, Any]) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("wb") as plist_file:
        plistlib.dump(value, plist_file)
    bridge.ensure_private_file(temporary_path)
    temporary_path.replace(path)
    bridge.ensure_private_file(path)


def configured_launch_agent_mode() -> str:
    try:
        with bridge.PLIST_PATH.open("rb") as plist_file:
            plist = plistlib.load(plist_file)
    except (FileNotFoundError, OSError, ValueError):
        return "missing"
    arguments = [str(value) for value in plist.get("ProgramArguments", [])]
    if str(SCRIPT_PATH) in arguments and "run" in arguments:
        return "durable"
    if str(bridge.SCRIPT_PATH) in arguments and "listen" in arguments:
        return "stage0"
    return "unknown"


def install_command(args: argparse.Namespace) -> None:
    config = bridge.load_config()
    bridge.read_token()
    handler_path = Path(config["handler_path"]).expanduser().resolve()
    if not handler_path.is_file():
        raise StoreError(f"Handler does not exist: {handler_path}")
    bridge.handler_command(handler_path)
    with open_store(args.db):
        pass

    bridge.LOG_DIR.mkdir(parents=True, exist_ok=True)
    bridge.PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    previous_plist: Optional[bytes]
    try:
        previous_plist = bridge.PLIST_PATH.read_bytes()
    except FileNotFoundError:
        previous_plist = None
    was_loaded = (
        bridge.launchctl(
            "print",
            f"{domain}/{bridge.LAUNCH_AGENT_LABEL}",
            check=False,
        ).returncode
        == 0
    )

    bridge.launchctl("bootout", domain, str(bridge.PLIST_PATH), check=False)
    write_plist(bridge.PLIST_PATH, launch_agent_plist(args.db))
    result = bridge.launchctl(
        "bootstrap",
        domain,
        str(bridge.PLIST_PATH),
        check=False,
    )
    if result.returncode != 0:
        bridge.launchctl("bootout", domain, str(bridge.PLIST_PATH), check=False)
        if previous_plist is None:
            bridge.PLIST_PATH.unlink(missing_ok=True)
        else:
            temporary_path = bridge.PLIST_PATH.with_suffix(".rollback.tmp")
            temporary_path.write_bytes(previous_plist)
            bridge.ensure_private_file(temporary_path)
            temporary_path.replace(bridge.PLIST_PATH)
            if was_loaded:
                bridge.launchctl(
                    "bootstrap",
                    domain,
                    str(bridge.PLIST_PATH),
                    check=False,
                )
        raise StoreError(
            result.stderr.strip()
            or "launchctl could not install the durable controller."
        )

    bridge.launchctl(
        "kickstart",
        "-k",
        f"{domain}/{bridge.LAUNCH_AGENT_LABEL}",
        check=False,
    )
    print("Durable Telegram controller installed and started.")
    print(f"Database: {args.db}")
    print(f"Logs: {bridge.LOG_DIR / 'telegram-control.log'}")
    print(f"Errors: {bridge.LOG_DIR / 'telegram-control.error.log'}")
    print(
        "Rollback: "
        f"{bridge.SCRIPT_PATH} install --handler {handler_path}"
    )


def init_command(args: argparse.Namespace) -> None:
    with open_store(args.db) as store:
        print(f"Initialized schema {SCHEMA_VERSION} at {store.path}")
        print(f"Database check: {store.quick_check()}")
        print(f"Polling offset: {store.poll_offset()}")


def status_command(args: argparse.Namespace) -> None:
    domain = f"gui/{os.getuid()}"
    launch_result = bridge.launchctl(
        "print",
        f"{domain}/{bridge.LAUNCH_AGENT_LABEL}",
        check=False,
    )
    with open_store(args.db) as store:
        status = {
            "database": str(store.path),
            "quick_check": store.quick_check(),
            "poll_offset": store.poll_offset(),
            "launch_agent": {
                "configured_mode": configured_launch_agent_mode(),
                "loaded": launch_result.returncode == 0,
            },
            "queues": store.status_counts(),
        }
    print(json.dumps(status, indent=2, sort_keys=True))


def topic_capability_command(_: argparse.Namespace) -> None:
    bot = bridge.api_call(bridge.read_token(), "getMe")
    capability = {
        "username": bot.get("username"),
        "has_topics_enabled": bool(bot.get("has_topics_enabled", False)),
        "allows_users_to_create_topics": bool(
            bot.get("allows_users_to_create_topics", False)
        ),
    }
    print(json.dumps(capability, indent=2, sort_keys=True))


def provision_topic_command(args: argparse.Namespace) -> None:
    name = str(args.name).strip()
    if not name or len(name) > 128:
        raise StoreError("Topic name must contain between 1 and 128 characters.")
    config = bridge.load_config()
    chat_id = int(config["chat_id"])
    with open_store(args.db) as store:
        existing = store.resolve_named_surface(
            chat_id,
            name,
            surface_type=args.surface_type,
        )
        if existing is not None:
            result = {
                "created": False,
                "binding_id": existing.binding_id,
                "chat_id": existing.chat_id,
                "message_thread_id": existing.message_thread_id,
                "display_name": existing.display_name,
                "target": f"{existing.target_type}/{existing.target_id}",
            }
        else:
            token = bridge.read_token()
            bot = bridge.api_call(token, "getMe")
            if not bool(bot.get("has_topics_enabled", False)):
                raise StoreError(
                    "Telegram Threaded Mode is disabled for this bot. "
                    "Enable it in BotFather first."
                )
            topic = bridge.api_call(
                token,
                "createForumTopic",
                chat_id=chat_id,
                name=name,
            )
            try:
                message_thread_id = int(topic["message_thread_id"])
            except (KeyError, TypeError, ValueError):
                raise StoreError(
                    "Telegram returned an invalid forum-topic result."
                ) from None
            binding = store.ensure_surface_binding(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                surface_type=args.surface_type,
                display_name=name,
                target_type=args.target_type,
                target_id=args.target_id,
            )
            result = {
                "created": True,
                "binding_id": binding.binding_id,
                "chat_id": binding.chat_id,
                "message_thread_id": binding.message_thread_id,
                "display_name": binding.display_name,
                "target": f"{binding.target_type}/{binding.target_id}",
            }
    print(json.dumps(result, indent=2, sort_keys=True))


def register_agent_command(args: argparse.Namespace) -> None:
    requested_path = Path(args.project_path).expanduser().resolve()
    if not requested_path.is_dir():
        raise StoreError(f"Project directory does not exist: {requested_path}")
    git_result = subprocess.run(
        ["git", "-C", str(requested_path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if git_result.returncode != 0:
        raise StoreError("Managed Codex projects must be Git repositories.")
    project_path = str(Path(git_result.stdout.strip()).resolve())
    config = bridge.load_config()
    with open_store(args.db) as store:
        agent, created = store.register_project_agent(
            chat_id=int(config["chat_id"]),
            surface_name=args.surface,
            slug=args.slug,
            provider=args.provider,
            project_path=project_path,
        )
    print(
        json.dumps(
            {
                "created": created,
                "agent_id": agent.agent_id,
                "name": agent.hierarchical_name,
                "provider": agent.provider,
                "project_path": agent.project_path,
                "state": agent.lifecycle_state,
                "surface_binding_id": agent.surface_binding_id,
            },
            indent=2,
            sort_keys=True,
        )
    )


def resolve_cli_agent(store: DurableStore, name: str):
    agent = store.resolve_agent_by_name(name)
    if agent is None or agent.role not in {"project", "worker"}:
        raise StoreError(f"Managed agent was not found: {name}")
    return agent


def console_open_command(args: argparse.Namespace) -> None:
    with open_store(args.db) as store:
        agent = resolve_cli_agent(store, args.agent)
        console = tmux_console.open_agent_console(store, agent)
    print(f"Managed console running: {console.tmux_session_name}")
    print(f"Attach: tmux attach-session -t '={console.tmux_session_name}'")


def console_close_command(args: argparse.Namespace) -> None:
    with open_store(args.db) as store:
        agent = resolve_cli_agent(store, args.agent)
        console = tmux_console.close_agent_console(store, agent)
    print(f"Managed console stopped: {console.tmux_session_name}")


def console_status_command(args: argparse.Namespace) -> None:
    with open_store(args.db) as store:
        agent = resolve_cli_agent(store, args.agent)
        console = tmux_console.reconcile_agent_console(store, agent.agent_id)
    state = console.state if console is not None else "not created"
    session_name = (
        console.tmux_session_name if console is not None else agent.hierarchical_name
    )
    print(
        json.dumps(
            {
                "agent": agent.hierarchical_name,
                "console": state,
                "tmux_session": session_name,
            },
            indent=2,
            sort_keys=True,
        )
    )


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

    agent_work_parser = subparsers.add_parser(
        "work-agents",
        help="Process serialized managed-agent mailbox items.",
    )
    add_loop_arguments(agent_work_parser, lease_seconds=2 * 60 * 60)
    agent_work_parser.set_defaults(function=work_agents_command)

    outbox_parser = subparsers.add_parser(
        "send-outbox", help="Deliver durable Telegram API calls."
    )
    add_loop_arguments(outbox_parser, lease_seconds=90)
    outbox_parser.set_defaults(function=send_outbox_command)

    run_parser = subparsers.add_parser(
        "run", help="Run collector, inbox worker, and outbox sender."
    )
    run_parser.set_defaults(function=run_command)

    install_parser = subparsers.add_parser(
        "install", help="Replace Stage 0 with the durable background controller."
    )
    install_parser.set_defaults(function=install_command)

    status_parser = subparsers.add_parser("status", help="Show durable queue status.")
    status_parser.set_defaults(function=status_command)

    topic_parser = subparsers.add_parser(
        "topic-capability",
        help="Show whether Telegram private-chat topics are enabled for this bot.",
    )
    topic_parser.set_defaults(function=topic_capability_command)

    provision_parser = subparsers.add_parser(
        "provision-topic",
        help="Create and bind one managed private-chat topic.",
    )
    provision_parser.add_argument("name", help="Telegram topic and surface name.")
    provision_parser.add_argument(
        "--surface-type",
        choices=("project", "task"),
        default="project",
    )
    provision_parser.add_argument("--target-type", default="controller")
    provision_parser.add_argument("--target-id", default="control")
    provision_parser.set_defaults(function=provision_topic_command)

    register_parser = subparsers.add_parser(
        "register-agent",
        help="Register a managed project agent on an existing topic surface.",
    )
    register_parser.add_argument("surface", help="Existing managed topic name.")
    register_parser.add_argument("slug", help="Lowercase hierarchical agent slug.")
    register_parser.add_argument("project_path", help="Local Git project directory.")
    register_parser.add_argument(
        "--provider",
        choices=("codex", "claude"),
        default="codex",
    )
    register_parser.set_defaults(function=register_agent_command)

    console_open_parser = subparsers.add_parser(
        "console-open",
        help="Open an explicit tmux takeover for a persisted agent session.",
    )
    console_open_parser.add_argument("agent", help="Hierarchical managed-agent name.")
    console_open_parser.set_defaults(function=console_open_command)

    console_close_parser = subparsers.add_parser(
        "console-close",
        help="Close a managed tmux console and resume mailbox processing.",
    )
    console_close_parser.add_argument("agent", help="Hierarchical managed-agent name.")
    console_close_parser.set_defaults(function=console_close_command)

    console_status_parser = subparsers.add_parser(
        "console-status",
        help="Show and reconcile a managed tmux console.",
    )
    console_status_parser.add_argument("agent", help="Hierarchical managed-agent name.")
    console_status_parser.set_defaults(function=console_status_command)

    doctor_parser = subparsers.add_parser("doctor", help="Check Stage 1 prerequisites.")
    doctor_parser.set_defaults(function=doctor_command)

    retry_parser = subparsers.add_parser("retry", help="Requeue dead items.")
    retry_parser.add_argument("queue", choices=("inbox", "outbox", "agent"))
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
