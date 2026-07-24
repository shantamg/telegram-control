#!/usr/bin/python3
"""Provider-neutral agent execution adapters."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal, Optional, Protocol

from durable_store import ManagedAgent, StoreError


class ProviderAdapterError(StoreError):
    """Raised when a provider cannot complete a normalized turn."""


class ProviderTurnCancelled(ProviderAdapterError):
    """Raised after a running provider turn has been cancelled."""


@dataclass(frozen=True)
class ProviderControl:
    """One durable live-control request for the currently running turn."""

    control_id: int
    kind: Literal["steer", "cancel"]
    text: str = ""
    expected_turn_id: Optional[str] = None


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
        on_progress: Optional[Callable[[str, str], None]] = None,
        poll_control: Optional[Callable[[], Optional[ProviderControl]]] = None,
        on_control: Optional[
            Callable[[ProviderControl, Literal["applied", "rejected"], str], None]
        ] = None,
    ) -> ProviderTurnResult:
        ...


_END_OF_STREAM = object()
_NO_LINE = object()


def _validate_control(control: Any) -> ProviderControl:
    if not isinstance(control, ProviderControl):
        raise ProviderAdapterError("Provider control callback returned an invalid value.")
    if type(control.control_id) is not int or control.control_id <= 0:
        raise ProviderAdapterError("Provider control ID must be a positive integer.")
    if control.kind not in {"steer", "cancel"}:
        raise ProviderAdapterError("Provider control kind is invalid.")
    if control.kind == "steer" and not control.text.strip():
        raise ProviderAdapterError("A steer control requires non-empty text.")
    if control.expected_turn_id is not None and not control.expected_turn_id:
        raise ProviderAdapterError("Expected provider turn ID cannot be empty.")
    return control


def _emit_progress(
    callback: Optional[Callable[[str, str], None]],
    stage: str,
    detail: str,
) -> None:
    """Emit a normalized provider progress event."""

    if callback is not None:
        callback(stage, detail)


class _UserFacingProgress:
    """Throttle and bound provider-authored text before status-card updates."""

    MAX_CHARACTERS = 3400
    MIN_INTERVAL_SECONDS = 0.75

    def __init__(
        self,
        callback: Optional[Callable[[str, str], None]],
    ) -> None:
        self.callback = callback
        self.last_text = ""
        self.last_emitted_at = 0.0
        self.has_output = False

    def emit(self, stage: str, text: str, force: bool = False) -> None:
        rendered = str(text).strip()
        if not rendered:
            return
        if len(rendered) > self.MAX_CHARACTERS:
            rendered = "…\n\n" + rendered[-(self.MAX_CHARACTERS - 3) :]
        if rendered == self.last_text:
            return
        now = time.monotonic()
        if (
            not force
            and self.last_emitted_at
            and now - self.last_emitted_at < self.MIN_INTERVAL_SECONDS
        ):
            return
        _emit_progress(self.callback, stage, rendered)
        self.last_text = rendered
        self.last_emitted_at = now
        self.has_output = True


def _combined_user_output(
    completed_parts: list[str],
    active_parts: Iterable[str] = (),
) -> str:
    parts = [
        str(part).strip()
        for part in [*completed_parts, *active_parts]
        if str(part).strip()
    ]
    return "\n\n".join(parts)


def _append_distinct_output(parts: list[str], text: str) -> None:
    rendered = str(text).strip()
    if rendered and (not parts or parts[-1] != rendered):
        parts.append(rendered)


def _emit_control(
    callback: Optional[
        Callable[[ProviderControl, Literal["applied", "rejected"], str], None]
    ],
    control: ProviderControl,
    outcome: Literal["applied", "rejected"],
    detail: str,
) -> None:
    if callback is not None:
        callback(control, outcome, detail)


def _start_line_reader(stream: Any) -> "queue.Queue[Any]":
    lines: "queue.Queue[Any]" = queue.Queue()

    def read_lines() -> None:
        try:
            for line in stream:
                lines.put(line)
        except BaseException as exc:
            lines.put(exc)
        finally:
            lines.put(_END_OF_STREAM)

    thread = threading.Thread(target=read_lines, daemon=True)
    thread.start()
    return lines


def _write_json_line(process: Any, payload: dict[str, Any]) -> None:
    if process.stdin is None:
        raise ProviderAdapterError("Provider input stream is unavailable.")
    try:
        process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        process.stdin.flush()
    except (BrokenPipeError, OSError, ValueError) as exc:
        raise ProviderAdapterError("Provider input stream closed unexpectedly.") from exc


def _read_json_line(
    lines: "queue.Queue[Any]",
    timeout_seconds: float,
) -> Any:
    try:
        item = lines.get(timeout=max(0.0, timeout_seconds))
    except queue.Empty:
        return _NO_LINE
    if item is _END_OF_STREAM:
        return _END_OF_STREAM
    if isinstance(item, BaseException):
        raise ProviderAdapterError("Provider output stream failed.") from item
    try:
        value = json.loads(str(item))
    except json.JSONDecodeError:
        raise ProviderAdapterError("Provider emitted invalid JSONL output.") from None
    if not isinstance(value, dict):
        raise ProviderAdapterError("Provider emitted an invalid event.")
    return value


def _terminate_process_group(process: Any, grace_seconds: float = 2.0) -> None:
    """Terminate the process group created for one adapter invocation."""

    if process.poll() is not None:
        return
    signalled = False
    pid = getattr(process, "pid", None)
    if isinstance(pid, int) and pid > 0:
        try:
            os.killpg(pid, signal.SIGTERM)
            signalled = True
        except (OSError, ProcessLookupError):
            pass
    if not signalled:
        try:
            process.terminate()
        except (AttributeError, OSError, ProcessLookupError):
            pass
    try:
        process.wait(timeout=grace_seconds)
        return
    except (subprocess.TimeoutExpired, TimeoutError):
        pass
    if isinstance(pid, int) and pid > 0:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except (AttributeError, OSError, ProcessLookupError):
                pass
    else:
        try:
            process.kill()
        except (AttributeError, OSError, ProcessLookupError):
            pass
    try:
        process.wait(timeout=grace_seconds)
    except (subprocess.TimeoutExpired, TimeoutError):
        pass


def _finish_process(process: Any) -> None:
    if process.stdin is not None and not getattr(process.stdin, "closed", False):
        try:
            process.stdin.close()
        except (OSError, ValueError):
            pass
    if process.poll() is None:
        try:
            process.wait(timeout=2.0)
        except (subprocess.TimeoutExpired, TimeoutError):
            _terminate_process_group(process)


def _safe_rpc_error_detail(provider: str, action: str, error: Any) -> str:
    code = error.get("code") if isinstance(error, dict) else None
    suffix = f" (code {code})" if isinstance(code, int) else ""
    return f"{provider} rejected {action}{suffix}."


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
    """Codex app-server adapter with native same-turn steering and interrupt."""

    def __init__(
        self,
        binary: Optional[str] = None,
        timeout_seconds: int = 90 * 60,
        poll_interval_seconds: float = 1.0,
        control_timeout_seconds: float = 30.0,
        _popen_factory: Callable[..., Any] = subprocess.Popen,
    ):
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
        self.poll_interval_seconds = max(0.01, float(poll_interval_seconds))
        self.control_timeout_seconds = max(0.01, float(control_timeout_seconds))
        self._popen_factory = _popen_factory

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            resume=True,
            interrupt=True,
            structured_events=True,
            interactive_console=True,
        )

    @staticmethod
    def _sandbox_mode(agent: ManagedAgent) -> str:
        return str(
            agent.provider_config.get("sandbox", "danger-full-access")
        )

    @staticmethod
    def _thread_request(
        persisted_session: Optional[str],
        launch_directory: str,
        model: Optional[Any],
        sandbox: str,
    ) -> tuple[str, dict[str, Any]]:
        params: dict[str, Any] = {
            "cwd": launch_directory,
            "sandbox": sandbox,
            "approvalPolicy": "never",
        }
        if model:
            params["model"] = str(model)
        if persisted_session:
            params["threadId"] = persisted_session
            return "thread/resume", params
        return "thread/start", params

    @staticmethod
    def _turn_request(
        thread_id: str,
        prompt: str,
        launch_directory: str,
        model: Optional[Any],
        effort: Optional[Any],
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "cwd": launch_directory,
        }
        if model:
            params["model"] = str(model)
        if effort:
            params["effort"] = str(effort)
        return params

    @staticmethod
    def _normalize_usage(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        last = payload.get("last")
        if not isinstance(last, dict):
            return {}
        return {
            "input_tokens": int(last.get("inputTokens", 0)),
            "cached_input_tokens": int(last.get("cachedInputTokens", 0)),
            "output_tokens": int(last.get("outputTokens", 0)),
            "reasoning_output_tokens": int(
                last.get("reasoningOutputTokens", 0)
            ),
            "total_tokens": int(last.get("totalTokens", 0)),
        }

    def _wait_for_rpc(
        self,
        process: Any,
        lines: "queue.Queue[Any]",
        request_id: int,
        deadline: float,
        heartbeat: Callable[[], None],
    ) -> dict[str, Any]:
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise ProviderAdapterError("Codex app-server request timed out.")
            heartbeat()
            message = _read_json_line(
                lines,
                min(self.poll_interval_seconds, deadline - now),
            )
            if message is _NO_LINE:
                continue
            if message is _END_OF_STREAM:
                raise ProviderAdapterError("Codex app-server exited unexpectedly.")
            if message.get("id") != request_id:
                if "method" in message and "id" in message:
                    raise ProviderAdapterError(
                        "Codex requested unsupported interactive input."
                    )
                continue
            if "error" in message:
                detail = _safe_rpc_error_detail(
                    "Codex",
                    "app-server request",
                    message.get("error"),
                )
                raise ProviderAdapterError(detail)
            result = message.get("result")
            if not isinstance(result, dict):
                raise ProviderAdapterError(
                    "Codex app-server returned an invalid response."
                )
            return result

    def run_turn(
        self,
        agent: ManagedAgent,
        prompt: str,
        mailbox_session_id: Optional[str],
        on_session: Callable[[str], None],
        heartbeat: Callable[[], None],
        on_progress: Optional[Callable[[str, str], None]] = None,
        poll_control: Optional[Callable[[], Optional[ProviderControl]]] = None,
        on_control: Optional[
            Callable[[ProviderControl, Literal["applied", "rejected"], str], None]
        ] = None,
    ) -> ProviderTurnResult:
        if not agent.project_path:
            raise ProviderAdapterError("Codex agent has no project path.")
        launch_directory = agent.working_directory or agent.project_path
        model = agent.provider_config.get("model")
        effort = agent.provider_config.get("effort")
        sandbox = self._sandbox_mode(agent)
        persisted_session = mailbox_session_id or agent.provider_session_id
        recovery = mailbox_session_id is not None
        effective_prompt = prompt
        if recovery:
            effective_prompt = (
                "The controller lost the completion status for the previous "
                "delivery of this request. Inspect the existing conversation "
                "and project state, then finish or report the result without "
                "repeating work that already completed.\n\n"
                f"Original request:\n{prompt}"
            )

        deadline = time.monotonic() + self.timeout_seconds
        command = [self.binary, "app-server", "--stdio"]
        _emit_progress(on_progress, "starting", "Starting Codex.")
        with tempfile.TemporaryFile(mode="w+t") as stderr_file:
            process = self._popen_factory(
                command,
                cwd=launch_directory,
                env={**os.environ, **agent.runtime_environment},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            try:
                if process.stdout is None:
                    raise ProviderAdapterError(
                        "Codex app-server output stream is unavailable."
                    )
                lines = _start_line_reader(process.stdout)
                request_id = 1
                _write_json_line(
                    process,
                    {
                        "id": request_id,
                        "method": "initialize",
                        "params": {
                            "clientInfo": {
                                "name": "telegram-control",
                                "title": "Telegram Control",
                                "version": "1",
                            }
                        },
                    },
                )
                self._wait_for_rpc(
                    process,
                    lines,
                    request_id,
                    deadline,
                    heartbeat,
                )
                _write_json_line(process, {"method": "initialized"})

                request_id += 1
                thread_method, thread_params = self._thread_request(
                    persisted_session,
                    launch_directory,
                    model,
                    sandbox,
                )
                _write_json_line(
                    process,
                    {
                        "id": request_id,
                        "method": thread_method,
                        "params": thread_params,
                    },
                )
                thread_result = self._wait_for_rpc(
                    process,
                    lines,
                    request_id,
                    deadline,
                    heartbeat,
                )
                thread = thread_result.get("thread")
                thread_id = (
                    str(thread.get("id", ""))
                    if isinstance(thread, dict)
                    else ""
                )
                if not thread_id:
                    raise ProviderAdapterError(
                        "Codex app-server returned an empty thread ID."
                    )
                if persisted_session is not None and thread_id != persisted_session:
                    raise ProviderAdapterError(
                        "Codex changed the persisted thread ID."
                    )
                on_session(thread_id)
                _emit_progress(on_progress, "session_ready", thread_id)

                request_id += 1
                _write_json_line(
                    process,
                    {
                        "id": request_id,
                        "method": "turn/start",
                        "params": self._turn_request(
                            thread_id,
                            effective_prompt,
                            launch_directory,
                            model,
                            effort,
                        ),
                    },
                )
                turn_result = self._wait_for_rpc(
                    process,
                    lines,
                    request_id,
                    deadline,
                    heartbeat,
                )
                turn = turn_result.get("turn")
                turn_id = (
                    str(turn.get("id", ""))
                    if isinstance(turn, dict)
                    else ""
                )
                if not turn_id:
                    raise ProviderAdapterError(
                        "Codex app-server returned an empty turn ID."
                    )
                _emit_progress(on_progress, "turn_started", turn_id)

                final_text = ""
                usage: dict[str, Any] = {}
                completed = False
                item_phases: dict[str, str] = {}
                item_text: dict[str, str] = {}
                commentary_parts: list[str] = []
                visible_progress = _UserFacingProgress(on_progress)
                pending: Optional[
                    tuple[int, ProviderControl, float]
                ] = None
                while not completed:
                    now = time.monotonic()
                    if now >= deadline:
                        raise ProviderAdapterError("Codex turn timed out.")
                    heartbeat()

                    if pending is None and poll_control is not None:
                        candidate = poll_control()
                        if candidate is not None:
                            control = _validate_control(candidate)
                            if (
                                control.expected_turn_id is not None
                                and control.expected_turn_id != turn_id
                            ):
                                _emit_control(
                                    on_control,
                                    control,
                                    "rejected",
                                    "Control targeted a stale provider turn.",
                                )
                            else:
                                request_id += 1
                                if control.kind == "steer":
                                    method = "turn/steer"
                                    params = {
                                        "threadId": thread_id,
                                        "expectedTurnId": turn_id,
                                        "input": [
                                            {
                                                "type": "text",
                                                "text": control.text,
                                            }
                                        ],
                                    }
                                    progress_stage = "steering"
                                    progress_detail = (
                                        "Sending guidance to the active Codex turn."
                                    )
                                else:
                                    method = "turn/interrupt"
                                    params = {
                                        "threadId": thread_id,
                                        "turnId": turn_id,
                                    }
                                    progress_stage = "cancelling"
                                    progress_detail = (
                                        "Interrupting the active Codex turn."
                                    )
                                _emit_progress(
                                    on_progress,
                                    progress_stage,
                                    progress_detail,
                                )
                                _write_json_line(
                                    process,
                                    {
                                        "id": request_id,
                                        "method": method,
                                        "params": params,
                                    },
                                )
                                pending = (
                                    request_id,
                                    control,
                                    now + self.control_timeout_seconds,
                                )

                    if pending is not None and now >= pending[2]:
                        _pending_id, control, _pending_deadline = pending
                        if control.kind == "cancel":
                            _terminate_process_group(process)
                            _emit_control(
                                on_control,
                                control,
                                "applied",
                                "Codex did not acknowledge interrupt; "
                                "its local process group was terminated.",
                            )
                            raise ProviderTurnCancelled(
                                "Codex turn was cancelled by local fallback."
                            )
                        _emit_control(
                            on_control,
                            control,
                            "rejected",
                            "Codex did not acknowledge steering before timeout.",
                        )
                        pending = None

                    wait_for = min(self.poll_interval_seconds, deadline - now)
                    if pending is not None:
                        wait_for = min(wait_for, max(0.0, pending[2] - now))
                    message = _read_json_line(lines, wait_for)
                    if message is _NO_LINE:
                        continue
                    if message is _END_OF_STREAM:
                        if pending is not None and pending[1].kind == "cancel":
                            control = pending[1]
                            _emit_control(
                                on_control,
                                control,
                                "applied",
                                "Codex exited while processing the interrupt.",
                            )
                            raise ProviderTurnCancelled(
                                "Codex turn was cancelled."
                            )
                        raise ProviderAdapterError(
                            "Codex app-server exited before turn completion."
                        )

                    if "id" in message and "method" in message:
                        raise ProviderAdapterError(
                            "Codex requested unsupported interactive input."
                        )
                    if "id" in message:
                        if pending is None or message.get("id") != pending[0]:
                            continue
                        control = pending[1]
                        if "error" in message:
                            _emit_control(
                                on_control,
                                control,
                                "rejected",
                                _safe_rpc_error_detail(
                                    "Codex",
                                    control.kind,
                                    message.get("error"),
                                ),
                            )
                            pending = None
                            continue
                        result = message.get("result")
                        if not isinstance(result, dict):
                            _emit_control(
                                on_control,
                                control,
                                "rejected",
                                "Codex returned an invalid control response.",
                            )
                            pending = None
                            continue
                        if control.kind == "steer":
                            returned_turn = str(result.get("turnId", ""))
                            if returned_turn != turn_id:
                                _emit_control(
                                    on_control,
                                    control,
                                    "rejected",
                                    "Codex acknowledged a different provider turn.",
                                )
                                pending = None
                                continue
                            _emit_control(
                                on_control,
                                control,
                                "applied",
                                "Guidance was accepted by the active Codex turn.",
                            )
                            _emit_progress(
                                (
                                    on_progress
                                    if not visible_progress.has_output
                                    else None
                                ),
                                "working",
                                "Codex is continuing with the new guidance.",
                            )
                            pending = None
                            continue
                        _emit_control(
                            on_control,
                            control,
                            "applied",
                            "Codex acknowledged the interrupt.",
                        )
                        raise ProviderTurnCancelled("Codex turn was cancelled.")

                    method = str(message.get("method", ""))
                    params = message.get("params")
                    if not isinstance(params, dict):
                        continue
                    if method == "item/agentMessage/delta":
                        if (
                            params.get("threadId") == thread_id
                            and params.get("turnId") == turn_id
                        ):
                            item_id = str(params.get("itemId", ""))
                            delta = str(params.get("delta", ""))
                            if item_id and delta:
                                item_text[item_id] = (
                                    item_text.get(item_id, "") + delta
                                )
                                phase = item_phases.get(item_id, "")
                                if phase == "commentary":
                                    visible_progress.emit(
                                        "commentary",
                                        _combined_user_output(
                                            commentary_parts,
                                            [item_text[item_id]],
                                        ),
                                    )
                                else:
                                    visible_progress.emit(
                                        "response",
                                        item_text[item_id],
                                    )
                    elif method == "item/started":
                        if (
                            params.get("threadId") == thread_id
                            and params.get("turnId") == turn_id
                        ):
                            item = params.get("item")
                            item_type = (
                                str(item.get("type", "work"))
                                if isinstance(item, dict)
                                else "work"
                            )
                            if item_type == "agentMessage" and isinstance(item, dict):
                                item_id = str(item.get("id", ""))
                                if item_id:
                                    phase = str(item.get("phase") or "")
                                    item_phases[item_id] = phase
                                    initial_text = str(item.get("text") or "")
                                    if initial_text:
                                        item_text[item_id] = initial_text
                                        if phase == "commentary":
                                            visible_progress.emit(
                                                "commentary",
                                                _combined_user_output(
                                                    commentary_parts,
                                                    [initial_text],
                                                ),
                                            )
                                        else:
                                            visible_progress.emit(
                                                "response",
                                                initial_text,
                                            )
                                continue
                            safe_types = {
                                "commandExecution": "Running a project operation.",
                                "fileChange": "Preparing a project change.",
                                "reasoning": "Reasoning about the request.",
                                "webSearch": "Looking up requested information.",
                                "mcpToolCall": "Using an approved tool.",
                                "collabAgentToolCall": "Coordinating agent work.",
                            }
                            _emit_progress(
                                (
                                    on_progress
                                    if not visible_progress.has_output
                                    else None
                                ),
                                "working",
                                safe_types.get(item_type, "Codex is working."),
                            )
                    elif method == "item/completed":
                        if (
                            params.get("threadId") == thread_id
                            and params.get("turnId") == turn_id
                        ):
                            item = params.get("item")
                            if (
                                isinstance(item, dict)
                                and item.get("type") == "agentMessage"
                            ):
                                text = str(item.get("text", "")).strip()
                                item_id = str(item.get("id", ""))
                                phase = str(
                                    item.get("phase")
                                    or item_phases.get(item_id, "")
                                )
                                if phase == "commentary" and text:
                                    _append_distinct_output(
                                        commentary_parts,
                                        text,
                                    )
                                    visible_progress.emit(
                                        "commentary",
                                        _combined_user_output(commentary_parts),
                                        force=True,
                                    )
                                elif text:
                                    final_text = text
                                    visible_progress.emit(
                                        "response",
                                        text,
                                        force=True,
                                    )
                                if item_id:
                                    item_text.pop(item_id, None)
                                    item_phases.pop(item_id, None)
                    elif method == "thread/tokenUsage/updated":
                        if (
                            params.get("threadId") == thread_id
                            and params.get("turnId") == turn_id
                        ):
                            usage = self._normalize_usage(
                                params.get("tokenUsage")
                            )
                    elif method == "error":
                        if (
                            params.get("threadId") == thread_id
                            and params.get("turnId") == turn_id
                            and not bool(params.get("willRetry"))
                        ):
                            error = params.get("error")
                            message_text = (
                                str(error.get("message", ""))
                                if isinstance(error, dict)
                                else ""
                            )
                            raise ProviderAdapterError(
                                message_text or "Codex turn failed."
                            )
                    elif method == "turn/completed":
                        completed_turn = params.get("turn")
                        if (
                            params.get("threadId") != thread_id
                            or not isinstance(completed_turn, dict)
                            or completed_turn.get("id") != turn_id
                        ):
                            continue
                        status = str(completed_turn.get("status", ""))
                        if pending is not None:
                            control = pending[1]
                            if control.kind == "cancel" and status == "interrupted":
                                _emit_control(
                                    on_control,
                                    control,
                                    "applied",
                                    "Codex reported the turn interrupted.",
                                )
                                raise ProviderTurnCancelled(
                                    "Codex turn was cancelled."
                                )
                            _emit_control(
                                on_control,
                                control,
                                "rejected",
                                "The Codex turn completed before control "
                                "acknowledgment.",
                            )
                            pending = None
                        if status == "interrupted":
                            raise ProviderTurnCancelled(
                                "Codex turn was interrupted."
                            )
                        if status != "completed":
                            error = completed_turn.get("error")
                            detail = (
                                str(error.get("message", ""))
                                if isinstance(error, dict)
                                else ""
                            )
                            raise ProviderAdapterError(
                                detail or "Codex turn failed."
                            )
                        completed = True

                if not final_text:
                    raise ProviderAdapterError(
                        "Codex completed without a final agent message."
                    )
                _emit_progress(on_progress, "completed", "Codex turn completed.")
                result = ProviderTurnResult(
                    provider_session_id=thread_id,
                    final_text=final_text,
                    usage=usage,
                )
            finally:
                _finish_process(process)
        return result


class ClaudePrintAdapter:
    """Bidirectional Claude stream-JSON adapter with SDK live control."""

    PERMISSION_MODES = {
        "acceptEdits",
        "auto",
        "bypassPermissions",
        "dontAsk",
        "plan",
    }

    def __init__(
        self,
        binary: Optional[str] = None,
        timeout_seconds: int = 90 * 60,
        poll_interval_seconds: float = 1.0,
        control_timeout_seconds: float = 30.0,
        _popen_factory: Callable[..., Any] = subprocess.Popen,
    ):
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
        self.poll_interval_seconds = max(0.01, float(poll_interval_seconds))
        self.control_timeout_seconds = max(0.01, float(control_timeout_seconds))
        self._popen_factory = _popen_factory

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            resume=True,
            interrupt=True,
            structured_events=True,
            interactive_console=True,
        )

    def command(
        self,
        agent: ManagedAgent,
        persisted_session: Optional[str],
        fresh_session_id: Optional[str] = None,
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
            "--input-format",
            "stream-json",
            "--replay-user-messages",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode",
            permission_mode,
        ]
        if permission_mode == "bypassPermissions":
            command.append("--dangerously-skip-permissions")
        model = agent.provider_config.get("model")
        if model:
            command.extend(["--model", str(model)])
        effort = agent.provider_config.get("effort")
        if effort:
            command.extend(["--effort", str(effort)])
        if persisted_session:
            command.extend(["--resume", persisted_session])
        elif fresh_session_id:
            command.extend(["--session-id", fresh_session_id])
        return command

    @staticmethod
    def _user_message(
        text: str,
        session_id: str,
        message_id: str,
    ) -> dict[str, Any]:
        return {
            "type": "user",
            "uuid": message_id,
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": None,
            "session_id": session_id,
        }

    def _wait_for_control_response(
        self,
        lines: "queue.Queue[Any]",
        request_id: str,
        deadline: float,
        heartbeat: Callable[[], None],
    ) -> dict[str, Any]:
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise ProviderAdapterError("Claude control request timed out.")
            heartbeat()
            message = _read_json_line(
                lines,
                min(self.poll_interval_seconds, deadline - now),
            )
            if message is _NO_LINE:
                continue
            if message is _END_OF_STREAM:
                raise ProviderAdapterError("Claude exited during initialization.")
            if message.get("type") != "control_response":
                continue
            response = message.get("response")
            if (
                not isinstance(response, dict)
                or response.get("request_id") != request_id
            ):
                continue
            if response.get("subtype") == "error":
                raise ProviderAdapterError(
                    "Claude rejected the SDK initialization request."
                )
            result = response.get("response")
            return result if isinstance(result, dict) else {}

    def run_turn(
        self,
        agent: ManagedAgent,
        prompt: str,
        mailbox_session_id: Optional[str],
        on_session: Callable[[str], None],
        heartbeat: Callable[[], None],
        on_progress: Optional[Callable[[str, str], None]] = None,
        poll_control: Optional[Callable[[], Optional[ProviderControl]]] = None,
        on_control: Optional[
            Callable[[ProviderControl, Literal["applied", "rejected"], str], None]
        ] = None,
    ) -> ProviderTurnResult:
        if not agent.project_path:
            raise ProviderAdapterError("Claude agent has no project path.")
        launch_directory = agent.working_directory or agent.project_path
        persisted_session = mailbox_session_id or agent.provider_session_id
        recovery = mailbox_session_id is not None
        session_id = persisted_session or str(uuid.uuid4())
        command = self.command(agent, persisted_session, session_id)
        effective_prompt = prompt
        if recovery:
            effective_prompt = (
                "The controller lost the completion status for the previous "
                "delivery of this request. Inspect the existing conversation "
                "and project state, then finish or report the result without "
                "repeating work that already completed.\n\n"
                f"Original request:\n{prompt}"
            )

        deadline = time.monotonic() + self.timeout_seconds
        events: list[dict[str, Any]] = []
        _emit_progress(on_progress, "starting", "Starting Claude.")
        with tempfile.TemporaryFile(mode="w+t") as stderr_file:
            process = self._popen_factory(
                command,
                cwd=launch_directory,
                env={**os.environ, **agent.runtime_environment},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            try:
                if process.stdout is None:
                    raise ProviderAdapterError(
                        "Claude output stream is unavailable."
                    )
                lines = _start_line_reader(process.stdout)
                initialize_request_id = "tc-initialize"
                _write_json_line(
                    process,
                    {
                        "type": "control_request",
                        "request_id": initialize_request_id,
                        "request": {
                            "subtype": "initialize",
                            "hooks": None,
                        },
                    },
                )
                self._wait_for_control_response(
                    lines,
                    initialize_request_id,
                    deadline,
                    heartbeat,
                )

                initial_message_id = str(uuid.uuid4())
                _write_json_line(
                    process,
                    self._user_message(
                        effective_prompt,
                        session_id,
                        initial_message_id,
                    ),
                )
                turn_id = f"claude-turn-{uuid.uuid4()}"
                _emit_progress(on_progress, "turn_started", turn_id)

                notified_session = False
                completed_text_parts: list[str] = []
                active_text_blocks: dict[int, str] = {}
                visible_progress = _UserFacingProgress(on_progress)
                pending: Optional[
                    tuple[ProviderControl, str, float]
                ] = None
                while True:
                    now = time.monotonic()
                    if now >= deadline:
                        raise ProviderAdapterError("Claude turn timed out.")
                    heartbeat()

                    if pending is None and poll_control is not None:
                        candidate_control = poll_control()
                        if candidate_control is not None:
                            control = _validate_control(candidate_control)
                            if (
                                control.expected_turn_id is not None
                                and control.expected_turn_id != turn_id
                            ):
                                _emit_control(
                                    on_control,
                                    control,
                                    "rejected",
                                    "Control targeted a stale provider turn.",
                                )
                            elif control.kind == "steer":
                                steer_message_id = str(uuid.uuid4())
                                _emit_progress(
                                    on_progress,
                                    "steering",
                                    "Sending guidance to the active Claude turn.",
                                )
                                _write_json_line(
                                    process,
                                    self._user_message(
                                        control.text,
                                        session_id,
                                        steer_message_id,
                                    ),
                                )
                                pending = (
                                    control,
                                    steer_message_id,
                                    now + self.control_timeout_seconds,
                                )
                            else:
                                control_request_id = (
                                    f"tc-control-{control.control_id}"
                                )
                                _emit_progress(
                                    on_progress,
                                    "cancelling",
                                    "Interrupting the active Claude turn.",
                                )
                                _write_json_line(
                                    process,
                                    {
                                        "type": "control_request",
                                        "request_id": control_request_id,
                                        "request": {"subtype": "interrupt"},
                                    },
                                )
                                pending = (
                                    control,
                                    control_request_id,
                                    now + self.control_timeout_seconds,
                                )

                    if pending is not None and now >= pending[2]:
                        control = pending[0]
                        if control.kind == "cancel":
                            _terminate_process_group(process)
                            _emit_control(
                                on_control,
                                control,
                                "applied",
                                "Claude did not acknowledge interrupt; "
                                "its local process group was terminated.",
                            )
                            raise ProviderTurnCancelled(
                                "Claude turn was cancelled by local fallback."
                            )
                        _emit_control(
                            on_control,
                            control,
                            "rejected",
                            "Claude did not acknowledge steering before timeout.",
                        )
                        pending = None

                    wait_for = min(self.poll_interval_seconds, deadline - now)
                    if pending is not None:
                        wait_for = min(wait_for, max(0.0, pending[2] - now))
                    event = _read_json_line(lines, wait_for)
                    if event is _NO_LINE:
                        continue
                    if event is _END_OF_STREAM:
                        if pending is not None and pending[0].kind == "cancel":
                            control = pending[0]
                            _emit_control(
                                on_control,
                                control,
                                "applied",
                                "Claude exited while processing the interrupt.",
                            )
                            raise ProviderTurnCancelled(
                                "Claude turn was cancelled."
                            )
                        raise ProviderAdapterError(
                            "Claude exited before turn completion."
                        )

                    if event.get("type") == "control_request":
                        provider_request_id = event.get("request_id")
                        if provider_request_id is not None:
                            _write_json_line(
                                process,
                                {
                                    "type": "control_response",
                                    "response": {
                                        "subtype": "error",
                                        "request_id": provider_request_id,
                                        "error": (
                                            "Interactive controller requests "
                                            "are unavailable."
                                        ),
                                    },
                                },
                            )
                        continue

                    if event.get("type") == "control_response":
                        response = event.get("response")
                        if (
                            pending is None
                            or pending[0].kind != "cancel"
                            or not isinstance(response, dict)
                            or response.get("request_id") != pending[1]
                        ):
                            continue
                        control = pending[0]
                        if response.get("subtype") == "success":
                            _emit_control(
                                on_control,
                                control,
                                "applied",
                                "Claude acknowledged the interrupt.",
                            )
                            raise ProviderTurnCancelled(
                                "Claude turn was cancelled."
                            )
                        _terminate_process_group(process)
                        _emit_control(
                            on_control,
                            control,
                            "applied",
                            "Claude rejected the SDK interrupt; "
                            "its local process group was terminated.",
                        )
                        raise ProviderTurnCancelled(
                            "Claude turn was cancelled by local fallback."
                        )

                    candidate = event.get("session_id")
                    if candidate and not notified_session:
                        candidate_text = str(candidate)
                        if candidate_text != session_id:
                            raise ProviderAdapterError(
                                "Claude changed the persisted session ID."
                            )
                        on_session(candidate_text)
                        _emit_progress(
                            on_progress,
                            "session_ready",
                            candidate_text,
                        )
                        notified_session = True

                    if (
                        pending is not None
                        and pending[0].kind == "steer"
                        and event.get("type") == "user"
                        and event.get("uuid") == pending[1]
                    ):
                        control = pending[0]
                        _emit_control(
                            on_control,
                            control,
                            "applied",
                            "Guidance was accepted by the active Claude turn.",
                        )
                        _emit_progress(
                            (
                                on_progress
                                if not visible_progress.has_output
                                else None
                            ),
                            "working",
                            "Claude is continuing with the new guidance.",
                        )
                        pending = None

                    events.append(event)
                    event_type = str(event.get("type", ""))
                    if event_type == "assistant":
                        message = event.get("message")
                        content = (
                            message.get("content")
                            if isinstance(message, dict)
                            else None
                        )
                        text_parts = (
                            [
                                str(item.get("text", "")).strip()
                                for item in content
                                if isinstance(item, dict)
                                and item.get("type") == "text"
                                and str(item.get("text", "")).strip()
                            ]
                            if isinstance(content, list)
                            else []
                        )
                        assistant_text = "\n".join(text_parts).strip()
                        if assistant_text:
                            active_rendered = _combined_user_output(
                                [],
                                [
                                    active_text_blocks[index]
                                    for index in sorted(active_text_blocks)
                                ],
                            )
                            if active_rendered != assistant_text:
                                _append_distinct_output(
                                    completed_text_parts,
                                    assistant_text,
                                )
                            else:
                                for index in sorted(active_text_blocks):
                                    _append_distinct_output(
                                        completed_text_parts,
                                        active_text_blocks[index],
                                    )
                            active_text_blocks.clear()
                            visible_progress.emit(
                                "commentary",
                                _combined_user_output(completed_text_parts),
                                force=True,
                            )
                    elif event_type == "stream_event":
                        stream_event = event.get("event")
                        stream_type = (
                            str(stream_event.get("type", ""))
                            if isinstance(stream_event, dict)
                            else ""
                        )
                        if stream_type == "message_start":
                            active_text_blocks.clear()
                        elif stream_type == "content_block_start":
                            index = stream_event.get("index")
                            block = stream_event.get("content_block")
                            if (
                                type(index) is int
                                and isinstance(block, dict)
                                and block.get("type") == "text"
                            ):
                                active_text_blocks[index] = str(
                                    block.get("text", "")
                                )
                        elif stream_type == "content_block_delta":
                            index = stream_event.get("index")
                            delta = stream_event.get("delta")
                            if (
                                type(index) is int
                                and isinstance(delta, dict)
                                and delta.get("type") == "text_delta"
                            ):
                                active_text_blocks[index] = (
                                    active_text_blocks.get(index, "")
                                    + str(delta.get("text", ""))
                                )
                                visible_progress.emit(
                                    "commentary",
                                    _combined_user_output(
                                        completed_text_parts,
                                        [
                                            active_text_blocks[key]
                                            for key in sorted(
                                                active_text_blocks
                                            )
                                        ],
                                    ),
                                )
                        elif stream_type == "content_block_stop":
                            index = stream_event.get("index")
                            if type(index) is int:
                                _append_distinct_output(
                                    completed_text_parts,
                                    active_text_blocks.pop(index, ""),
                                )
                                visible_progress.emit(
                                    "commentary",
                                    _combined_user_output(
                                        completed_text_parts,
                                        [
                                            active_text_blocks[key]
                                            for key in sorted(
                                                active_text_blocks
                                            )
                                        ],
                                    ),
                                    force=True,
                                )
                        elif not visible_progress.has_output:
                            _emit_progress(
                                on_progress,
                                "working",
                                "Claude is working.",
                            )
                    elif event_type == "system" and not visible_progress.has_output:
                        _emit_progress(
                            on_progress,
                            "working",
                            "Claude is working.",
                        )
                    elif event_type == "result":
                        if pending is not None:
                            control = pending[0]
                            if control.kind == "steer":
                                _emit_control(
                                    on_control,
                                    control,
                                    "rejected",
                                    "The Claude turn completed before steering "
                                    "acknowledgment.",
                                )
                                pending = None
                            else:
                                # The SDK control response can follow the turn's
                                # result; keep stdin open until it is definitive.
                                continue
                        result = consume_claude_events(
                            events,
                            existing_session_id=persisted_session,
                        )
                        _emit_progress(
                            on_progress,
                            "completed",
                            "Claude turn completed.",
                        )
                        break
            finally:
                _finish_process(process)
        return result


def adapter_for(agent: ManagedAgent) -> ProviderAdapter:
    if agent.provider == "codex":
        return CodexExecAdapter()
    if agent.provider == "claude":
        return ClaudePrintAdapter()
    raise ProviderAdapterError(
        f"Provider adapter is not implemented: {agent.provider}"
    )
