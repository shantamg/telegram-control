import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import on_message


class InboundImageTests(unittest.TestCase):
    def test_selects_largest_telegram_photo_rendition(self):
        selected = on_message.inbound_image(
            {
                "photo": [
                    {"file_id": "small", "file_size": 100, "width": 90, "height": 90},
                    {"file_id": "large", "file_size": 900, "width": 900, "height": 900},
                ]
            }
        )

        self.assertEqual(selected["file_id"], "large")
        self.assertEqual(selected["extension"], ".jpg")

    def test_accepts_supported_image_document_and_rejects_other_document(self):
        selected = on_message.inbound_image(
            {
                "document": {
                    "file_id": "png",
                    "file_unique_id": "unique",
                    "mime_type": "image/png",
                }
            }
        )

        self.assertEqual(selected["extension"], ".png")
        self.assertIsNone(
            on_message.inbound_image(
                {"document": {"file_id": "pdf", "mime_type": "application/pdf"}}
            )
        )

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
                "extension": ".jpg",
            }
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                on_message.bridge, "download_telegram_file", side_effect=fake_download
            ):
                first = on_message.persist_inbound_image(image)
                second = on_message.persist_inbound_image(image)

            self.assertEqual(first, second)
            self.assertEqual(first.read_bytes(), b"image-data")
            self.assertEqual(first.name, "stable_id.jpg")
            self.assertEqual(first.stat().st_mode & 0o777, 0o600)
            self.assertEqual(calls, [("telegram-file", on_message.MAX_IMAGE_BYTES)])

    def test_image_prompt_includes_absolute_path_and_caption(self):
        prompt = on_message.image_prompt(Path("/private/image.png"), "What is this?")

        self.assertIn("/private/image.png", prompt)
        self.assertIn("User caption:\nWhat is this?", prompt)


if __name__ == "__main__":
    unittest.main()
