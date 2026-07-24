import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import claude_sessions


class ClaudeSessionDiscoveryTests(unittest.TestCase):
    def write_session(
        self,
        projects_root: Path,
        session_id: str,
        cwd: str,
        *,
        prompt: str = "Review the current plan",
        mtime: float = 100,
    ) -> Path:
        directory = projects_root / claude_sessions._encoded_project_name(cwd)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{session_id}.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "mode",
                            "mode": "normal",
                            "sessionId": session_id,
                        }
                    ),
                    json.dumps(
                        {
                            "type": "user",
                            "sessionId": session_id,
                            "cwd": cwd,
                            "entrypoint": "cli",
                            "message": {
                                "role": "user",
                                "content": prompt,
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.utime(path, (mtime, mtime))
        return path

    def test_discovers_recent_exact_directory_sessions(self):
        first_id = "6054b13e-168d-4940-a58e-f60f5d34d9e7"
        second_id = "9447e3eb-6258-4893-8a9b-469cb283505d"
        excluded_id = "fa64c55f-c9b1-432d-a944-857fd4768965"
        with tempfile.TemporaryDirectory() as temporary_directory:
            projects_root = Path(temporary_directory) / "projects"
            workspace = Path(temporary_directory) / "workspace"
            workspace.mkdir()
            other = Path(temporary_directory) / "other"
            other.mkdir()
            self.write_session(
                projects_root,
                first_id,
                str(workspace),
                prompt="Investigate the journal layout",
                mtime=300,
            )
            self.write_session(
                projects_root,
                second_id,
                str(workspace),
                mtime=200,
            )
            self.write_session(
                projects_root,
                excluded_id,
                str(workspace),
                mtime=400,
            )
            self.write_session(
                projects_root,
                "6ac4b13e-168d-4940-a58e-f60f5d34d9e7",
                str(other),
                mtime=500,
            )

            sessions = claude_sessions.discover_sessions(
                str(workspace),
                projects_root=projects_root,
                excluded_session_ids={excluded_id},
            )

            self.assertEqual(
                [session.session_id for session in sessions],
                [first_id, second_id],
            )
            self.assertEqual(sessions[0].title, "Investigate the journal layout")
            self.assertIn(
                "Investigate the journal layout",
                sessions[0].button_label(),
            )

    def test_resolve_revalidates_id_and_directory(self):
        session_id = "6054b13e-168d-4940-a58e-f60f5d34d9e7"
        with tempfile.TemporaryDirectory() as temporary_directory:
            projects_root = Path(temporary_directory) / "projects"
            workspace = Path(temporary_directory) / "workspace"
            workspace.mkdir()
            other = Path(temporary_directory) / "other"
            other.mkdir()
            self.write_session(projects_root, session_id, str(workspace))

            self.assertEqual(
                claude_sessions.resolve_session(
                    session_id,
                    str(workspace),
                    projects_root=projects_root,
                ).session_id,
                session_id,
            )
            self.assertIsNone(
                claude_sessions.resolve_session(
                    session_id,
                    str(other),
                    projects_root=projects_root,
                )
            )
            self.assertIsNone(
                claude_sessions.resolve_session(
                    "../../not-a-session",
                    str(workspace),
                    projects_root=projects_root,
                )
            )

    def test_discovery_caps_files_and_rejects_oversized_records(self):
        ids = [
            "6054b13e-168d-4940-a58e-f60f5d34d9e7",
            "9447e3eb-6258-4893-8a9b-469cb283505d",
            "fa64c55f-c9b1-432d-a944-857fd4768965",
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            projects_root = Path(temporary_directory) / "projects"
            workspace = Path(temporary_directory) / "workspace"
            workspace.mkdir()
            for index, session_id in enumerate(ids):
                self.write_session(
                    projects_root,
                    session_id,
                    str(workspace),
                    mtime=100 + index,
                )
            directory = (
                projects_root
                / claude_sessions._encoded_project_name(str(workspace))
            )
            oversized_id = "6ac4b13e-168d-4940-a58e-f60f5d34d9e7"
            (directory / f"{oversized_id}.jsonl").write_bytes(
                b'{"type":"user","padding":"'
                + (b"x" * (claude_sessions.MAX_SCAN_BYTES + 1))
                + b'"}\n'
            )
            with mock.patch.object(
                claude_sessions,
                "MAX_SESSION_FILES",
                2,
            ):
                sessions = claude_sessions.discover_sessions(
                    str(workspace),
                    projects_root=projects_root,
                    limit=5,
                )
            self.assertLessEqual(len(sessions), 2)
            self.assertNotIn(
                oversized_id,
                {session.session_id for session in sessions},
            )


if __name__ == "__main__":
    unittest.main()
