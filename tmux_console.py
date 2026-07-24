#!/usr/bin/python3
"""Explicit tmux console takeover for managed provider sessions."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from durable_store import AgentConsole, DurableStore, ManagedAgent, StoreError


def tmux_binary() -> str:
    binary = shutil.which("tmux")
    if not binary:
        raise StoreError("tmux is not installed.")
    return binary


def codex_binary() -> str:
    binary = shutil.which("codex")
    if binary:
        return binary
    for candidate in (
        Path("/opt/homebrew/bin/codex"),
        Path("/usr/local/bin/codex"),
    ):
        if candidate.is_file():
            return str(candidate)
    raise StoreError("Codex CLI is not installed.")


def claude_binary() -> str:
    binary = shutil.which("claude")
    if binary:
        return binary
    for candidate in (
        Path.home() / ".local" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ):
        if candidate.is_file():
            return str(candidate)
    raise StoreError("Claude Code CLI is not installed.")


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
    if agent.provider not in {"codex", "claude"}:
        raise StoreError(
            f"Interactive console is not implemented for provider: {agent.provider}"
        )
    if not agent.project_path or not Path(agent.project_path).is_dir():
        raise StoreError("Managed agent project directory is unavailable.")
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

    store.reserve_agent_console(agent.agent_id, session_name)
    if agent.provider == "codex":
        sandbox = str(agent.provider_config.get("sandbox", "workspace-write"))
        command = [
            codex_binary(),
            "resume",
            "--include-non-interactive",
            "--sandbox",
            sandbox,
            "--cd",
            agent.project_path,
        ]
        model = agent.provider_config.get("model")
        if model:
            command.extend(["--model", str(model)])
        effort = agent.provider_config.get("effort")
        if effort:
            command.extend(
                ["--config", f'model_reasoning_effort="{effort}"']
            )
        command.append(agent.provider_session_id)
    else:
        permission_mode = str(
            agent.provider_config.get("permission_mode", "bypassPermissions")
        )
        if permission_mode not in {
            "acceptEdits",
            "auto",
            "bypassPermissions",
            "dontAsk",
            "plan",
        }:
            raise StoreError("Claude permission mode is invalid.")
        command = [
            claude_binary(),
            "--resume",
            agent.provider_session_id,
            "--permission-mode",
            permission_mode,
        ]
        model = agent.provider_config.get("model")
        if model:
            command.extend(["--model", str(model)])
        effort = agent.provider_config.get("effort")
        if effort:
            command.extend(["--effort", str(effort)])
    try:
        result = subprocess.run(
            [
                tmux_binary(),
                "new-session",
                "-d",
                "-s",
                session_name,
                "-c",
                agent.project_path,
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
