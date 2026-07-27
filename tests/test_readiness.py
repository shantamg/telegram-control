import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import on_message
import telegram_control
from durable_store import StoreError


class ReadinessTests(unittest.TestCase):
    def test_executable_status_reports_version_and_failure(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            binary = Path(temporary_directory) / "provider"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o700)
            with mock.patch.object(
                telegram_control.subprocess,
                "run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout="provider 1.2.3\n",
                    stderr="",
                ),
            ):
                self.assertEqual(
                    telegram_control.executable_status(str(binary)),
                    (True, "provider 1.2.3"),
                )
        self.assertEqual(
            telegram_control.executable_status(None),
            (False, "not installed"),
        )

    def test_doctor_accepts_one_provider_and_optional_missing_helpers(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = SimpleNamespace(db=root / "controller.sqlite3")
            config = {
                "chat_id": 123,
                "handler_path": str(Path(on_message.__file__).resolve()),
            }
            missing = root / "missing"
            with mock.patch.object(
                telegram_control.sys,
                "platform",
                "darwin",
            ), mock.patch.object(
                telegram_control.bridge,
                "load_config",
                return_value=config,
            ), mock.patch.object(
                telegram_control.bridge,
                "read_token",
                return_value="test-token",
            ), mock.patch.object(
                telegram_control.provider_adapters,
                "provider_availability",
                return_value={"claude": "/bin/claude", "codex": None},
            ), mock.patch.object(
                telegram_control,
                "executable_status",
                side_effect=lambda path: (
                    (True, "Claude Code 1.0")
                    if path
                    else (False, "not installed")
                ),
            ), mock.patch.object(
                telegram_control.helper_paths,
                "resolve_binary",
                return_value=missing,
            ), mock.patch.object(
                telegram_control,
                "HANDY_MODEL_DIR",
                missing,
            ), mock.patch.object(
                telegram_control.shutil,
                "which",
                return_value=None,
            ):
                telegram_control.doctor_command(args)

    def test_doctor_rejects_control_without_codex(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            args = SimpleNamespace(db=root / "controller.sqlite3")
            config = {
                "chat_id": 123,
                "handler_path": str(Path(on_message.__file__).resolve()),
                "telegram_control": {
                    "control_agent": {"enabled": True},
                },
            }
            missing = root / "missing"
            with mock.patch.object(
                telegram_control.sys,
                "platform",
                "darwin",
            ), mock.patch.object(
                telegram_control.bridge,
                "load_config",
                return_value=config,
            ), mock.patch.object(
                telegram_control.bridge,
                "read_token",
                return_value="test-token",
            ), mock.patch.object(
                telegram_control.provider_adapters,
                "provider_availability",
                return_value={"claude": "/bin/claude", "codex": None},
            ), mock.patch.object(
                telegram_control,
                "executable_status",
                side_effect=lambda path: (
                    (True, "Claude Code 1.0")
                    if path
                    else (False, "not installed")
                ),
            ), mock.patch.object(
                telegram_control.helper_paths,
                "resolve_binary",
                return_value=missing,
            ), mock.patch.object(
                telegram_control,
                "HANDY_MODEL_DIR",
                missing,
            ), mock.patch.object(
                telegram_control.shutil,
                "which",
                return_value=None,
            ):
                with self.assertRaisesRegex(
                    StoreError,
                    "Doctor found 1 problem",
                ):
                    telegram_control.doctor_command(args)

    def test_bootstrap_checks_installs_and_reports_status(self):
        args = SimpleNamespace()
        calls = []
        with mock.patch.object(
            telegram_control,
            "doctor_command",
            side_effect=lambda _: calls.append("doctor"),
        ), mock.patch.object(
            telegram_control,
            "install_command",
            side_effect=lambda _: calls.append("install"),
        ), mock.patch.object(
            telegram_control,
            "status_command",
            side_effect=lambda _: calls.append("status"),
        ):
            telegram_control.bootstrap_command(args)

        self.assertEqual(calls, ["doctor", "install", "status"])

    def test_config_show_prints_effective_workspace_layers(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / ".telegram-control.json").write_text(
                json.dumps(
                    {"presentation": {"status_style": "compact"}}
                ),
                encoding="utf-8",
            )
            args = SimpleNamespace(
                workspace=str(workspace),
                effective=True,
            )
            output = StringIO()
            with mock.patch.object(
                telegram_control.bridge,
                "load_config",
                return_value={},
            ), redirect_stdout(output):
                telegram_control.config_show_command(args)

        payload = json.loads(output.getvalue())
        self.assertEqual(
            payload["effective"]["presentation"]["status_style"],
            "compact",
        )
        self.assertTrue(
            payload["layers"]["workspace"].endswith(
                ".telegram-control.json"
            )
        )


if __name__ == "__main__":
    unittest.main()
