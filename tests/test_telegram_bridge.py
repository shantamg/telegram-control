import unittest

import telegram_bridge


VALID_TOKEN = "123456789:" + ("A" * 35)


class TokenValidationTests(unittest.TestCase):
    def test_accepts_valid_token(self):
        self.assertEqual(
            telegram_bridge.clean_and_validate_token(f"  {VALID_TOKEN}\n"),
            VALID_TOKEN,
        )

    def test_removes_bracketed_paste_escape_prefix(self):
        pasted = f"\x1b[200~{VALID_TOKEN}\x1b[201~"
        self.assertEqual(
            telegram_bridge.clean_and_validate_token(pasted),
            VALID_TOKEN,
        )

    def test_rejects_control_character_inside_token(self):
        with self.assertRaises(telegram_bridge.BridgeError):
            telegram_bridge.clean_and_validate_token(
                VALID_TOKEN[:10] + "\n" + VALID_TOKEN[10:]
            )

    def test_rejects_non_token_text(self):
        with self.assertRaises(telegram_bridge.BridgeError):
            telegram_bridge.clean_and_validate_token("not a token")


class MessageDescriptionTests(unittest.TestCase):
    def test_describes_sender_without_mutating_message(self):
        message = {
            "from": {
                "first_name": "Test",
                "last_name": "Person",
                "username": "tester",
            },
            "chat": {"id": 1234},
        }
        snapshot = {
            "from": dict(message["from"]),
            "chat": dict(message["chat"]),
        }

        description = telegram_bridge.describe_message(message)

        self.assertEqual(
            description,
            "Test Person (@tester), chat ID 1234",
        )
        self.assertEqual(message, snapshot)

    def test_ignores_automatic_topic_root_reply(self):
        message = {
            "message_id": 43,
            "message_thread_id": 62,
            "is_topic_message": True,
            "reply_to_message": {
                "message_id": 42,
                "message_thread_id": 62,
                "forum_topic_created": {"name": "Stage 2 Test"},
            },
        }

        self.assertEqual(
            telegram_bridge.explicit_reply_message_id(message),
            "",
        )

    def test_preserves_explicit_reply_inside_topic(self):
        message = {
            "message_id": 45,
            "message_thread_id": 62,
            "is_topic_message": True,
            "reply_to_message": {
                "message_id": 44,
                "message_thread_id": 62,
                "text": "bot response",
            },
        }

        self.assertEqual(
            telegram_bridge.explicit_reply_message_id(message),
            "44",
        )


if __name__ == "__main__":
    unittest.main()
