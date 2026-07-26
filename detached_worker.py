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
import subprocess
from pathlib import Path
from typing import Any, Optional

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

    command = launch_command_for(provider, directory, provider_config or {})

    worker = store.create_detached_worker(
        name=name,
        binding_id=binding_id,
        project_path=project_path,
        provider=provider,
        tmux_session_name=session_name,
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
    return store.set_detached_worker_states(name, observed_state="running")


def launch_command_for(
    provider: str,
    working_directory: str,
    provider_config: dict[str, Any],
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
        return adapter.detached_launch_command(working_directory, provider_config)
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
    if alive and worker.observed_state != "running":
        return store.set_detached_worker_states(name, observed_state="running")
    if not alive and worker.observed_state != "stopped":
        return store.set_detached_worker_states(name, observed_state="stopped")
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
