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

import fcntl
import os
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
from telegram_formatting import escape_html

NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MAX_REPORT_CHARACTERS = 3_500

# A worker that dies immediately should not be respawned forever. Restarting
# is a recovery mechanism, not a supervisor loop.
MAX_RESTARTS = 3

# Every intended-running worker holds a full provider CLI in memory whether or
# not it is doing anything, and recovery faithfully resurrects all of them on
# every reboot. Unbounded accumulation of finished-but-never-stopped workers
# once swapped a 16 GB machine to a standstill, so creation refuses past this
# point and names the workers to stop instead.
MAX_RUNNING_WORKERS = 16
RESTART_BACKOFF_SECONDS = 30
RECOVERY_CONFIRM_TIMEOUT_SECONDS = 30 * 60
SESSION_DISCOVERY_TIMEOUT_SECONDS = 15
SESSION_DISCOVERY_POLL_SECONDS = 0.25
BRIEF_SUBMIT_ATTEMPTS = 4
BRIEF_POLL_SECONDS = 0.2
BRIEF_READY_POLLS = 25
BRIEF_RECEIPT_POLLS = 40
BRIEF_ENTER_RETRY_POLLS = 5
_BRIEF_FILENAME = re.compile(r"brief-[0-9a-f]{32}\.md")
_COMPOSER_PROMPT = re.compile(r"^( *)[›❯](?:\s|$)")
_SUBMITTED_PROMPT = re.compile(r"^ *[›❯>](?:\s|$)")

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


def attach_command(worker: DetachedWorker) -> str:
    target = shlex.quote(f"={worker.tmux_session_name}")
    if target.startswith("="):
        # zsh treats an unquoted leading = as executable-path expansion.
        target = f"'{target}'"
    return f"tmux attach-session -t {target}"


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
    """Remove controller-owned recovery state and temporary handoffs."""
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
    handoffs = expected.parent / "handoffs"
    if handoffs.is_dir() and not handoffs.is_symlink():
        directory_fd = os.open(handoffs, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_EX)
            for path in handoffs.iterdir():
                if _BRIEF_FILENAME.fullmatch(path.name) and path.is_file():
                    path.unlink(missing_ok=True)
            try:
                handoffs.rmdir()
            except OSError:
                # Preserve any companion files the controller did not create.
                pass
        finally:
            os.close(directory_fd)
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
    running = [
        existing
        for existing in store.list_detached_workers()
        if existing.intended_state == "running" and existing.name != name
    ]
    if len(running) >= MAX_RUNNING_WORKERS:
        roster = ", ".join(sorted(existing.name for existing in running))
        raise StoreError(
            f"{len(running)} detached workers are already intended running "
            f"({roster}). Each one keeps a full provider process resident and "
            "is resurrected on every reboot, so stop the finished ones with "
            "worker-stop before starting another."
        )
    directory = working_directory or project_path
    if not Path(directory).is_dir():
        raise StoreError("Detached worker working directory is unavailable.")

    session_name = tmux_session_name(name)
    if tmux_console.has_tmux_session(session_name):
        raise StoreError(f"A tmux session already uses the name {session_name}.")

    config = dict(provider_config or {})
    account = provider_adapters.project_account(directory)
    session_options = account.session_options(provider) if account else {}
    recovery_file = ensure_recovery_file(store, name)
    baseline_sessions = _session_ids_for(provider, directory, **session_options)
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
    if account is not None:
        command = account.tmux_command(provider, command)

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
                **session_options,
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


def _session_ids_for(
    provider: str, working_directory: str, **session_options: Path
) -> set[str]:
    if provider == "claude":
        sessions = claude_sessions.discover_sessions(
            working_directory,
            limit=100,
            **session_options,
        )
    elif provider == "codex":
        sessions = codex_sessions.discover_sessions(
            working_directory,
            limit=100,
            **session_options,
        )
    else:
        return set()
    return {session.session_id for session in sessions}


def _wait_for_new_session(
    provider: str,
    working_directory: str,
    baseline_session_ids: set[str],
    **session_options: Path,
) -> str:
    deadline = time.monotonic() + SESSION_DISCOVERY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        candidates = (
            _session_ids_for(provider, working_directory, **session_options)
            - baseline_session_ids
        )
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
    session_options = provider_adapters.project_session_options(
        worker.provider, worker.working_directory
    )
    if worker.provider == "claude":
        return (
            claude_sessions.resolve_session(
                worker.provider_session_id,
                worker.working_directory,
                **session_options,
            )
            is not None
        )
    if worker.provider == "codex":
        return (
            codex_sessions.resolve_session(
                worker.provider_session_id,
                worker.working_directory,
                **session_options,
            )
            is not None
        )
    return False


def resume_command_for(
    worker: DetachedWorker,
    prompt: Optional[str] = None,
) -> list[str]:
    """Argv that reopens the worker's exact conversation.

    The prompt is optional because recovery no longer needs one: resuming the
    session ID is itself what restores the worker's scheduled work.
    """
    if not worker.provider_session_id:
        raise StoreError("Detached worker has no persisted provider session.")
    adapter = provider_adapters.adapter_for(_detached_agent_stub(worker))
    try:
        command = adapter.console_command(_detached_agent_stub(worker))
    except provider_adapters.ProviderAdapterError as error:
        raise StoreError(str(error)) from error
    if prompt:
        command.append(prompt)
    account = provider_adapters.project_account(worker.working_directory)
    return account.tmux_command(worker.provider, command) if account else command


def pane_text(session: str) -> str:
    """What the session's pane is currently showing."""
    result = subprocess.run(
        [tmux_console.tmux_binary(), "capture-pane", "-p", "-t", session],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout or ""


# A resumed provider CLI can stall on an interactive startup dialog instead of
# reaching its composer: Claude offers to condense a large session before
# resuming it, and Codex offers to update itself. The tmux session then looks
# recovered while the agent does nothing — after the August 14, 2026 reboot,
# nine workers sat at these dialogs for two weeks, each pinning a full
# provider process. Each entry: identifier, pane substrings that identify the
# dialog, and the tmux keys that dismiss it without side effects.
_STARTUP_DIALOGS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "claude-resume-summary",
        ("Resume from summary (recommended)", "Enter to confirm"),
        ("Enter",),
    ),
    (
        "codex-update-offer",
        ("Update available!", "Press enter to continue"),
        # The highlighted default is "Update now"; move down onto "Skip"
        # rather than running an npm install inside a worker.
        ("Down", "Enter"),
    ),
)


def answer_startup_dialog(session_name: str) -> Optional[str]:
    """Dismiss a known startup dialog so a resumed worker reaches its composer.

    Returns the dialog's identifier when one was answered, None when the pane
    shows anything else. Called from every maintenance pass over a live
    session, so a dialog that takes a moment to render is still caught.
    """
    pane = pane_text(session_name)
    for dialog_id, markers, keys in _STARTUP_DIALOGS:
        if all(marker in pane for marker in markers):
            subprocess.run(
                [
                    tmux_console.tmux_binary(),
                    "send-keys",
                    "-t",
                    f"={session_name}",
                    *keys,
                ],
                capture_output=True,
                text=True,
            )
            return dialog_id
    return None


def _brief_tmux(binary: str, *args: str) -> str:
    try:
        result = subprocess.run(
            [binary, *args], capture_output=True, text=True, check=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise StoreError(f"tmux {args[0]} failed during worker handoff.") from error
    return result.stdout


def _brief_pane_state(snapshot: str, reference: str) -> str:
    """Distinguish a live composer from the provider's echo of a sent message."""
    header, _, body = snapshot.partition("\n")
    x, y, height, in_mode, dead = map(int, header.split())
    lines = body.split("\n")[:-1]
    cursor = len(lines) - height + y
    if in_mode or dead or not 0 <= cursor < len(lines):
        return "unavailable"

    expected = "".join(reference.split())
    prompt = _COMPOSER_PROMPT.match(lines[cursor])
    if prompt and x == len(prompt[1]) + 2:
        placeholder = lines[cursor][prompt.end():].strip()
        if (
            not placeholder
            or placeholder == "Ask Codex to do anything"
            or placeholder.startswith('Try "')
        ):
            if expected:
                # A unique reference in the transcript above an empty composer
                # is positive evidence. Its disappearance alone proves nothing.
                for row in range(cursor - 1, -1, -1):
                    echo = _SUBMITTED_PROMPT.match(lines[row])
                    if not echo:
                        continue
                    following = [lines[row][echo.end():], *lines[row + 1:cursor]]
                    if "".join("".join(following).split()).startswith(expected):
                        return "accepted"
            return "empty"

    if expected:
        # The cursor can be several screen rows below the prompt after wrapping.
        for row in range(cursor, max(-1, cursor - height), -1):
            prompt = _COMPOSER_PROMPT.match(lines[row])
            if prompt:
                draft = [lines[row][prompt.end():], *lines[row + 1:cursor + 1]]
                if (row < cursor or x > len(prompt[1]) + 2) and (
                    "".join("".join(draft).split()) == expected
                ):
                    return "draft"
                break
    return "unavailable"


def send_brief(name: str, worker_dir: Path, brief: bytes) -> Path:
    """Keep the full brief on disk and submit only its short file reference."""
    session = tmux_session_name(_validate_name(name))
    binary = tmux_console.tmux_binary()
    # The launch pane is first. A watcher or attached client can change which
    # window is active, so resolve once and use the exact pane ID thereafter.
    panes = _brief_tmux(
        binary, "list-panes", "-s", "-t", f"={session}", "-F", "#{pane_id}"
    )
    pane = panes.partition("\n")[0]
    if not re.fullmatch(r"%[0-9]+", pane):
        raise StoreError("The detached worker has no tmux pane.")

    def state(reference: str = "") -> str:
        snapshot = _brief_tmux(
            binary, "display-message", "-t", pane, "-p",
            "#{cursor_x} #{cursor_y} #{pane_height} #{pane_in_mode} #{pane_dead}",
            ";", "capture-pane", "-p", "-t", pane, "-S", "-200",
        )
        return _brief_pane_state(snapshot, reference)

    handoffs = worker_dir / "handoffs"
    handoffs.mkdir(mode=0o700, exist_ok=True)
    directory_fd = os.open(handoffs, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise StoreError("Another handoff is in progress for this worker.") from error

        for _ in range(BRIEF_READY_POLLS):
            if state() == "empty":
                break
            time.sleep(BRIEF_POLL_SECONDS)
        else:
            raise StoreError("The worker did not show an empty composer. No keys were sent.")

        path = handoffs / f"brief-{uuid.uuid4().hex}.md"
        reference = f"Read and follow {shlex.quote(str(path))}"
        if not reference.isprintable():
            raise StoreError("The handoff path contains terminal control characters.")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(brief)

        try:
            _brief_tmux(binary, "send-keys", "-t", pane, "-l", "--", reference)
            attempts = 0
            next_enter = 0
            for poll in range(BRIEF_RECEIPT_POLLS):
                status = state(reference)
                if status == "accepted" and attempts:
                    return path
                if (
                    status == "draft"
                    and attempts < BRIEF_SUBMIT_ATTEMPTS
                    and poll >= next_enter
                ):
                    _brief_tmux(binary, "send-keys", "-t", pane, "Enter")
                    attempts += 1
                    next_enter = poll + BRIEF_ENTER_RETRY_POLLS
                time.sleep(BRIEF_POLL_SECONDS)
        except StoreError as error:
            raise StoreError(
                f"{error} Delivery is unconfirmed. Handoff saved at {path}."
            ) from error
        raise StoreError(
            f"Telegram Control could not verify submission. Handoff saved at {path}. "
            "Inspect the pane before retrying."
        )
    finally:
        os.close(directory_fd)


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
        dialog = answer_startup_dialog(worker.tmux_session_name)
        if dialog is not None:
            enqueue_recovery_report(
                store,
                worker,
                phase=f"dialog-{dialog}",
                text=(
                    f"⏯️ Detached worker '{worker.name}' was stalled at a "
                    f"provider startup dialog ({dialog}). It was dismissed so "
                    "the session can reach its composer and continue."
                ),
            )
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
        # No prompt. Resuming the session ID is what restores the worker's
        # scheduled work, and a prompt would only make the session busy —
        # scheduled jobs fire while it is idle, so asking it to describe its
        # own recovery briefly delays the thing being recovered.
        command = resume_command_for(worker)
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

    if not tmux_console.has_tmux_session(worker.tmux_session_name):
        worker = store.fail_detached_worker_recovery(
            worker.name,
            worker.recovery_generation,
            "The resumed provider process exited immediately.",
            now=timestamp,
        )
        enqueue_recovery_report(
            store,
            worker,
            phase="process-exited",
            text=(
                f"⚠️ Recovery attempt {worker.restart_count} for "
                f"'{worker.name}' failed because the resumed provider process "
                "exited immediately."
            ),
        )
        return "failed", worker

    worker = store.complete_detached_worker_recovery(
        worker.name,
        worker.recovery_generation,
    )
    enqueue_recovery_report(
        store,
        worker,
        phase="succeeded",
        text=(
            f"♻️ Detached worker '{worker.name}' stopped and was recovered. Its "
            f"exact {worker.provider} conversation is running again, with the "
            "scheduled work it had before."
        ),
    )
    return "recovered", worker


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

    voice_configuration = store.voice_configuration()
    voice_path = voice_responses.synthesize_voice(
        body,
        f"detached-{name}-{key}",
        voice_name=voice_configuration.voice_name,
        rate=voice_configuration.rate,
    )
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
    """Render the Telegram HTML reply for inbound worker-topic messages."""
    destination = f" in {escape_html(main_topic_name)}" if main_topic_name else ""
    command = escape_html(attach_command(worker))
    notice = (
        f"This topic is report-only — it carries updates from the detached "
        f"worker '{escape_html(worker.name)}' and nothing here is read by it.\n\n"
        "To interact directly, run this in a terminal on the machine running "
        f"Telegram Control.\n<code>{command}</code>\n\n"
        "To steer or stop this worker, message the project's main agent"
        f"{destination}."
    )
    if main_topic_url:
        notice += f"\n\nOpen the main agent topic: {escape_html(main_topic_url)}"
    return notice
