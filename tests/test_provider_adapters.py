import unittest

import provider_adapters


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


if __name__ == "__main__":
    unittest.main()
