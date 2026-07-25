#!/usr/bin/python3
"""Explicit tmux console takeover for managed provider sessions."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import discovery
import provider_adapters
from durable_store import AgentConsole, DurableStore, ManagedAgent, StoreError


def tmux_binary() -> str:
    binary = shutil.which("tmux")
    if not binary:
        raise StoreError("tmux is not installed.")
    return binary


def console_command_for(agent: ManagedAgent) -> list[str]:
    """Ask the agent's adapter how to resume it as an interactive console.

    Kept adapter-blind on purpose: which binary, which resume flag and which
    permission or sandbox vocabulary a provider uses are all adapter details.
    A provider that cannot offer a console says so through its capabilities,
    so adding one is a matter of implementing the protocol rather than
    editing a branch in here.
    """
    adapter = provider_adapters.adapter_for(agent)
    if not adapter.capabilities().interactive_console:
        raise StoreError(
            f"Interactive console is not implemented for provider: {agent.provider}"
        )
    try:
        return adapter.console_command(agent)
    except provider_adapters.ProviderAdapterError as error:
        raise StoreError(str(error)) from error


def has_tmux_session(session_name: str) -> bool:
    result = subprocess.run(
        [tmux_binary(), "has-session", "-t", f"={session_name}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def reconcile_agent_console(
    store: DurableStore,
    agent_id: str,
) -> AgentConsole | None:
    console = store.resolve_agent_console(agent_id)
    if (
        console is not None
        and console.state in {"starting", "running"}
        and not has_tmux_session(console.tmux_session_name)
    ):
        console = store.set_agent_console_state(
            agent_id,
            console.state,
            "stopped",
        )
    return console


def open_agent_console(
    store: DurableStore,
    agent: ManagedAgent,
) -> AgentConsole:
    adapter = provider_adapters.adapter_for(agent)
    if not adapter.capabilities().interactive_console:
        raise StoreError(
            f"Interactive console is not implemented for provider: {agent.provider}"
        )
    if not agent.project_path or not Path(agent.project_path).is_dir():
        raise StoreError("Managed agent project directory is unavailable.")
    launch_directory = agent.working_directory or agent.project_path
    root_real, workdir_real, git_root = discovery.validate_agent_workspace(
        agent.project_path,
        agent.working_directory,
        agent.git_repository_root,
    )
    if (
        root_real != agent.project_path
        or workdir_real != launch_directory
        or git_root != agent.git_repository_root
    ):
        raise StoreError(
            "Managed agent workspace paths no longer resolve to their "
            "enrolled locations."
        )
    if not agent.provider_session_id:
        raise StoreError("Managed agent has no persisted provider session to resume.")
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", agent.provider_session_id):
        raise StoreError("Persisted provider session ID is invalid.")

    session_name = agent.hierarchical_name
    existing = reconcile_agent_console(store, agent.agent_id)
    if existing is not None and existing.state in {"starting", "running"}:
        raise StoreError("Managed agent console is already running.")
    if has_tmux_session(session_name):
        raise StoreError(
            f"An unmanaged tmux session already uses the name {session_name}."
        )

    # Built before the console row is reserved, so a provider that cannot
    # produce a command leaves no half-reserved console behind.
    command = console_command_for(agent)

    store.reserve_agent_console(agent.agent_id, session_name)
    try:
        result = subprocess.run(
            [
                tmux_binary(),
                "new-session",
                "-d",
                "-s",
                session_name,
                "-c",
                launch_directory,
                *command,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise StoreError(
                result.stderr.strip() or "tmux could not create the managed console."
            )
        return store.set_agent_console_state(
            agent.agent_id,
            "starting",
            "running",
        )
    except BaseException:
        console = store.resolve_agent_console(agent.agent_id)
        if console is not None and console.state == "starting":
            store.set_agent_console_state(agent.agent_id, "starting", "stopped")
        raise


def close_agent_console(
    store: DurableStore,
    agent: ManagedAgent,
) -> AgentConsole:
    console = reconcile_agent_console(store, agent.agent_id)
    if console is None or console.state == "stopped":
        raise StoreError("Managed agent console is not running.")
    result = subprocess.run(
        [tmux_binary(), "kill-session", "-t", f"={console.tmux_session_name}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and has_tmux_session(console.tmux_session_name):
        raise StoreError(
            result.stderr.strip() or "tmux could not close the managed console."
        )
    return store.set_agent_console_state(
        agent.agent_id,
        console.state,
        "stopped",
    )
