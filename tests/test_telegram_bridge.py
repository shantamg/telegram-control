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


if __name__ == "__main__":
    unittest.main()
