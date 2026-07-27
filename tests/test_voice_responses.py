import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import voice_responses


class VoiceResponseTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.speech_dir = self.root / "speech"
        self.edge = self.root / "edge-tts"
        self.ffmpeg = self.root / "ffmpeg"
        self.edge.write_text("#!/bin/sh\n")
        self.ffmpeg.write_text("#!/bin/sh\n")
        self.edge.chmod(0o700)
        self.ffmpeg.chmod(0o700)
        self.patches = [
            mock.patch.object(voice_responses, "SPEECH_DIR", self.speech_dir),
            mock.patch.object(voice_responses, "EDGE_TTS_BINARY", self.edge),
            mock.patch.object(voice_responses, "FFMPEG_BINARY", self.ffmpeg),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.temporary_directory.cleanup()

    def test_speech_text_removes_terminal_markup_and_bounds_length(self):
        source = (
            "# Result\n"
            "Use [`the guide`](https://example.com/secret) and `status`.\n"
            "```python\nprint('secret code')\n```\n"
            + ("word " * 1_000)
        )
        spoken = voice_responses.speech_text(source)
        self.assertIn("the guide", spoken)
        self.assertIn("status", spoken)
        self.assertIn("Code block omitted.", spoken)
        self.assertNotIn("https://", spoken)
        self.assertNotIn("secret code", spoken)
        self.assertLessEqual(
            len(spoken),
            voice_responses.MAX_SPEECH_CHARACTERS,
        )
        self.assertTrue(spoken.endswith("text response."))

    def test_synthesis_is_private_deterministic_and_cleans_intermediates(self):
        def fake_run(arguments, **_kwargs):
            if "--write-media" in arguments:
                output = Path(arguments[arguments.index("--write-media") + 1])
            else:
                output = Path(arguments[-1])
            output.write_bytes(b"voice-bytes")
            return subprocess.CompletedProcess(arguments, 0)

        with mock.patch.object(
            voice_responses.subprocess,
            "run",
            side_effect=fake_run,
        ) as run:
            result = voice_responses.synthesize_voice(
                "A concise response.",
                "agent-12-request-44",
                voice_name="en-US-AndrewNeural",
                rate="-10%",
            )
            repeated = voice_responses.synthesize_voice(
                "This different text is not regenerated on retry.",
                "agent-12-request-44",
                voice_name="en-US-AndrewNeural",
                rate="-10%",
            )

        self.assertEqual(result, repeated)
        self.assertEqual(run.call_count, 2)
        self.assertIn("en-US-AndrewNeural", run.call_args_list[0].args[0])
        self.assertIn("-10%", run.call_args_list[0].args[0])
        self.assertEqual(result.read_bytes(), b"voice-bytes")
        self.assertEqual(result.stat().st_mode & 0o777, 0o600)
        self.assertFalse(result.with_suffix(".txt").exists())
        self.assertFalse(result.with_suffix(".mp3").exists())
        self.assertEqual(
            voice_responses.validate_voice_path(str(result)),
            result.resolve(),
        )

    def test_validation_rejects_outside_or_overly_open_files(self):
        outside = self.root / "outside.ogg"
        outside.write_bytes(b"voice")
        outside.chmod(0o600)
        with self.assertRaisesRegex(
            voice_responses.VoiceResponseError,
            "unavailable",
        ):
            voice_responses.validate_voice_path(str(outside))

        self.speech_dir.mkdir(mode=0o700, exist_ok=True)
        unsafe = self.speech_dir / "unsafe.ogg"
        unsafe.write_bytes(b"voice")
        unsafe.chmod(0o644)
        with self.assertRaisesRegex(
            voice_responses.VoiceResponseError,
            "permissions",
        ):
            voice_responses.validate_voice_path(str(unsafe))

    def test_cleanup_removes_only_stale_files(self):
        self.speech_dir.mkdir(mode=0o700)
        stale = self.speech_dir / "stale-orphan.ogg"
        protected = self.speech_dir / "stale-pending.ogg"
        current = self.speech_dir / "current.ogg"
        stale.write_bytes(b"old")
        protected.write_bytes(b"pending")
        current.write_bytes(b"new")
        stale.chmod(0o600)
        protected.chmod(0o600)
        current.chmod(0o600)
        os.utime(stale, (1, 1))
        os.utime(protected, (1, 1))
        os.utime(current, (100_000, 100_000))

        voice_responses.cleanup_stale_files(
            now=voice_responses.STALE_FILE_SECONDS + 10,
            protected_paths={str(protected)},
        )

        self.assertFalse(stale.exists())
        self.assertTrue(protected.exists())
        self.assertTrue(current.exists())

    def test_failed_encode_never_publishes_partial_cache(self):
        def failed_run(arguments, **_kwargs):
            if "--write-media" in arguments:
                output = Path(arguments[arguments.index("--write-media") + 1])
                output.write_bytes(b"media")
                return subprocess.CompletedProcess(arguments, 0)
            Path(arguments[-1]).write_bytes(b"partial-voice")
            return subprocess.CompletedProcess(arguments, 1)

        with mock.patch.object(
            voice_responses.subprocess,
            "run",
            side_effect=failed_run,
        ):
            with self.assertRaisesRegex(
                voice_responses.VoiceResponseError,
                "encoding failed",
            ):
                voice_responses.synthesize_voice(
                    "Retry me.",
                    "atomic-retry",
                    protected_paths=set(),
                )

        final_path = self.speech_dir / "atomic-retry.ogg"
        self.assertFalse(final_path.exists())
        self.assertEqual(list(self.speech_dir.glob(".atomic-retry-*")), [])

        def successful_run(arguments, **_kwargs):
            if "--write-media" in arguments:
                output = Path(arguments[arguments.index("--write-media") + 1])
            else:
                output = Path(arguments[-1])
            output.write_bytes(b"complete-voice")
            return subprocess.CompletedProcess(arguments, 0)

        with mock.patch.object(
            voice_responses.subprocess,
            "run",
            side_effect=successful_run,
        ):
            result = voice_responses.synthesize_voice(
                "Retry me.",
                "atomic-retry",
                protected_paths=set(),
            )
        self.assertEqual(result.read_bytes(), b"complete-voice")

    def test_encode_timeout_cleans_every_partial_file(self):
        def timed_out_run(arguments, **_kwargs):
            if "--write-media" in arguments:
                output = Path(arguments[arguments.index("--write-media") + 1])
                output.write_bytes(b"media")
                return subprocess.CompletedProcess(arguments, 0)
            Path(arguments[-1]).write_bytes(b"partial-voice")
            raise subprocess.TimeoutExpired(arguments[0], 1)

        with mock.patch.object(
            voice_responses.subprocess,
            "run",
            side_effect=timed_out_run,
        ):
            with self.assertRaisesRegex(
                voice_responses.VoiceResponseError,
                "generation failed",
            ):
                voice_responses.synthesize_voice(
                    "Timeout.",
                    "timeout-retry",
                    protected_paths=set(),
                )

        self.assertFalse((self.speech_dir / "timeout-retry.ogg").exists())
        self.assertEqual(list(self.speech_dir.glob(".timeout-retry-*")), [])


if __name__ == "__main__":
    unittest.main()
