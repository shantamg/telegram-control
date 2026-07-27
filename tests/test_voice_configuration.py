import json
import os
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

import on_message
import voice_settings
from durable_store import DurableStore, StoreError


def message_update(update_id=10, text="/voice"):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 99,
            "from": {"id": 123, "username": "tester"},
            "chat": {"id": 123, "type": "private"},
            "text": text,
        },
    }


def callback_update(update_id, data, message_id=99):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": 123, "username": "tester"},
            "data": data,
            "message": {
                "message_id": message_id,
                "chat": {"id": 123, "type": "private"},
            },
        },
    }


class VoiceConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "controller.sqlite3"
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _job_id(self, store, update_id):
        row = store.connection.execute(
            "SELECT job_id FROM inbox_jobs WHERE update_id = ?",
            (int(update_id),),
        ).fetchone()
        return int(row["job_id"])

    def _environment(self, job_id, message_id=99):
        return {
            "TELEGRAM_CONTROL_DB": str(self.database_path),
            "TELEGRAM_CONTROL_JOB_ID": str(job_id),
            "TELEGRAM_CHAT_ID": "123",
            "TELEGRAM_CHAT_TYPE": "private",
            "TELEGRAM_FROM_ID": "123",
            "TELEGRAM_MESSAGE_ID": str(message_id),
        }

    def _invoke_command(self, store):
        update = message_update()
        store.ingest_update(update, now=100)
        job_id = self._job_id(store, update["update_id"])
        with mock.patch.dict(
            os.environ,
            self._environment(job_id),
            clear=True,
        ), mock.patch.object(
            on_message.sys,
            "stdin",
            StringIO(json.dumps(update)),
        ):
            self.assertEqual(on_message.main(), 0)
        row = store.connection.execute(
            """
            SELECT params_json FROM outbox_messages
            WHERE operation_id LIKE ? AND method = 'sendMessage'
            """,
            (f"inbox:{job_id}:%",),
        ).fetchone()
        return json.loads(row["params_json"])

    def _invoke_callback(self, store, update_id, callback_data):
        update = callback_update(update_id, callback_data)
        store.ingest_update(update, now=100 + update_id)
        job_id = self._job_id(store, update_id)
        with mock.patch.dict(
            os.environ,
            self._environment(job_id),
            clear=True,
        ):
            on_message.handle_callback(update, update["callback_query"])
        rows = store.connection.execute(
            """
            SELECT method, params_json FROM outbox_messages
            WHERE operation_id LIKE ?
            ORDER BY message_id
            """,
            (f"inbox:{job_id}:%",),
        ).fetchall()
        return job_id, [
            (str(row["method"]), json.loads(row["params_json"]))
            for row in rows
        ]

    def test_store_defaults_persists_and_rejects_invalid_voice_configuration(self):
        with DurableStore(self.database_path) as store:
            self.assertEqual(
                store.voice_configuration(),
                voice_settings.DEFAULT_CONFIGURATION,
            )
            selected = voice_settings.VoiceConfiguration(
                voice_name="en-US-AndrewNeural",
                rate="-10%",
            )
            self.assertEqual(store.set_voice_configuration(selected), selected)
            self.assertEqual(store.voice_configuration(), selected)
            with self.assertRaisesRegex(StoreError, "unsupported"):
                store.set_voice_configuration(
                    voice_settings.VoiceConfiguration(
                        voice_name="not-a-real-voice",
                        rate="+10%",
                    )
                )

    def test_voice_picker_previews_before_confirm_and_back_does_not_apply(self):
        with DurableStore(self.database_path) as store:
            picker = self._invoke_command(store)
            self.assertIn("Spoken reply settings", picker["text"])
            self.assertIn("🇬🇧 Sonia", picker["text"])
            labels = [
                button["text"]
                for row in picker["reply_markup"]["inline_keyboard"]
                for button in row
            ]
            self.assertIn("🇺🇸 Andrew", labels)
            self.assertIn("✓ Quick · +10%", labels)
            andrew_data = next(
                button["callback_data"]
                for row in picker["reply_markup"]["inline_keyboard"]
                for button in row
                if button["text"] == "🇺🇸 Andrew"
            )

            _job_id, selection_calls = self._invoke_callback(
                store,
                11,
                andrew_data,
            )
            review = next(
                params
                for method, params in selection_calls
                if method == "editMessageText"
            )
            self.assertIn("Review spoken reply settings", review["text"])
            self.assertIn("Selected: 🇺🇸 Andrew", review["text"])
            review_buttons = {
                button["text"]: button["callback_data"]
                for row in review["reply_markup"]["inline_keyboard"]
                for button in row
            }
            self.assertEqual(
                set(review_buttons),
                {"🔊 Preview", "✅ Confirm", "‹ Back"},
            )
            self.assertEqual(
                store.voice_configuration(),
                voice_settings.DEFAULT_CONFIGURATION,
            )

            preview_path = Path(self.temporary_directory.name) / "preview.ogg"
            preview_path.write_bytes(b"voice")
            preview_update = callback_update(
                12,
                review_buttons["🔊 Preview"],
            )
            store.ingest_update(preview_update, now=112)
            preview_job_id = self._job_id(store, 12)
            with mock.patch.dict(
                os.environ,
                self._environment(preview_job_id),
                clear=True,
            ), mock.patch.object(
                on_message.voice_responses,
                "synthesize_voice",
                return_value=preview_path,
            ) as synthesize:
                on_message.handle_callback(
                    preview_update,
                    preview_update["callback_query"],
                )
            synthesize.assert_called_once_with(
                (
                    "Hello. This is Andrew, your Telegram Control voice. "
                    "Spoken replies will sound like this."
                ),
                f"voice-preview-{preview_job_id}",
                protected_paths=set(),
                voice_name="en-US-AndrewNeural",
                rate="+10%",
            )
            preview_voice = store.connection.execute(
                """
                SELECT params_json FROM outbox_messages
                WHERE operation_id = ?
                """,
                (f"inbox:{preview_job_id}:voice-config-preview",),
            ).fetchone()
            self.assertEqual(
                json.loads(preview_voice["params_json"])[
                    "__voice_file_path"
                ],
                str(preview_path),
            )
            self.assertEqual(
                store.voice_configuration(),
                voice_settings.DEFAULT_CONFIGURATION,
            )

            _job_id, back_calls = self._invoke_callback(
                store,
                13,
                review_buttons["‹ Back"],
            )
            back = next(
                params
                for method, params in back_calls
                if method == "editMessageText"
            )
            self.assertIn("Spoken reply settings", back["text"])
            self.assertEqual(
                store.voice_configuration(),
                voice_settings.DEFAULT_CONFIGURATION,
            )

            second_andrew_data = next(
                button["callback_data"]
                for row in back["reply_markup"]["inline_keyboard"]
                for button in row
                if button["text"] == "🇺🇸 Andrew"
            )
            _job_id, second_selection_calls = self._invoke_callback(
                store,
                14,
                second_andrew_data,
            )
            second_review = next(
                params
                for method, params in second_selection_calls
                if method == "editMessageText"
            )
            confirm_data = next(
                button["callback_data"]
                for row in second_review["reply_markup"]["inline_keyboard"]
                for button in row
                if button["text"] == "✅ Confirm"
            )
            _job_id, confirm_calls = self._invoke_callback(
                store,
                15,
                confirm_data,
            )
            confirmed = next(
                params
                for method, params in confirm_calls
                if method == "editMessageText"
            )
            self.assertIn("Spoken reply settings updated", confirmed["text"])
            self.assertEqual(
                confirmed["reply_markup"],
                {"inline_keyboard": []},
            )
            self.assertEqual(
                store.voice_configuration(),
                voice_settings.VoiceConfiguration(
                    voice_name="en-US-AndrewNeural",
                    rate="+10%",
                ),
            )


if __name__ == "__main__":
    unittest.main()
