import unittest

import provider_adapters
from durable_store import ManagedAgent


class CodexEventTests(unittest.TestCase):
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
        self.assertIn("bypassPermissions", fresh)
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


if __name__ == "__main__":
    unittest.main()
