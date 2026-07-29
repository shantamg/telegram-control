from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import local_actions


class LocalActionTests(unittest.TestCase):
    def write_config(
        self,
        directory: Path,
        *,
        mode: int = 0o600,
    ) -> Path:
        path = directory / "local-actions.json"
        path.write_text(
            json.dumps(
                {
                    "actions": {
                        "test-check": {
                            "argv": [
                                sys.executable,
                                "-c",
                                "print('check complete')",
                            ],
                            "working_directory": str(directory),
                            "timeout_seconds": 5,
                        }
                    }
                }
            )
        )
        path.chmod(mode)
        return path

    def test_runs_allowlisted_action_without_shell(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            path = self.write_config(directory)
            self.assertEqual(
                local_actions.run_local_action(
                    "test-check",
                    config_path=path,
                ),
                "check complete",
            )

    def test_rejects_unknown_action(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            path = self.write_config(directory)
            with self.assertRaisesRegex(
                local_actions.LocalActionError,
                "not allowlisted",
            ):
                local_actions.load_local_action(
                    "missing-action",
                    config_path=path,
                )

    def test_rejects_writable_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            path = self.write_config(directory, mode=0o666)
            with self.assertRaisesRegex(
                local_actions.LocalActionError,
                "must not be group- or world-writable",
            ):
                local_actions.load_local_action(
                    "test-check",
                    config_path=path,
                )


if __name__ == "__main__":
    unittest.main()

