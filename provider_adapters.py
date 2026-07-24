#!/usr/bin/python3
"""Provider-neutral agent execution adapters."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Protocol

from durable_store import ManagedAgent, StoreError


class ProviderAdapterError(StoreError):
    """Raised when a provider cannot complete a normalized turn."""


@dataclass(frozen=True)
class ProviderCapabilities:
    streaming: bool
    resume: bool
    interrupt: bool
    structured_events: bool
    interactive_console: bool


@dataclass(frozen=True)
class ProviderTurnResult:
    provider_session_id: str
    final_text: str
    usage: dict[str, Any]


class ProviderAdapter(Protocol):
    def capabilities(self) -> ProviderCapabilities:
        ...

    def run_turn(
        self,
        agent: ManagedAgent,
        prompt: str,
        mailbox_session_id: Optional[str],
        on_session: Callable[[str], None],
        heartbeat: Callable[[], None],
    ) -> ProviderTurnResult:
        ...


def consume_codex_events(
    events: Iterable[dict[str, Any]],
    existing_session_id: Optional[str] = None,
    on_session: Optional[Callable[[str], None]] = None,
) -> ProviderTurnResult:
    session_id = existing_session_id
    final_text = ""
    usage: dict[str, Any] = {}
    failure: Optional[str] = None
    for event in events:
        event_type = str(event.get("type", ""))
        if event_type == "thread.started":
            candidate = str(event.get("thread_id", ""))
            if not candidate:
                raise ProviderAdapterError("Codex emitted an empty thread ID.")
            if session_id is not None and session_id != candidate:
                raise ProviderAdapterError("Codex changed the persisted thread ID.")
            session_id = candidate
            if on_session is not None:
                on_session(candidate)
        elif event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                final_text = str(item.get("text", "")).strip()
        elif event_type == "turn.completed":
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict):
                usage = raw_usage
        elif event_type in {"turn.failed", "error"}:
            failure = str(
                event.get("message")
                or event.get("error")
                or "Codex reported a failed turn."
            )
    if failure:
        raise ProviderAdapterError(failure)
    if not session_id:
        raise ProviderAdapterError("Codex did not provide a persistent thread ID.")
    if not final_text:
        raise ProviderAdapterError("Codex completed without a final agent message.")
    return ProviderTurnResult(
        provider_session_id=session_id,
        final_text=final_text,
        usage=usage,
    )


def consume_claude_events(
    events: Iterable[dict[str, Any]],
    existing_session_id: Optional[str] = None,
    on_session: Optional[Callable[[str], None]] = None,
) -> ProviderTurnResult:
    session_id = existing_session_id
    final_text = ""
    usage: dict[str, Any] = {}
    failure: Optional[str] = None
    notified_session = False
    for event in events:
        candidate_value = event.get("session_id")
        if candidate_value is not None:
            candidate = str(candidate_value)
            if not candidate:
                raise ProviderAdapterError("Claude emitted an empty session ID.")
            if session_id is not None and session_id != candidate:
                raise ProviderAdapterError("Claude changed the persisted session ID.")
            session_id = candidate
            if on_session is not None and not notified_session:
                on_session(candidate)
                notified_session = True

        event_type = str(event.get("type", ""))
        if event_type == "assistant":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                text_parts = [
                    str(item["text"])
                    for item in content
                    if isinstance(item, dict)
                    and item.get("type") == "text"
                    and item.get("text")
                ]
                if text_parts:
                    final_text = "\n".join(text_parts).strip()
        elif event_type == "result":
            if bool(event.get("is_error")) or event.get("subtype") != "success":
                failure = str(
                    event.get("result")
                    or event.get("error")
                    or "Claude reported a failed turn."
                )
            result_text = event.get("result")
            if isinstance(result_text, str) and result_text.strip():
                final_text = result_text.strip()
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict):
                usage = raw_usage
    if failure:
        raise ProviderAdapterError(failure)
    if not session_id:
        raise ProviderAdapterError("Claude did not provide a persistent session ID.")
    if not final_text:
        raise ProviderAdapterError("Claude completed without a final agent message.")
    return ProviderTurnResult(
        provider_session_id=session_id,
        final_text=final_text,
        usage=usage,
    )


class CodexExecAdapter:
    """Structured local Codex adapter using the stable JSONL exec surface."""

    def __init__(self, binary: Optional[str] = None, timeout_seconds: int = 90 * 60):
        self.binary = binary or shutil.which("codex")
        if not self.binary:
            for candidate in (
                Path("/opt/homebrew/bin/codex"),
                Path("/usr/local/bin/codex"),
            ):
                if candidate.is_file():
                    self.binary = str(candidate)
                    break
        if not self.binary:
            raise ProviderAdapterError("Codex CLI is not installed.")
        self.timeout_seconds = int(timeout_seconds)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            resume=True,
            interrupt=False,
            structured_events=True,
            interactive_console=True,
        )

    def run_turn(
        self,
        agent: ManagedAgent,
        prompt: str,
        mailbox_session_id: Optional[str],
        on_session: Callable[[str], None],
        heartbeat: Callable[[], None],
    ) -> ProviderTurnResult:
        if not agent.project_path:
            raise ProviderAdapterError("Codex agent has no project path.")
        model = agent.provider_config.get("model")
        sandbox = str(agent.provider_config.get("sandbox", "workspace-write"))
        persisted_session = mailbox_session_id or agent.provider_session_id
        recovery = mailbox_session_id is not None
        if persisted_session:
            command = [
                self.binary,
                "exec",
                "--sandbox",
                sandbox,
                "resume",
                "--json",
            ]
            if model:
                command.extend(["--model", str(model)])
            command.extend([persisted_session, "-"])
        else:
            command = [
                self.binary,
                "exec",
                "--json",
                "--color",
                "never",
                "--sandbox",
                sandbox,
                "--cd",
                agent.project_path,
            ]
            if model:
                command.extend(["--model", str(model)])
            command.append("-")
        effective_prompt = prompt
        if recovery:
            effective_prompt = (
                "The controller lost the completion status for the previous "
                "delivery of this request. Inspect the existing conversation "
                "and project state, then finish or report the result without "
                "repeating work that already completed.\n\n"
                f"Original request:\n{prompt}"
            )

        events: list[dict[str, Any]] = []
        timed_out = threading.Event()
        with tempfile.TemporaryFile(mode="w+t") as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=agent.project_path,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
            )

            def kill_on_timeout() -> None:
                timed_out.set()
                if process.poll() is None:
                    process.kill()

            timer = threading.Timer(self.timeout_seconds, kill_on_timeout)
            timer.daemon = True
            timer.start()
            try:
                assert process.stdin is not None
                process.stdin.write(effective_prompt)
                process.stdin.close()
                assert process.stdout is not None
                for line in process.stdout:
                    heartbeat()
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        raise ProviderAdapterError(
                            "Codex emitted invalid JSONL output."
                        ) from None
                    if not isinstance(event, dict):
                        raise ProviderAdapterError(
                            "Codex emitted an invalid event."
                        )
                    events.append(event)
                    if event.get("type") == "thread.started":
                        session_id = str(event.get("thread_id", ""))
                        if session_id:
                            on_session(session_id)
                return_code = process.wait()
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.kill()
                    process.wait()
            stderr_file.seek(0)
            stderr = stderr_file.read().strip()
        if timed_out.is_set():
            raise ProviderAdapterError("Codex turn timed out.")
        if return_code != 0:
            raise ProviderAdapterError(
                stderr[-2000:] or f"Codex exited with status {return_code}."
            )
        return consume_codex_events(
            events,
            existing_session_id=persisted_session,
        )


class ClaudePrintAdapter:
    """Structured local Claude adapter using persistent stream-JSON sessions."""

    PERMISSION_MODES = {
        "acceptEdits",
        "auto",
        "bypassPermissions",
        "dontAsk",
        "plan",
    }

    def __init__(self, binary: Optional[str] = None, timeout_seconds: int = 90 * 60):
        self.binary = binary or shutil.which("claude")
        if not self.binary:
            for candidate in (
                Path.home() / ".local" / "bin" / "claude",
                Path("/opt/homebrew/bin/claude"),
                Path("/usr/local/bin/claude"),
            ):
                if candidate.is_file():
                    self.binary = str(candidate)
                    break
        if not self.binary:
            raise ProviderAdapterError("Claude Code CLI is not installed.")
        self.timeout_seconds = int(timeout_seconds)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            resume=True,
            interrupt=False,
            structured_events=True,
            interactive_console=True,
        )

    def command(
        self,
        agent: ManagedAgent,
        persisted_session: Optional[str],
    ) -> list[str]:
        permission_mode = str(
            agent.provider_config.get("permission_mode", "bypassPermissions")
        )
        if permission_mode not in self.PERMISSION_MODES:
            raise ProviderAdapterError("Claude permission mode is invalid.")
        command = [
            self.binary,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            permission_mode,
        ]
        model = agent.provider_config.get("model")
        if model:
            command.extend(["--model", str(model)])
        if persisted_session:
            command.extend(["--resume", persisted_session])
        return command

    def run_turn(
        self,
        agent: ManagedAgent,
        prompt: str,
        mailbox_session_id: Optional[str],
        on_session: Callable[[str], None],
        heartbeat: Callable[[], None],
    ) -> ProviderTurnResult:
        if not agent.project_path:
            raise ProviderAdapterError("Claude agent has no project path.")
        persisted_session = mailbox_session_id or agent.provider_session_id
        recovery = mailbox_session_id is not None
        command = self.command(agent, persisted_session)
        effective_prompt = prompt
        if recovery:
            effective_prompt = (
                "The controller lost the completion status for the previous "
                "delivery of this request. Inspect the existing conversation "
                "and project state, then finish or report the result without "
                "repeating work that already completed.\n\n"
                f"Original request:\n{prompt}"
            )

        events: list[dict[str, Any]] = []
        timed_out = threading.Event()
        with tempfile.TemporaryFile(mode="w+t") as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=agent.project_path,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
            )

            def kill_on_timeout() -> None:
                timed_out.set()
                if process.poll() is None:
                    process.kill()

            timer = threading.Timer(self.timeout_seconds, kill_on_timeout)
            timer.daemon = True
            timer.start()
            try:
                assert process.stdin is not None
                process.stdin.write(effective_prompt)
                process.stdin.close()
                assert process.stdout is not None
                notified_session = False
                for line in process.stdout:
                    heartbeat()
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        raise ProviderAdapterError(
                            "Claude emitted invalid JSONL output."
                        ) from None
                    if not isinstance(event, dict):
                        raise ProviderAdapterError(
                            "Claude emitted an invalid event."
                        )
                    events.append(event)
                    candidate = event.get("session_id")
                    if candidate and not notified_session:
                        candidate_text = str(candidate)
                        if (
                            persisted_session is not None
                            and candidate_text != persisted_session
                        ):
                            raise ProviderAdapterError(
                                "Claude changed the persisted session ID."
                            )
                        on_session(candidate_text)
                        notified_session = True
                return_code = process.wait()
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.kill()
                    process.wait()
            stderr_file.seek(0)
            stderr = stderr_file.read().strip()
        if timed_out.is_set():
            raise ProviderAdapterError("Claude turn timed out.")
        if return_code != 0:
            raise ProviderAdapterError(
                stderr[-2000:] or f"Claude exited with status {return_code}."
            )
        return consume_claude_events(
            events,
            existing_session_id=persisted_session,
        )


def adapter_for(agent: ManagedAgent) -> ProviderAdapter:
    if agent.provider == "codex":
        return CodexExecAdapter()
    if agent.provider == "claude":
        return ClaudePrintAdapter()
    raise ProviderAdapterError(
        f"Provider adapter is not implemented: {agent.provider}"
    )
