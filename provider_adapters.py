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
            command = [self.binary, "exec", "resume", "--json"]
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


def adapter_for(agent: ManagedAgent) -> ProviderAdapter:
    if agent.provider == "codex":
        return CodexExecAdapter()
    raise ProviderAdapterError(
        f"Provider adapter is not implemented: {agent.provider}"
    )
