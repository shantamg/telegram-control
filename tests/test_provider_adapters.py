import json
import queue
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import provider_adapters
import turn_guidance
from durable_store import ManagedAgent


_FAKE_EOF = object()


class FakeStdout:
    def __init__(self):
        self.items = queue.Queue()

    def emit(self, payload):
        self.items.put(json.dumps(payload) + "\n")

    def close(self):
        self.items.put(_FAKE_EOF)

    def __iter__(self):
        return self

    def __next__(self):
        item = self.items.get()
        if item is _FAKE_EOF:
            raise StopIteration
        return item


class FakeStdin:
    def __init__(self, process):
        self.process = process
        self.buffer = ""
        self.closed = False

    def write(self, value):
        if self.closed:
            raise ValueError("closed")
        self.buffer += value
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line:
                payload = json.loads(line)
                self.process.payloads.append(payload)
                self.process.on_payload(payload)
        return len(value)

    def flush(self):
        return None

    def close(self):
        if not self.closed:
            self.closed = True
            self.process.returncode = 0
            self.process.stdout.close()


class FakeProcess:
    def __init__(self, on_payload):
        self.on_payload = on_payload
        self.payloads = []
        self.stdout = FakeStdout()
        self.stdin = FakeStdin(self)
        self.returncode = None
        self.pid = 98765
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake-provider", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self.stdout.close()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.close()


class FakePopenFactory:
    def __init__(self, process):
        self.process = process
        self.command = None
        self.kwargs = None

    def __call__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        return self.process


def codex_agent(**overrides):
    values = {
        "agent_id": "agent-codex",
        "parent_agent_id": None,
        "role": "project",
        "slug": "project",
        "hierarchical_name": "tc--root--project",
        "provider": "codex",
        "project_path": "/tmp/project",
        "provider_session_id": None,
        "surface_binding_id": None,
        "lifecycle_state": "registered",
        "provider_config": {},
        "working_directory": "/tmp/project/app",
    }
    values.update(overrides)
    return ManagedAgent(**values)


def claude_agent(**overrides):
    values = {
        **codex_agent().__dict__,
        "agent_id": "agent-claude",
        "provider": "claude",
    }
    values.update(overrides)
    return ManagedAgent(**values)


class CodexEventTests(unittest.TestCase):
    def test_provider_availability_reports_each_cli_independently(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            claude = Path(temporary_directory) / "claude"
            claude.write_text("#!/bin/sh\n", encoding="utf-8")
            with mock.patch.object(
                provider_adapters.shutil,
                "which",
                side_effect=lambda name: (
                    str(claude) if name == "claude" else None
                ),
            ), mock.patch.dict(
                provider_adapters.PROVIDER_BINARY_CANDIDATES,
                {"claude": (), "codex": ()},
            ):
                available = provider_adapters.provider_availability()

        self.assertEqual(available["claude"], str(claude))
        self.assertIsNone(available["codex"])

    def test_managed_codex_default_is_unrestricted_without_approvals(self):
        self.assertEqual(
            provider_adapters.CodexExecAdapter._sandbox_mode(codex_agent()),
            "danger-full-access",
        )
        method, params = provider_adapters.CodexExecAdapter._thread_request(
            None,
            "/tmp/project",
            None,
            "danger-full-access",
        )
        self.assertEqual(method, "thread/start")
        self.assertEqual(
            params,
            {
                "cwd": "/tmp/project",
                "sandbox": "danger-full-access",
                "approvalPolicy": "never",
                "developerInstructions": turn_guidance.TURN_GUIDANCE,
            },
        )

    def test_consumes_persistent_session_final_message_and_usage(self):
        sessions = []
        result = provider_adapters.consume_codex_events(
            [
                {"type": "thread.started", "thread_id": "session-123"},
                {"type": "turn.started"},
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Completed the requested inspection.",
                    },
                },
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 80,
                        "output_tokens": 20,
                    },
                },
            ],
            on_session=sessions.append,
        )

        self.assertEqual(sessions, ["session-123"])
        self.assertEqual(result.provider_session_id, "session-123")
        self.assertEqual(
            result.final_text,
            "Completed the requested inspection.",
        )
        self.assertEqual(result.usage["input_tokens"], 100)

    def test_rejects_failed_or_incomplete_turn(self):
        with self.assertRaises(provider_adapters.ProviderAdapterError):
            provider_adapters.consume_codex_events(
                [
                    {"type": "thread.started", "thread_id": "session-123"},
                    {"type": "turn.failed", "message": "model unavailable"},
                ]
            )
        with self.assertRaises(provider_adapters.ProviderAdapterError):
            provider_adapters.consume_codex_events(
                [{"type": "thread.started", "thread_id": "session-123"}]
            )


class ClaudeEventTests(unittest.TestCase):
    def setUp(self):
        self.agent = ManagedAgent(
            agent_id="agent-claude",
            parent_agent_id=None,
            role="project",
            slug="project",
            hierarchical_name="tc--root--project",
            provider="claude",
            project_path="/tmp/project",
            provider_session_id=None,
            surface_binding_id=None,
            lifecycle_state="registered",
            provider_config={},
        )

    def test_consumes_persistent_session_final_result_and_usage(self):
        sessions = []
        result = provider_adapters.consume_claude_events(
            [
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "94761696-6c7d-4f07-b1c3-588d9e886ac7",
                },
                {
                    "type": "assistant",
                    "session_id": "94761696-6c7d-4f07-b1c3-588d9e886ac7",
                    "message": {
                        "content": [{"type": "text", "text": "intermediate"}]
                    },
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "session_id": "94761696-6c7d-4f07-b1c3-588d9e886ac7",
                    "result": "Completed through Claude.",
                    "usage": {"input_tokens": 30, "output_tokens": 5},
                },
            ],
            on_session=sessions.append,
        )

        self.assertEqual(
            sessions,
            ["94761696-6c7d-4f07-b1c3-588d9e886ac7"],
        )
        self.assertEqual(
            result.provider_session_id,
            "94761696-6c7d-4f07-b1c3-588d9e886ac7",
        )
        self.assertEqual(result.final_text, "Completed through Claude.")
        self.assertEqual(result.usage["input_tokens"], 30)

    def test_rejects_changed_session_error_and_missing_result(self):
        with self.assertRaisesRegex(
            provider_adapters.ProviderAdapterError,
            "changed",
        ):
            provider_adapters.consume_claude_events(
                [
                    {"type": "system", "session_id": "session-one"},
                    {"type": "result", "session_id": "session-two"},
                ]
            )
        with self.assertRaisesRegex(
            provider_adapters.ProviderAdapterError,
            "failed",
        ):
            provider_adapters.consume_claude_events(
                [
                    {
                        "type": "result",
                        "subtype": "error",
                        "is_error": True,
                        "session_id": "session-one",
                        "result": "request failed",
                    }
                ]
            )
        with self.assertRaisesRegex(
            provider_adapters.ProviderAdapterError,
            "final agent message",
        ):
            provider_adapters.consume_claude_events(
                [{"type": "system", "session_id": "session-one"}]
            )

    def test_command_uses_structured_resume_and_validated_permissions(self):
        adapter = provider_adapters.ClaudePrintAdapter(binary="/bin/claude")
        fresh = adapter.command(self.agent, None)
        self.assertIn("stream-json", fresh)
        self.assertIn("--include-partial-messages", fresh)
        self.assertIn("bypassPermissions", fresh)
        self.assertIn("--dangerously-skip-permissions", fresh)
        self.assertNotIn("--resume", fresh)

        configured = ManagedAgent(
            **{
                **self.agent.__dict__,
                "provider_config": {
                    "model": "sonnet",
                    "effort": "high",
                    "permission_mode": "acceptEdits",
                },
            }
        )
        resumed = adapter.command(configured, "session-123")
        self.assertIn("--model", resumed)
        self.assertIn("sonnet", resumed)
        self.assertIn("--effort", resumed)
        self.assertIn("high", resumed)
        self.assertEqual(resumed[-2:], ["--resume", "session-123"])
        self.assertIn("acceptEdits", resumed)
        self.assertNotIn("--dangerously-skip-permissions", resumed)

        invalid = ManagedAgent(
            **{
                **self.agent.__dict__,
                "provider_config": {"permission_mode": "invented"},
            }
        )
        with self.assertRaisesRegex(
            provider_adapters.ProviderAdapterError,
            "permission mode",
        ):
            adapter.command(invalid, None)

    def test_turn_guidance_is_appended_for_new_and_resumed_sessions(self):
        adapter = provider_adapters.ClaudePrintAdapter(binary="/bin/claude")

        for permission_mode in ("bypassPermissions", "acceptEdits"):
            agent = ManagedAgent(
                **{
                    **self.agent.__dict__,
                    "provider_config": {"permission_mode": permission_mode},
                }
            )
            for session_id in (None, "desktop-session"):
                command = adapter.command(agent, session_id)
                self.assertIn("--append-system-prompt", command)
                index = command.index("--append-system-prompt")
                self.assertEqual(
                    command[index + 1],
                    turn_guidance.TURN_GUIDANCE,
                )
                if session_id:
                    self.assertEqual(
                        command[-2:],
                        ["--resume", session_id],
                    )

        self.assertTrue(adapter.capabilities().turn_guidance)

    def test_turn_guidance_warns_against_background_work(self):
        # The whole point of the guidance: agents kept backgrounding work that
        # died with the turn. If this text stops saying so, it stops helping.
        text = turn_guidance.TURN_GUIDANCE.lower()
        self.assertIn("background", text)
        self.assertIn("tmux", text)

    def test_turn_guidance_identifies_resumed_turn_as_telegram_managed(self):
        text = turn_guidance.TURN_GUIDANCE.lower()
        self.assertIn("telegram control-managed turn", text)
        self.assertIn("local terminal", text)
        self.assertIn("telegram_control_", text)
        self.assertIn("voice note", text)
        self.assertIn("session's history", text)


class TurnGuidanceCapabilityTests(unittest.TestCase):
    def test_every_adapter_declares_whether_it_delivers_guidance(self):
        # ProviderCapabilities.turn_guidance is required, so a new adapter
        # cannot omit it by accident — unsupported has to be stated.
        for adapter in (
            provider_adapters.ClaudePrintAdapter(binary="/bin/claude"),
            provider_adapters.CodexExecAdapter(binary="/bin/codex"),
        ):
            self.assertIsInstance(adapter.capabilities().turn_guidance, bool)

    def test_each_adapter_exposes_its_configuration_choices(self):
        codex = provider_adapters.configuration_options("codex")
        claude = provider_adapters.configuration_options("claude")

        self.assertIn(("GPT-5.6 Terra", "gpt-5.6-terra"), codex.models)
        self.assertIn(("Ultra", "ultra"), codex.efforts)
        self.assertIn(("Opus", "opus"), claude.models)
        self.assertIn(("Max", "max"), claude.efforts)
        self.assertEqual(codex.models[0], ("Default", None))
        self.assertEqual(claude.models[0], ("Default", None))

    def test_codex_delivers_guidance_on_new_and_resumed_threads(self):
        adapter = provider_adapters.CodexExecAdapter(binary="/bin/codex")
        self.assertTrue(adapter.capabilities().turn_guidance)
        for session_id, expected_method in (
            (None, "thread/start"),
            ("desktop-session", "thread/resume"),
        ):
            method, params = adapter._thread_request(
                session_id,
                "/tmp/project",
                None,
                "danger-full-access",
            )
            self.assertEqual(method, expected_method)
            self.assertEqual(
                params["developerInstructions"],
                turn_guidance.TURN_GUIDANCE,
            )


class ConsoleCommandTests(unittest.TestCase):
    """The console argv is an adapter concern, not tmux_console's."""

    SESSION = "0e6cd6ab-1d0a-4f0e-9f5f-4b1a1f4b8e21"

    def test_codex_console_resumes_the_persisted_session(self):
        adapter = provider_adapters.CodexExecAdapter(binary="/bin/codex")
        agent = codex_agent(
            provider_session_id=self.SESSION,
            provider_config={"model": "gpt-5.6-sol", "effort": "high"},
        )
        command = adapter.console_command(agent)

        self.assertEqual(command[0], "/bin/codex")
        self.assertIn("resume", command)
        self.assertIn("--include-non-interactive", command)
        self.assertIn("--sandbox", command)
        # Codex spells effort as a config override, Claude as a flag; that
        # divergence is exactly what belongs behind the adapter.
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertEqual(command[-1], self.SESSION)

    def test_claude_console_resumes_the_persisted_session(self):
        adapter = provider_adapters.ClaudePrintAdapter(binary="/bin/claude")
        agent = claude_agent(
            provider_session_id=self.SESSION,
            provider_config={"model": "sonnet", "effort": "high"},
        )
        command = adapter.console_command(agent)

        self.assertEqual(command[0], "/bin/claude")
        self.assertEqual(command[1:3], ["--resume", self.SESSION])
        self.assertIn("--effort", command)
        self.assertIn("--dangerously-skip-permissions", command)

    def test_claude_console_rejects_an_invalid_permission_mode(self):
        adapter = provider_adapters.ClaudePrintAdapter(binary="/bin/claude")
        agent = claude_agent(
            provider_session_id=self.SESSION,
            provider_config={"permission_mode": "invented"},
        )
        with self.assertRaisesRegex(
            provider_adapters.ProviderAdapterError,
            "permission mode",
        ):
            adapter.console_command(agent)

    def test_console_requires_a_session_to_resume(self):
        for adapter, agent in (
            (
                provider_adapters.CodexExecAdapter(binary="/bin/codex"),
                codex_agent(provider_session_id=None),
            ),
            (
                provider_adapters.ClaudePrintAdapter(binary="/bin/claude"),
                claude_agent(provider_session_id=None),
            ),
        ):
            with self.assertRaisesRegex(
                provider_adapters.ProviderAdapterError,
                "no persisted provider session",
            ):
                adapter.console_command(agent)


class LiveControlContractTests(unittest.TestCase):
    @staticmethod
    def emit_codex_completion(process, text="Codex final"):
        process.stdout.emit(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {
                        "id": "message-1",
                        "type": "agentMessage",
                        "text": text,
                    },
                },
            }
        )
        process.stdout.emit(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "tokenUsage": {
                        "last": {
                            "inputTokens": 12,
                            "cachedInputTokens": 7,
                            "outputTokens": 3,
                            "reasoningOutputTokens": 1,
                            "totalTokens": 16,
                        },
                        "total": {},
                        "modelContextWindow": 200,
                    },
                },
            }
        )
        process.stdout.emit(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {
                        "id": "turn-1",
                        "items": [],
                        "status": "completed",
                    },
                },
            }
        )

    @staticmethod
    def codex_handshake(process, payload):
        method = payload.get("method")
        if method == "initialize":
            process.stdout.emit({"id": payload["id"], "result": {"userAgent": "test"}})
        elif method == "thread/start":
            process.stdout.emit(
                {
                    "id": payload["id"],
                    "result": {"thread": {"id": "thread-1"}},
                }
            )
        elif method == "thread/resume":
            process.stdout.emit(
                {
                    "id": payload["id"],
                    "result": {"thread": {"id": payload["params"]["threadId"]}},
                }
            )
        elif method == "turn/start":
            process.stdout.emit(
                {
                    "id": payload["id"],
                    "result": {
                        "turn": {
                            "id": "turn-1",
                            "items": [],
                            "status": "inProgress",
                        }
                    },
                }
            )

    def test_provider_control_validation_and_capabilities(self):
        control = provider_adapters.ProviderControl(
            control_id=4,
            kind="steer",
            text="New direction",
            expected_turn_id="turn-1",
        )
        self.assertEqual(provider_adapters._validate_control(control), control)
        with self.assertRaisesRegex(
            provider_adapters.ProviderAdapterError,
            "non-empty",
        ):
            provider_adapters._validate_control(
                provider_adapters.ProviderControl(5, "steer")
            )
        self.assertTrue(
            provider_adapters.CodexExecAdapter(binary="/bin/codex")
            .capabilities()
            .interrupt
        )
        self.assertTrue(
            provider_adapters.ClaudePrintAdapter(binary="/bin/claude")
            .capabilities()
            .interrupt
        )

    def test_codex_app_server_sends_exact_native_steer_and_safe_progress(self):
        process = FakeProcess(lambda payload: None)

        def handle(payload):
            self.codex_handshake(process, payload)
            if payload.get("method") == "turn/steer":
                process.stdout.emit(
                    {
                        "id": payload["id"],
                        "result": {"turnId": "turn-1"},
                    }
                )
                self.emit_codex_completion(process)

        process.on_payload = handle
        factory = FakePopenFactory(process)
        controls = [
            provider_adapters.ProviderControl(
                11,
                "steer",
                "Use the safer approach.",
                "turn-1",
            )
        ]
        outcomes = []
        progress = []
        sessions = []
        adapter = provider_adapters.CodexExecAdapter(
            binary="/bin/codex",
            poll_interval_seconds=0.01,
            _popen_factory=factory,
        )
        result = adapter.run_turn(
            codex_agent(
                provider_config={
                    "model": "gpt-test",
                    "effort": "high",
                    "sandbox": "workspace-write",
                },
                runtime_environment={"TELEGRAM_CONTROL_MAILBOX_ID": "42"},
            ),
            "SECRET prompt at /private/project",
            None,
            sessions.append,
            lambda: None,
            lambda stage, detail: progress.append((stage, detail)),
            lambda: controls.pop(0) if controls else None,
            lambda control, outcome, detail: outcomes.append(
                (control.control_id, outcome, detail)
            ),
        )

        self.assertEqual(factory.command, ["/bin/codex", "app-server", "--stdio"])
        self.assertEqual(factory.kwargs["cwd"], "/tmp/project/app")
        self.assertEqual(
            factory.kwargs["env"]["TELEGRAM_CONTROL_MAILBOX_ID"],
            "42",
        )
        self.assertTrue(factory.kwargs["start_new_session"])
        self.assertEqual(process.payloads[0]["method"], "initialize")
        self.assertEqual(process.payloads[1], {"method": "initialized"})
        self.assertEqual(
            process.payloads[2],
            {
                "id": 2,
                "method": "thread/start",
                "params": {
                    "cwd": "/tmp/project/app",
                    "sandbox": "workspace-write",
                    "approvalPolicy": "never",
                    "developerInstructions": turn_guidance.TURN_GUIDANCE,
                    "model": "gpt-test",
                },
            },
        )
        self.assertEqual(
            process.payloads[3]["params"],
            {
                "threadId": "thread-1",
                "input": [
                    {"type": "text", "text": "SECRET prompt at /private/project"}
                ],
                "cwd": "/tmp/project/app",
                "model": "gpt-test",
                "effort": "high",
            },
        )
        self.assertEqual(
            process.payloads[4],
            {
                "id": 4,
                "method": "turn/steer",
                "params": {
                    "threadId": "thread-1",
                    "expectedTurnId": "turn-1",
                    "input": [
                        {"type": "text", "text": "Use the safer approach."}
                    ],
                },
            },
        )
        self.assertEqual(sessions, ["thread-1"])
        self.assertEqual(outcomes[0][0:2], (11, "applied"))
        self.assertEqual(result.provider_session_id, "thread-1")
        self.assertEqual(result.final_text, "Codex final")
        self.assertEqual(result.usage["cached_input_tokens"], 7)
        self.assertEqual(result.usage["context_tokens"], 16)
        self.assertEqual(result.usage["context_window_tokens"], 200)
        rendered_progress = repr(progress)
        self.assertNotIn("SECRET", rendered_progress)
        self.assertNotIn("/private/project", rendered_progress)

    def test_codex_streams_commentary_and_final_answer_as_visible_progress(self):
        process = FakeProcess(lambda payload: None)

        def emit_agent_message(item_id, phase, chunks, text):
            process.stdout.emit(
                {
                    "method": "item/started",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {
                            "id": item_id,
                            "type": "agentMessage",
                            "text": "",
                            "phase": phase,
                        },
                    },
                }
            )
            for chunk in chunks:
                process.stdout.emit(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "itemId": item_id,
                            "delta": chunk,
                        },
                    }
                )
            process.stdout.emit(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {
                            "id": item_id,
                            "type": "agentMessage",
                            "text": text,
                            "phase": phase,
                        },
                    },
                }
            )

        def handle(payload):
            self.codex_handshake(process, payload)
            if payload.get("method") == "turn/start":
                emit_agent_message(
                    "commentary-1",
                    "commentary",
                    ["I’m checking ", "the event pipeline."],
                    "I’m checking the event pipeline.",
                )
                process.stdout.emit(
                    {
                        "method": "item/started",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "item": {
                                "id": "tool-1",
                                "type": "commandExecution",
                            },
                        },
                    }
                )
                emit_agent_message(
                    "answer-1",
                    "final_answer",
                    ["Implemented ", "and verified."],
                    "Implemented and verified.",
                )
                process.stdout.emit(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {
                                "id": "turn-1",
                                "items": [],
                                "status": "completed",
                            },
                        },
                    }
                )

        process.on_payload = handle
        progress = []
        result = provider_adapters.CodexExecAdapter(
            binary="/bin/codex",
            poll_interval_seconds=0.01,
            _popen_factory=FakePopenFactory(process),
        ).run_turn(
            codex_agent(),
            "Inspect it.",
            None,
            lambda _session: None,
            lambda: None,
            lambda stage, detail: progress.append((stage, detail)),
        )

        visible = [
            event
            for event in progress
            if event[0] in {"commentary", "response"}
        ]
        self.assertIn(
            ("commentary", "I’m checking the event pipeline."),
            visible,
        )
        self.assertEqual(
            visible[-1],
            ("response", "Implemented and verified."),
        )
        self.assertEqual(result.final_text, "Implemented and verified.")
        commentary_index = progress.index(
            ("commentary", "I’m checking the event pipeline.")
        )
        self.assertNotIn(
            ("working", "Running a project operation."),
            progress[commentary_index + 1 :],
        )

    def test_codex_rejects_stale_and_provider_error_controls(self):
        process = FakeProcess(lambda payload: None)
        control_queue = [
            provider_adapters.ProviderControl(
                20, "steer", "stale", "old-turn"
            ),
            provider_adapters.ProviderControl(
                21, "steer", "provider rejects", "turn-1"
            ),
        ]

        def handle(payload):
            self.codex_handshake(process, payload)
            if payload.get("method") == "turn/steer":
                process.stdout.emit(
                    {
                        "id": payload["id"],
                        "error": {
                            "code": -32602,
                            "message": "SECRET reflected message",
                        },
                    }
                )
                self.emit_codex_completion(process)

        process.on_payload = handle
        outcomes = []
        adapter = provider_adapters.CodexExecAdapter(
            binary="/bin/codex",
            poll_interval_seconds=0.01,
            _popen_factory=FakePopenFactory(process),
        )
        result = adapter.run_turn(
            codex_agent(),
            "prompt",
            None,
            lambda _session: None,
            lambda: None,
            poll_control=lambda: control_queue.pop(0) if control_queue else None,
            on_control=lambda control, outcome, detail: outcomes.append(
                (control.control_id, outcome, detail)
            ),
        )

        self.assertEqual(result.final_text, "Codex final")
        self.assertEqual([item[0:2] for item in outcomes], [(20, "rejected"), (21, "rejected")])
        self.assertNotIn("SECRET", repr(outcomes))

    def test_codex_polls_during_silence_and_acknowledges_cancel(self):
        process = FakeProcess(lambda payload: None)

        def handle(payload):
            self.codex_handshake(process, payload)
            if payload.get("method") == "turn/interrupt":
                process.stdout.emit({"id": payload["id"], "result": {}})

        process.on_payload = handle
        polls = []
        heartbeats = []
        outcomes = []

        def poll_control():
            polls.append(True)
            if len(polls) == 3:
                return provider_adapters.ProviderControl(
                    30, "cancel", expected_turn_id="turn-1"
                )
            return None

        adapter = provider_adapters.CodexExecAdapter(
            binary="/bin/codex",
            poll_interval_seconds=0.01,
            _popen_factory=FakePopenFactory(process),
        )
        with self.assertRaises(provider_adapters.ProviderTurnCancelled):
            adapter.run_turn(
                codex_agent(),
                "long task",
                None,
                lambda _session: None,
                lambda: heartbeats.append(True),
                poll_control=poll_control,
                on_control=lambda control, outcome, detail: outcomes.append(
                    (control.control_id, outcome, detail)
                ),
            )
        self.assertGreaterEqual(len(heartbeats), 3)
        self.assertEqual(outcomes[0][0:2], (30, "applied"))
        interrupt = [
            payload
            for payload in process.payloads
            if payload.get("method") == "turn/interrupt"
        ]
        self.assertEqual(
            interrupt[0]["params"],
            {"threadId": "thread-1", "turnId": "turn-1"},
        )

    @staticmethod
    def claude_initialize(process, payload):
        if (
            payload.get("type") == "control_request"
            and payload.get("request", {}).get("subtype") == "initialize"
        ):
            process.stdout.emit(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": payload["request_id"],
                        "response": {"commands": []},
                    },
                }
            )

    @staticmethod
    def emit_claude_result(process, session_id, text="Claude final"):
        process.stdout.emit(
            {
                "type": "assistant",
                "session_id": session_id,
                "uuid": str(uuid.uuid4()),
                "parent_tool_use_id": None,
                "message": {
                    "model": "claude-sonnet-test",
                    "content": [{"type": "text", "text": text}],
                    "usage": {
                        "input_tokens": 2,
                        "cache_creation_input_tokens": 3,
                        "cache_read_input_tokens": 40,
                        "output_tokens": 5,
                    },
                },
            }
        )
        process.stdout.emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": session_id,
                "uuid": str(uuid.uuid4()),
                "result": text,
                "usage": {"input_tokens": 8, "output_tokens": 2},
                "modelUsage": {
                    "claude-sonnet-test": {
                        "contextWindow": 200,
                    }
                },
            }
        )

    def test_claude_streams_uuid_messages_and_applies_live_steer(self):
        process = FakeProcess(lambda payload: None)
        user_messages = []

        def handle(payload):
            self.claude_initialize(process, payload)
            if payload.get("type") == "user":
                user_messages.append(payload)
                if len(user_messages) == 1:
                    process.stdout.emit(
                        {
                            "type": "system",
                            "subtype": "init",
                            "session_id": payload["session_id"],
                        }
                    )
                else:
                    process.stdout.emit(dict(payload))
                    self.emit_claude_result(process, payload["session_id"])

        process.on_payload = handle
        factory = FakePopenFactory(process)
        controls = [
            provider_adapters.ProviderControl(
                41,
                "steer",
                "Focus on tests.",
            )
        ]
        outcomes = []
        progress = []
        adapter = provider_adapters.ClaudePrintAdapter(
            binary="/bin/claude",
            poll_interval_seconds=0.01,
            _popen_factory=factory,
        )
        result = adapter.run_turn(
            claude_agent(
                runtime_environment={"TELEGRAM_CONTROL_MAILBOX_ID": "43"}
            ),
            "SECRET prompt at /private/project",
            None,
            lambda _session: None,
            lambda: None,
            lambda stage, detail: progress.append((stage, detail)),
            lambda: controls.pop(0) if controls else None,
            lambda control, outcome, detail: outcomes.append(
                (control.control_id, outcome, detail)
            ),
        )

        self.assertIn("--input-format", factory.command)
        self.assertIn("--replay-user-messages", factory.command)
        self.assertIn("--session-id", factory.command)
        self.assertEqual(factory.kwargs["cwd"], "/tmp/project/app")
        self.assertEqual(
            factory.kwargs["env"]["TELEGRAM_CONTROL_MAILBOX_ID"],
            "43",
        )
        self.assertTrue(factory.kwargs["start_new_session"])
        self.assertEqual(len(user_messages), 2)
        self.assertEqual(user_messages[1]["message"]["content"], "Focus on tests.")
        for message in user_messages:
            uuid.UUID(message["uuid"])
            uuid.UUID(message["session_id"])
            self.assertIsNone(message["parent_tool_use_id"])
        self.assertEqual(outcomes[0][0:2], (41, "applied"))
        self.assertEqual(result.final_text, "Claude final")
        self.assertEqual(result.usage["context_tokens"], 50)
        self.assertEqual(result.usage["context_window_tokens"], 200)
        self.assertEqual(result.usage["context_model"], "claude-sonnet-test")
        self.assertNotIn("SECRET", repr(progress))
        self.assertNotIn("/private/project", repr(progress))

    def test_claude_keeps_steer_in_flight_during_a_long_tool_call(self):
        process = FakeProcess(lambda payload: None)
        user_messages = []
        heartbeats_after_steer = 0
        emitted_result = False

        def handle(payload):
            self.claude_initialize(process, payload)
            if payload.get("type") != "user":
                return
            user_messages.append(payload)
            if len(user_messages) == 1:
                process.stdout.emit(
                    {
                        "type": "system",
                        "subtype": "init",
                        "session_id": payload["session_id"],
                    }
                )

        def heartbeat():
            nonlocal heartbeats_after_steer, emitted_result
            if len(user_messages) < 2 or emitted_result:
                return
            heartbeats_after_steer += 1
            if heartbeats_after_steer < 5:
                return
            emitted_result = True
            process.stdout.emit(dict(user_messages[1]))
            self.emit_claude_result(
                process,
                user_messages[1]["session_id"],
            )

        process.on_payload = handle
        controls = [
            provider_adapters.ProviderControl(
                42,
                "steer",
                "Apply this after the current tool call.",
            )
        ]
        outcomes = []
        adapter = provider_adapters.ClaudePrintAdapter(
            binary="/bin/claude",
            poll_interval_seconds=0.01,
            control_timeout_seconds=0.01,
            _popen_factory=FakePopenFactory(process),
        )

        result = adapter.run_turn(
            claude_agent(),
            "Start a long tool call.",
            None,
            lambda _session: None,
            heartbeat,
            poll_control=lambda: controls.pop(0) if controls else None,
            on_control=lambda control, outcome, detail: outcomes.append(
                (control.control_id, outcome, detail)
            ),
        )

        self.assertGreaterEqual(heartbeats_after_steer, 5)
        self.assertEqual(len(user_messages), 2)
        self.assertEqual(
            outcomes,
            [
                (
                    42,
                    "applied",
                    "Guidance was accepted by the active Claude turn.",
                )
            ],
        )
        self.assertEqual(result.final_text, "Claude final")

    def test_claude_unacknowledged_steer_does_not_block_stop(self):
        process = FakeProcess(lambda payload: None)
        user_messages = []

        def handle(payload):
            self.claude_initialize(process, payload)
            if payload.get("type") == "user":
                user_messages.append(payload)
                if len(user_messages) == 1:
                    process.stdout.emit(
                        {
                            "type": "system",
                            "subtype": "init",
                            "session_id": payload["session_id"],
                        }
                    )
                return
            request = payload.get("request")
            if (
                payload.get("type") == "control_request"
                and isinstance(request, dict)
                and request.get("subtype") == "interrupt"
            ):
                process.stdout.emit(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": payload["request_id"],
                            "response": {},
                        },
                    }
                )

        process.on_payload = handle
        controls = [
            provider_adapters.ProviderControl(
                43,
                "steer",
                "Apply this after the current tool call.",
            ),
            provider_adapters.ProviderControl(44, "cancel"),
        ]
        outcomes = []
        adapter = provider_adapters.ClaudePrintAdapter(
            binary="/bin/claude",
            poll_interval_seconds=0.01,
            control_timeout_seconds=0.01,
            _popen_factory=FakePopenFactory(process),
        )

        with self.assertRaises(provider_adapters.ProviderTurnCancelled):
            adapter.run_turn(
                claude_agent(),
                "Start a long tool call.",
                None,
                lambda _session: None,
                lambda: None,
                poll_control=lambda: controls.pop(0) if controls else None,
                on_control=lambda control, outcome, detail: outcomes.append(
                    (control.control_id, outcome, detail)
                ),
            )

        self.assertEqual(len(user_messages), 2)
        self.assertEqual(
            outcomes,
            [(44, "applied", "Claude acknowledged the interrupt.")],
        )

    def test_claude_streams_visible_text_blocks_as_incremental_progress(self):
        process = FakeProcess(lambda payload: None)

        def emit_text_message(session_id, text, chunks):
            process.stdout.emit(
                {
                    "type": "stream_event",
                    "session_id": session_id,
                    "event": {"type": "message_start"},
                }
            )
            process.stdout.emit(
                {
                    "type": "stream_event",
                    "session_id": session_id,
                    "event": {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                }
            )
            for chunk in chunks:
                process.stdout.emit(
                    {
                        "type": "stream_event",
                        "session_id": session_id,
                        "event": {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {
                                "type": "text_delta",
                                "text": chunk,
                            },
                        },
                    }
                )
            process.stdout.emit(
                {
                    "type": "stream_event",
                    "session_id": session_id,
                    "event": {
                        "type": "content_block_stop",
                        "index": 0,
                    },
                }
            )
            process.stdout.emit(
                {
                    "type": "assistant",
                    "session_id": session_id,
                    "uuid": str(uuid.uuid4()),
                    "message": {
                        "content": [{"type": "text", "text": text}]
                    },
                }
            )

        def handle(payload):
            self.claude_initialize(process, payload)
            if payload.get("type") == "user":
                session_id = payload["session_id"]
                process.stdout.emit(
                    {
                        "type": "system",
                        "subtype": "init",
                        "session_id": session_id,
                    }
                )
                emit_text_message(
                    session_id,
                    "I’m tracing the event stream.",
                    ["I’m tracing ", "the event stream."],
                )
                emit_text_message(
                    session_id,
                    "The update is complete.",
                    ["The update ", "is complete."],
                )
                process.stdout.emit(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "session_id": session_id,
                        "uuid": str(uuid.uuid4()),
                        "result": "The update is complete.",
                        "usage": {"input_tokens": 8, "output_tokens": 2},
                    }
                )

        process.on_payload = handle
        progress = []
        result = provider_adapters.ClaudePrintAdapter(
            binary="/bin/claude",
            poll_interval_seconds=0.01,
            _popen_factory=FakePopenFactory(process),
        ).run_turn(
            claude_agent(),
            "Inspect it.",
            None,
            lambda _session: None,
            lambda: None,
            lambda stage, detail: progress.append((stage, detail)),
        )

        visible = [detail for stage, detail in progress if stage == "commentary"]
        self.assertIn("I’m tracing the event stream.", visible)
        self.assertEqual(
            visible[-1],
            "I’m tracing the event stream.\n\nThe update is complete.",
        )
        self.assertEqual(result.final_text, "The update is complete.")

    def test_claude_uses_sdk_interrupt_and_polls_during_silence(self):
        process = FakeProcess(lambda payload: None)

        def handle(payload):
            self.claude_initialize(process, payload)
            if payload.get("type") == "user":
                process.stdout.emit(
                    {
                        "type": "system",
                        "subtype": "init",
                        "session_id": payload["session_id"],
                    }
                )
            if payload.get("request", {}).get("subtype") == "interrupt":
                process.stdout.emit(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": payload["request_id"],
                            "response": {},
                        },
                    }
                )

        process.on_payload = handle
        polls = []
        heartbeats = []
        outcomes = []

        def poll_control():
            polls.append(True)
            if len(polls) == 3:
                return provider_adapters.ProviderControl(52, "cancel")
            return None

        adapter = provider_adapters.ClaudePrintAdapter(
            binary="/bin/claude",
            poll_interval_seconds=0.01,
            _popen_factory=FakePopenFactory(process),
        )
        with self.assertRaises(provider_adapters.ProviderTurnCancelled):
            adapter.run_turn(
                claude_agent(),
                "long task",
                None,
                lambda _session: None,
                lambda: heartbeats.append(True),
                poll_control=poll_control,
                on_control=lambda control, outcome, detail: outcomes.append(
                    (control.control_id, outcome, detail)
                ),
            )

        self.assertGreaterEqual(len(heartbeats), 3)
        self.assertEqual(outcomes[0][0:2], (52, "applied"))
        interrupt = [
            payload
            for payload in process.payloads
            if payload.get("request", {}).get("subtype") == "interrupt"
        ]
        self.assertEqual(
            interrupt,
            [
                {
                    "type": "control_request",
                    "request_id": "tc-control-52",
                    "request": {"subtype": "interrupt"},
                }
            ],
        )

    def test_claude_cancel_error_uses_owned_process_group_fallback(self):
        process = FakeProcess(lambda payload: None)

        def handle(payload):
            self.claude_initialize(process, payload)
            if payload.get("type") == "user":
                process.stdout.emit(
                    {
                        "type": "system",
                        "subtype": "init",
                        "session_id": payload["session_id"],
                    }
                )
            if payload.get("request", {}).get("subtype") == "interrupt":
                process.stdout.emit(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "error",
                            "request_id": payload["request_id"],
                            "error": "SECRET reflected error",
                        },
                    }
                )

        process.on_payload = handle
        outcomes = []
        controls = [provider_adapters.ProviderControl(60, "cancel")]
        adapter = provider_adapters.ClaudePrintAdapter(
            binary="/bin/claude",
            poll_interval_seconds=0.01,
            _popen_factory=FakePopenFactory(process),
        )
        with self.assertRaises(provider_adapters.ProviderTurnCancelled):
            adapter.run_turn(
                claude_agent(),
                "long task",
                None,
                lambda _session: None,
                lambda: None,
                poll_control=lambda: controls.pop(0) if controls else None,
                on_control=lambda control, outcome, detail: outcomes.append(
                    (control.control_id, outcome, detail)
                ),
            )
        self.assertTrue(process.terminated)
        self.assertEqual(outcomes[0][0:2], (60, "applied"))
        self.assertIn("local process group", outcomes[0][2])
        self.assertNotIn("SECRET", outcomes[0][2])

    def test_optional_callbacks_preserve_existing_claude_call_shape(self):
        process = FakeProcess(lambda payload: None)

        def handle(payload):
            self.claude_initialize(process, payload)
            if payload.get("type") == "user":
                process.stdout.emit(
                    {
                        "type": "system",
                        "subtype": "init",
                        "session_id": payload["session_id"],
                    }
                )
                self.emit_claude_result(process, payload["session_id"])

        process.on_payload = handle
        adapter = provider_adapters.ClaudePrintAdapter(
            binary="/bin/claude",
            poll_interval_seconds=0.01,
            _popen_factory=FakePopenFactory(process),
        )
        result = adapter.run_turn(
            claude_agent(),
            "ordinary turn",
            None,
            lambda _session: None,
            lambda: None,
        )
        self.assertEqual(result.final_text, "Claude final")


if __name__ == "__main__":
    unittest.main()
