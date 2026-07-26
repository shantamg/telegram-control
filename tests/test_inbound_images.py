import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import on_message


class InboundImageTests(unittest.TestCase):
    def test_selects_largest_telegram_photo_rendition(self):
        selected = on_message.inbound_attachment(
            {
                "photo": [
                    {"file_id": "small", "file_size": 100, "width": 90, "height": 90},
                    {"file_id": "large", "file_size": 900, "width": 900, "height": 900},
                ]
            }
        )

        self.assertEqual(selected["file_id"], "large")
        self.assertEqual(selected["safe_filename"], "photo.jpg")
        self.assertEqual(selected["kind"], "image")

    def test_accepts_any_document_and_preserves_a_safe_filename(self):
        selected = on_message.inbound_attachment(
            {
                "document": {
                    "file_id": "archive",
                    "file_unique_id": "unique",
                    "file_name": "../../Quarterly report (final).xlsx",
                    "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                }
            }
        )

        self.assertEqual(selected["safe_filename"], "Quarterly_report__final_.xlsx")
        self.assertEqual(selected["kind"], "document")

    def test_persists_private_image_once_at_retry_stable_path(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "controller.sqlite3"
            calls = []

            def fake_download(file_id, destination, max_bytes):
                calls.append((file_id, max_bytes))
                destination.write_bytes(b"image-data")

            environment = {
                "TELEGRAM_CONTROL_DB": str(database),
                "TELEGRAM_CONTROL_JOB_ID": "42",
            }
            image = {
                "file_id": "telegram-file",
                "file_unique_id": "stable/id",
                "file_size": 10,
                "safe_filename": "camera shot.jpg",
                "kind": "image",
            }
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                on_message.bridge, "download_telegram_file", side_effect=fake_download
            ):
                first = on_message.persist_inbound_attachment(image)
                second = on_message.persist_inbound_attachment(image)

            self.assertEqual(first, second)
            self.assertEqual(first.read_bytes(), b"image-data")
            self.assertEqual(first.name, "stable_id--camera shot.jpg")
            self.assertEqual(first.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                calls,
                [("telegram-file", on_message.MAX_ATTACHMENT_BYTES)],
            )

    def test_attachment_prompt_includes_path_kind_and_caption(self):
        prompt = on_message.attachment_prompt(
            Path("/private/report.pdf"),
            "Summarize this",
            "document",
        )

        self.assertIn("Telegram document", prompt)
        self.assertIn("/private/report.pdf", prompt)
        self.assertIn("User caption:\nSummarize this", prompt)

    def test_acknowledgement_is_queued_before_attachment_download(self):
        source = inspect.getsource(on_message.main)
        acknowledgement = source.index(
            'send_message("📎 Attachment received. Downloading securely…")'
        )
        download = source.index("path = persist_inbound_attachment(attachment)")
        route = source.index("route_user_input(", download)

        self.assertLess(acknowledgement, download)
        self.assertLess(download, route)


if __name__ == "__main__":
    unittest.main()
