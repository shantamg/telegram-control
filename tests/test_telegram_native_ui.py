import unittest

import telegram_bridge
import telegram_native_ui


class TelegramNativeUiTests(unittest.TestCase):
    def test_force_reply_has_a_guided_composer_placeholder(self):
        self.assertEqual(
            telegram_native_ui.force_reply_markup("  Paste   a folder path  "),
            {
                "force_reply": True,
                "input_field_placeholder": "Paste a folder path",
                "selective": True,
            },
        )
        with self.assertRaises(ValueError):
            telegram_native_ui.force_reply_markup("")

    def test_showcase_keyboard_uses_native_styles_and_copy_actions(self):
        buttons = telegram_native_ui.showcase_keyboard()["inline_keyboard"][0]
        self.assertEqual(
            [button["style"] for button in buttons],
            ["primary", "success", "danger"],
        )
        self.assertTrue(all("copy_text" in button for button in buttons))
        self.assertTrue(all("callback_data" not in button for button in buttons))

    def test_group_showcase_sends_every_supported_surface(self):
        calls = []

        def fake_api_call(token, method, **params):
            calls.append((token, method, params))
            return {"message_id": 90}

        result = telegram_native_ui.send_showcase(
            "token",
            chat_id=-100123,
            message_thread_id=44,
            receiver_user_id=7,
            source_message_id=8,
            api_call=fake_api_call,
        )

        self.assertTrue(result["rich_message"])
        self.assertTrue(result["styled_buttons"])
        self.assertTrue(result["reaction"])
        self.assertTrue(result["ephemeral_hint"])
        self.assertTrue(result["force_reply"])
        self.assertFalse(result["chat_picker"])
        self.assertEqual(
            [method for _, method, _ in calls],
            [
                "sendRichMessage",
                "setMessageReaction",
                "sendMessage",
                "sendMessage",
            ],
        )
        self.assertEqual(calls[1][2]["message_id"], 8)
        self.assertEqual(calls[2][2]["receiver_user_id"], 7)
        self.assertTrue(calls[3][2]["reply_markup"]["force_reply"])

    def test_rich_message_rejection_falls_back_to_regular_html(self):
        calls = []

        def fake_api_call(token, method, **params):
            calls.append((method, params))
            if method == "sendRichMessage":
                raise telegram_bridge.BridgeError("not supported")
            return {"message_id": 91}

        result = telegram_native_ui.send_showcase(
            "token",
            chat_id=-100123,
            message_thread_id=44,
            api_call=fake_api_call,
        )

        self.assertFalse(result["rich_message"])
        self.assertTrue(result["styled_buttons"])
        self.assertEqual(calls[1][0], "sendMessage")
        self.assertEqual(calls[1][1]["parse_mode"], "HTML")


if __name__ == "__main__":
    unittest.main()
