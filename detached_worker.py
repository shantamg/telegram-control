#!/usr/bin/python3
"""Detached tmux workers that report into a topic of their own.

A managed turn is one-shot: the provider process is torn down once the turn
replies. Work that needs to keep running — a long refactor, an adversarial
review loop — cannot live inside one. A detached worker is a tmux session
started by a managed turn and deliberately outliving it, because the tmux
server is a daemon rather than a child of the turn.

That raises a delivery problem. `agent_telegram.py` can only post from the one
live managed turn: it resolves its destination through a leased mailbox, and
each turn gets a fresh lease. A detached worker will never hold one.

The answer is not to fake a lease but to give the worker somewhere of its own
to speak. Each worker gets a sibling topic in the same group, and that topic
is report-only: no managed agent owns it, so there is no live turn to talk
over and the one-turn-speaks rule simply does not apply there. The project's
main topic stays conversational — the operator talks to the main agent, which
can relay to the worker — and an inbound message to a worker topic is answered
by policy rather than routed (see `report_only_notice`).

Provider specifics stay behind the adapter protocol: this module never names
Claude or Codex.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import claude_sessions
import codex_sessions
import provider_adapters
import telegram_bridge
import tmux_console
import voice_responses
from durable_store import DetachedWorker, DurableStore, StoreError, SurfaceBinding

NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MAX_REPORT_CHARACTERS = 3_500

# A worker that dies immediately should not be respawned forever. Restarting
# is a recovery mechanism, not a supervisor loop.
MAX_RESTARTS = 3
RESTART_BACKOFF_SECONDS = 30
RECOVERY_CONFIRM_TIMEOUT_SECONDS = 30 * 60
SESSION_DISCOVERY_TIMEOUT_SECONDS = 15
SESSION_DISCOVERY_POLL_SECONDS = 0.25
BRIEF_SUBMIT_DELAY_SECONDS = 0.2

DEFAULT_RECOVERY_PROMPT = """You have been automatically resumed after the host or detached session stopped unexpectedly.

Check whether your scheduled tasks, wakeups, loops, monitors, and background work are still active, and recreate anything that is missing. Do not repeat side effects that already completed.

Then run the recovery-confirm command below with a short plain-text summary on standard input, or the recovery-fail command if you cannot safely continue. Do not claim recovery merely because this session opened."""

RECOVERY_FILE_TEMPLATE = """# Detached Worker Recovery State

This file is the durable recovery inventory for a Telegram Control detached worker.

## Current goal

No goal recorded yet.

## State that must be reactivated

Nothing recorded yet.

## Native wakeups and scheduled tasks

None recorded yet.

## Background agents, monitors, and processes

None recorded yet.

## Durable artifacts and identifiers

None recorded yet.

## Recovery procedure and verification

Review the active goal and state above. Reconcile current time and external state before recreating anything. Do not repeat completed side effects.

## Last updated

Created automatically by Telegram Control. The detached agent must update this whenever recovery-relevant state changes.
"""


def _validate_name(name: str) -> str:
    if not NAME_PATTERN.fullmatch(name) or len(name) > 48:
        raise StoreError(
            "Worker name must be 1 to 48 lowercase letters, numbers or hyphens."
        )
    return name


def tmux_session_name(name: str) -> str:
    return f"detached--{name}"


def topic_name(name: str) -> str:
    return f"{name} updates"


def recovery_file_path(store: DurableStore, name: str) -> Path:
    return store.path.parent / "detached-workers" / _validate_name(name) / "RECOVERY.md"


def ensure_recovery_file(store: DurableStore, name: str) -> Path:
    path = recovery_file_path(store, name)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        path.write_text(RECOVERY_FILE_TEMPLATE, encoding="utf-8")
        path.chmod(0o600)
    return path


def remove_recovery_file(store: DurableStore, worker: DetachedWorker) -> bool:
    """Remove only the exact harness-owned recovery file at worker teardown."""
    expected = recovery_file_path(store, worker.name)
    if worker.recovery_file_path and Path(worker.recovery_file_path) != expected:
        raise StoreError(
            "Detached worker recovery path does not match its managed location."
        )
    removed = False
    try:
        expected.unlink()
        removed = True
    except FileNotFoundError:
        pass
    try:
        expected.parent.rmdir()
    except OSError:
        # Preserve unexpected companion files instead of recursively deleting
        # a directory whose contents the controller does not own.
        pass
    return removed


def launch_preamble(name: str) -> str:
    """The one thing a worker needs to be told at launch.

    Workers used to be handed a recovery contract here: read this file, keep an
    inventory of every wakeup and background agent in it, update it on every
    change. That was written on the assumption that a resumed session came back
    empty and had to rebuild itself from notes. It does not — resuming the exact
    session ID restores its scheduled work, so the inventory was a hand-kept
    copy of state the harness already had. Maintaining it cost far more context
    than recovery ever did.
    """
    return (
        f"You are detached worker '{name}', running in tmux so your work "
        "survives after the turn that started you has ended. Use your own "
        "native scheduling, wakeup, loop, and background features as normal; "
        "nothing here replaces them. Wait for the task brief."
    )


def create_worker(
    store: DurableStore,
    *,
    name: str,
    binding_id: int,
    project_path: str,
    provider: str,
    working_directory: Optional[str] = None,
    provider_config: Optional[dict[str, Any]] = None,
    origin_agent_id: Optional[str] = None,
) -> DetachedWorker:
    """Record the worker, then start its tmux session.

    Recorded first on purpose: a crash between the row and the session leaves
    something reconciliation can see and clean up, whereas the reverse order
    leaves an orphan tmux session nothing knows about.
    """
    _validate_name(name)
    directory = working_directory or project_path
    if not Path(directory).is_dir():
        raise StoreError("Detached worker working directory is unavailable.")

    session_name = tmux_session_name(name)
    if tmux_console.has_tmux_session(session_name):
        raise StoreError(f"A tmux session already uses the name {session_name}.")

    config = dict(provider_config or {})
    recovery_file = ensure_recovery_file(store, name)
    baseline_sessions = _session_ids_for(provider, directory)
    provider_session_id = (
        str(uuid.uuid4()) if provider == "claude" else None
    )
    command = launch_command_for(
        provider,
        directory,
        config,
        provider_session_id=provider_session_id,
    )
    command.append(launch_preamble(name))

    worker = store.create_detached_worker(
        name=name,
        binding_id=binding_id,
        project_path=project_path,
        provider=provider,
        tmux_session_name=session_name,
        provider_session_id=provider_session_id,
        provider_config=config,
        working_directory=directory,
        recovery_file_path=str(recovery_file),
        recovery_prompt=DEFAULT_RECOVERY_PROMPT,
        origin_agent_id=origin_agent_id,
    )
    try:
        _start_session(session_name, directory, command)
    except BaseException:
        store.set_detached_worker_states(
            name,
            intended_state="stopped",
            observed_state="stopped",
        )
        raise
    if provider_session_id is None:
        try:
            provider_session_id = _wait_for_new_session(
                provider,
                directory,
                baseline_sessions,
            )
            worker = store.configure_detached_worker_recovery(
                name,
                provider_session_id=provider_session_id,
            )
        except BaseException:
            _kill_session(session_name)
            store.set_detached_worker_states(
                name,
                intended_state="stopped",
                observed_state="stopped",
            )
            raise
    return store.set_detached_worker_states(name, observed_state="running")


def launch_command_for(
    provider: str,
    working_directory: str,
    provider_config: dict[str, Any],
    *,
    provider_session_id: Optional[str] = None,
) -> list[str]:
    """Ask the provider's adapter how to start a fresh session.

    Adapter-blind by design: a new provider becomes available here by
    implementing the protocol, not by adding a branch.
    """
    adapter = provider_adapters.adapter_for(_provider_stub(provider))
    if not adapter.capabilities().interactive_console:
        raise StoreError(
            f"Detached workers are not implemented for provider: {provider}"
        )
    try:
        return adapter.detached_launch_command(
            working_directory,
            provider_config,
            provider_session_id,
        )
    except provider_adapters.ProviderAdapterError as error:
        raise StoreError(str(error)) from error


class _provider_stub:
    """Minimal stand-in so adapter_for can dispatch on provider alone.

    A detached worker is not a registered agent — it has no mailbox, no
    persisted session and no enrolled workspace — but adapter selection only
    ever reads `.provider`.
    """

    def __init__(self, provider: str) -> None:
        self.provider = provider


class _detached_agent_stub:
    """The fields provider console adapters need to construct resume argv."""

    def __init__(self, worker: DetachedWorker) -> None:
        self.provider = worker.provider
        self.provider_session_id = worker.provider_session_id
        self.provider_config = worker.provider_config
        self.project_path = worker.project_path
        self.working_directory = worker.working_directory


def _start_session(session_name: str, directory: str, command: list[str]) -> None:
    result = subprocess.run(
        [
            tmux_console.tmux_binary(),
            "new-session",
            "-d",
            "-s",
            session_name,
            "-c",
            directory,
            *command,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise StoreError(
            result.stderr.strip() or "tmux could not start the detached worker."
        )


def _kill_session(session_name: str) -> None:
    if not tmux_console.has_tmux_session(session_name):
        return
    subprocess.run(
        [
            tmux_console.tmux_binary(),
            "kill-session",
            "-t",
            f"={session_name}",
        ],
        capture_output=True,
        text=True,
    )


def _session_ids_for(provider: str, working_directory: str) -> set[str]:
    if provider == "claude":
        sessions = claude_sessions.discover_sessions(
            working_directory,
            limit=100,
        )
    elif provider == "codex":
        sessions = codex_sessions.discover_sessions(
            working_directory,
            limit=100,
        )
    else:
        return set()
    return {session.session_id for session in sessions}


def _wait_for_new_session(
    provider: str,
    working_directory: str,
    baseline_session_ids: set[str],
) -> str:
    deadline = time.monotonic() + SESSION_DISCOVERY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        candidates = _session_ids_for(provider, working_directory) - baseline_session_ids
        if len(candidates) == 1:
            return next(iter(candidates))
        if len(candidates) > 1:
            raise StoreError(
                "More than one new provider session appeared; recovery identity "
                "could not be chosen safely."
            )
        time.sleep(SESSION_DISCOVERY_POLL_SECONDS)
    raise StoreError(
        "The provider session ID was not persisted before the recovery timeout."
    )


def validate_provider_session(worker: DetachedWorker) -> bool:
    if not worker.provider_session_id:
        return False
    if worker.provider == "claude":
        return (
            claude_sessions.resolve_session(
                worker.provider_session_id,
                worker.working_directory,
            )
            is not None
        )
    if worker.provider == "codex":
        return (
            codex_sessions.resolve_session(
                worker.provider_session_id,
                worker.working_directory,
            )
            is not None
        )
    return False


def resume_command_for(worker: DetachedWorker, prompt: str) -> list[str]:
    if not worker.provider_session_id:
        raise StoreError("Detached worker has no persisted provider session.")
    adapter = provider_adapters.adapter_for(_detached_agent_stub(worker))
    try:
        command = adapter.console_command(_detached_agent_stub(worker))
    except provider_adapters.ProviderAdapterError as error:
        raise StoreError(str(error)) from error
    command.append(prompt)
    return command


def send_brief(name: str, brief: str) -> None:
    """Type a brief into the worker's session and submit it.

    Sent as a separate step from the launch so the provider has finished
    starting before it receives instructions, and so a caller can send further
    guidance later through the same path.
    """
    session = tmux_session_name(_validate_name(name))
    if not tmux_console.has_tmux_session(session):
        raise StoreError("Detached worker session is not running.")
    tmux = tmux_console.tmux_binary()
    subprocess.run(
        [tmux, "send-keys", "-t", session, brief],
        capture_output=True,
        text=True,
        check=False,
    )
    # Interactive providers process bracketed paste asynchronously. Without a
    # small gap, Enter can arrive before the pasted brief becomes submit-ready
    # and leave the entire task sitting unsent in the prompt.
    time.sleep(BRIEF_SUBMIT_DELAY_SECONDS)
    subprocess.run(
        [tmux, "send-keys", "-t", session, "Enter"],
        capture_output=True,
        text=True,
        check=False,
    )


def reconcile_worker(store: DurableStore, name: str) -> Optional[DetachedWorker]:
    """Bring observed state back in line with reality.

    Intended state is left alone: this answers "is it running", never
    "should it be".
    """
    worker = store.resolve_detached_worker(name)
    if worker is None:
        return None
    alive = tmux_console.has_tmux_session(worker.tmux_session_name)
    if (
        alive
        and worker.recovery_state != "recovering"
        and worker.observed_state != "running"
    ):
        return store.set_detached_worker_states(name, observed_state="running")
    if not alive and worker.observed_state != "stopped":
        return store.set_detached_worker_states(name, observed_state="stopped")
    return worker


def _recovery_prompt(store: DurableStore, worker: DetachedWorker) -> str:
    generation = worker.recovery_generation
    controller = Path(__file__).resolve().with_name("telegram_control.py")
    prefix = (
        f"/usr/bin/python3 {shlex.quote(str(controller))} "
        f"--db {shlex.quote(str(store.path))}"
    )
    confirm = (
        f"{prefix} worker-recovery-confirm {worker.name} "
        f"--generation {generation}"
    )
    fail = (
        f"{prefix} worker-recovery-fail {worker.name} "
        f"--generation {generation}"
    )
    base = worker.recovery_prompt.strip() or DEFAULT_RECOVERY_PROMPT
    return (
        f"{base}\n\n"
        f"Recovery generation: {generation}\n\n"
        "Success command (summary on standard input):\n"
        f"{confirm}\n\n"
        "Failure command (reason on standard input):\n"
        f"{fail}"
    )


def enqueue_recovery_report(
    store: DurableStore,
    worker: DetachedWorker,
    *,
    phase: str,
    text: str,
) -> int:
    chat_id, thread_id = resolve_destination(store, worker.name)
    return store.enqueue_api_call(
        operation_id=(
            f"detached-worker:{worker.worker_id}:recovery:"
            f"{worker.recovery_generation}:{phase}"
        ),
        method="sendMessage",
        params={
            "chat_id": chat_id,
            "message_thread_id": thread_id or None,
            "text": str(text).strip(),
        },
        serialize_key=f"detached-worker-recovery:{worker.worker_id}",
    )


def recover_worker(
    store: DurableStore,
    name: str,
    *,
    now: Optional[float] = None,
) -> tuple[str, DetachedWorker]:
    """Reconcile one worker and, when necessary, resume its provider session."""
    timestamp = time.time() if now is None else float(now)
    worker = store.resolve_detached_worker(_validate_name(name))
    if worker is None:
        raise StoreError("Detached worker was not found.")
    alive = tmux_console.has_tmux_session(worker.tmux_session_name)

    if worker.intended_state != "running":
        if not alive and worker.observed_state != "stopped":
            worker = store.set_detached_worker_states(
                worker.name,
                observed_state="stopped",
                now=timestamp,
            )
        return "stopped", worker

    if worker.recovery_state == "recovering":
        if alive:
            started_at = worker.recovery_started_at or timestamp
            if timestamp - started_at <= RECOVERY_CONFIRM_TIMEOUT_SECONDS:
                return "awaiting_confirmation", worker
            _kill_session(worker.tmux_session_name)
            worker = store.fail_detached_worker_recovery(
                worker.name,
                worker.recovery_generation,
                "The resumed agent did not confirm restoration before the timeout.",
                now=timestamp,
            )
            enqueue_recovery_report(
                store,
                worker,
                phase="timeout",
                text=(
                    f"⚠️ Recovery attempt {worker.restart_count} for "
                    f"'{worker.name}' timed out before the agent confirmed that "
                    "its state and native background work were restored."
                ),
            )
            return "failed", worker
        worker = store.fail_detached_worker_recovery(
            worker.name,
            worker.recovery_generation,
            "The resumed provider process exited before confirming restoration.",
            now=timestamp,
        )
        enqueue_recovery_report(
            store,
            worker,
            phase="process-exited",
            text=(
                f"⚠️ Recovery attempt {worker.restart_count} for "
                f"'{worker.name}' failed because the resumed provider process "
                "exited before confirming restoration."
            ),
        )
        return "failed", worker

    if alive:
        if worker.observed_state != "running":
            worker = store.set_detached_worker_states(
                worker.name,
                observed_state="running",
                now=timestamp,
            )
        return "running", worker

    if worker.observed_state != "stopped":
        worker = store.set_detached_worker_states(
            worker.name,
            observed_state="stopped",
            now=timestamp,
        )

    if worker.restart_count >= MAX_RESTARTS:
        if worker.last_recovery_error != "Automatic recovery attempts exhausted.":
            worker = store.fail_detached_worker_recovery(
                worker.name,
                worker.recovery_generation,
                "Automatic recovery attempts exhausted.",
                now=timestamp,
            )
            enqueue_recovery_report(
                store,
                worker,
                phase="exhausted",
                text=(
                    f"❌ Detached worker '{worker.name}' could not be recovered "
                    f"after {worker.restart_count} attempts. Its provider session "
                    "record is preserved for manual recovery."
                ),
            )
        return "exhausted", worker

    if (
        worker.last_restart_at is not None
        and timestamp - worker.last_restart_at < RESTART_BACKOFF_SECONDS
    ):
        return "backoff", worker

    if not worker.provider_session_id:
        message = "No provider session ID was recorded for automatic recovery."
        if worker.last_recovery_error != message:
            worker = store.fail_detached_worker_recovery(
                worker.name,
                worker.recovery_generation,
                message,
                now=timestamp,
            )
            enqueue_recovery_report(
                store,
                worker,
                phase="unavailable",
                text=(
                    f"❌ Detached worker '{worker.name}' stopped, but Telegram "
                    "Control cannot recover it automatically because its provider "
                    "session ID was not recorded. The worker remains intended "
                    "running for manual repair."
                ),
            )
        return "unavailable", worker

    if not validate_provider_session(worker):
        message = "The persisted provider session could not be found locally."
        if worker.last_recovery_error != message:
            worker = store.fail_detached_worker_recovery(
                worker.name,
                worker.recovery_generation,
                message,
                now=timestamp,
            )
            enqueue_recovery_report(
                store,
                worker,
                phase="session-missing",
                text=(
                    f"❌ Detached worker '{worker.name}' stopped, and its "
                    f"{worker.provider} session could not be found locally. "
                    "Automatic recovery was not attempted."
                ),
            )
        return "unavailable", worker

    worker = store.begin_detached_worker_recovery(worker.name, now=timestamp)
    try:
        command = resume_command_for(worker, _recovery_prompt(store, worker))
        _start_session(
            worker.tmux_session_name,
            worker.working_directory,
            command,
        )
    except BaseException as error:
        worker = store.fail_detached_worker_recovery(
            worker.name,
            worker.recovery_generation,
            str(error),
            now=timestamp,
        )
        enqueue_recovery_report(
            store,
            worker,
            phase="launch-failed",
            text=(
                f"⚠️ Recovery attempt {worker.restart_count} for "
                f"'{worker.name}' could not launch: {str(error)[:500]}"
            ),
        )
        return "failed", worker

    enqueue_recovery_report(
        store,
        worker,
        phase="started",
        text=(
            f"♻️ Recovering detached worker '{worker.name}' after its tmux "
            f"session stopped. The exact {worker.provider} conversation was "
            "resumed and is restoring its own native background work. Telegram "
            "Control is waiting for the agent to verify success."
        ),
    )
    return "started", worker


def confirm_recovery(
    store: DurableStore,
    name: str,
    generation: int,
    summary: str,
) -> DetachedWorker:
    worker = store.resolve_detached_worker(_validate_name(name))
    if worker is None:
        raise StoreError("Detached worker was not found.")
    if not tmux_console.has_tmux_session(worker.tmux_session_name):
        raise StoreError("Detached worker session exited before confirmation.")
    worker = store.complete_detached_worker_recovery(name, generation)
    detail = str(summary).strip()[:2_000]
    enqueue_recovery_report(
        store,
        worker,
        phase="succeeded",
        text=(
            f"✅ Detached worker '{worker.name}' recovered successfully. The "
            f"{worker.provider} session verified that it restored its state and "
            "native background work."
            + (f"\n\n{detail}" if detail else "")
        ),
    )
    return worker


def fail_recovery(
    store: DurableStore,
    name: str,
    generation: int,
    reason: str,
) -> DetachedWorker:
    worker = store.resolve_detached_worker(_validate_name(name))
    if worker is None:
        raise StoreError("Detached worker was not found.")
    _kill_session(worker.tmux_session_name)
    worker = store.fail_detached_worker_recovery(
        name,
        generation,
        str(reason).strip() or "The resumed agent could not restore its state.",
    )
    enqueue_recovery_report(
        store,
        worker,
        phase="agent-failed",
        text=(
            f"⚠️ Detached worker '{worker.name}' resumed, but the agent reported "
            "that it could not safely restore its state and native background "
            f"work.\n\n{worker.last_recovery_error}"
        ),
    )
    return worker


def stop_worker(store: DurableStore, name: str) -> DetachedWorker:
    """Kill the session and record that stopping was deliberate.

    Setting intended_state to stopped is the point: it is what stops
    reconciliation from treating this as a crash worth recovering from.
    """
    worker = store.resolve_detached_worker(_validate_name(name))
    if worker is None:
        raise StoreError("Detached worker was not found.")
    if tmux_console.has_tmux_session(worker.tmux_session_name):
        subprocess.run(
            [
                tmux_console.tmux_binary(),
                "kill-session",
                "-t",
                f"={worker.tmux_session_name}",
            ],
            capture_output=True,
            text=True,
        )
    return store.set_detached_worker_states(
        name,
        intended_state="stopped",
        observed_state="stopped",
    )


def resolve_destination(store: DurableStore, name: str) -> tuple[int, int]:
    worker = store.resolve_detached_worker(_validate_name(name))
    if worker is None:
        raise StoreError("Detached worker was not found.")
    row = store.connection.execute(
        """
        SELECT chat_id, message_thread_id
        FROM surface_bindings
        WHERE binding_id = ? AND state = 'active'
        """,
        (worker.binding_id,),
    ).fetchone()
    if row is None:
        raise StoreError("Detached worker topic is no longer available.")
    return int(row["chat_id"]), int(row["message_thread_id"] or 0)


def report(
    store: DurableStore,
    name: str,
    *,
    key: str,
    text: str,
    mode: str = "voice",
) -> None:
    """Post a progress update into the worker's own topic.

    Deliberately not routed through the leased-mailbox helper: a detached
    worker holds no lease and never will. It is safe precisely because the
    destination is a topic no managed turn owns.
    """
    if mode not in {"voice", "text"}:
        raise StoreError("Report mode must be voice or text.")
    body = text.strip()
    if not body:
        raise StoreError("Report text is empty.")
    if len(body) > MAX_REPORT_CHARACTERS:
        raise StoreError(
            f"Report is {len(body)} characters; the limit is {MAX_REPORT_CHARACTERS}."
        )

    chat_id, thread_id = resolve_destination(store, name)
    token = telegram_bridge.read_token()
    params: dict[str, Any] = {"chat_id": chat_id}
    if thread_id:
        params["message_thread_id"] = thread_id

    if mode == "text":
        telegram_bridge.api_call(token, "sendMessage", text=body, **params)
        return

    voice_path = voice_responses.synthesize_voice(body, f"detached-{name}-{key}")
    telegram_bridge.api_call(
        token,
        "sendVoice",
        __voice_file_path=str(voice_path),
        **params,
    )


def telegram_topic_url(binding: Optional[SurfaceBinding]) -> Optional[str]:
    """Return a private-supergroup topic link when Telegram supports one."""
    if (
        binding is None
        or binding.chat_id >= 0
        or binding.message_thread_id is None
        or binding.message_thread_id <= 0
    ):
        return None
    internal_chat_id = str(abs(binding.chat_id))
    if not internal_chat_id.startswith("100"):
        return None
    return (
        f"https://t.me/c/{internal_chat_id[3:]}/"
        f"{int(binding.message_thread_id)}"
    )


def origin_surface(
    store: DurableStore,
    worker: DetachedWorker,
) -> Optional[SurfaceBinding]:
    """Resolve the conversational topic that launched a detached worker."""
    if not worker.origin_agent_id:
        return None
    agent = store.resolve_agent(worker.origin_agent_id)
    if agent is None or agent.surface_binding_id is None:
        return None
    return store.resolve_surface_binding_by_id(agent.surface_binding_id)


def report_only_notice(
    worker: DetachedWorker,
    main_topic_name: Optional[str],
    main_topic_url: Optional[str] = None,
) -> str:
    """The reply an inbound message to a worker topic gets.

    Worker topics are one-way by design. Steering a tmux session through chat
    would mean reasoning about what it is doing and whether it is even at a
    prompt; relaying through the main agent is both simpler and the thing an
    operator actually wants.
    """
    destination = f" in {main_topic_name}" if main_topic_name else ""
    notice = (
        f"This topic is report-only — it carries updates from the detached "
        f"worker '{worker.name}' and nothing here is read by it.\n\n"
        f"To steer or stop this worker, message the project's main agent"
        f"{destination}."
    )
    if main_topic_url:
        notice += f"\n\nOpen the main agent topic: {main_topic_url}"
    return notice
