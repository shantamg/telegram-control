import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import codex_sessions


class CodexSessionDiscoveryTests(unittest.TestCase):
    def write_session(
        self,
        root: Path,
        session_id: str,
        cwd: str,
        *,
        originator: str = "Codex Desktop",
        mtime: float = 100,
    ) -> Path:
        directory = root / "2026" / "07" / "24"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"rollout-test-{session_id}.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": session_id,
                        "cwd": cwd,
                        "originator": originator,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        os.utime(path, (mtime, mtime))
        return path

    def test_discovers_recent_exact_directory_sessions_with_titles(self):
        first_id = "019f94f0-6c2d-7871-9456-88008cca34da"
        second_id = "019f94e5-f33b-7e63-83e2-c31554531867"
        excluded_id = "019f90ed-ca65-7510-b018-1fbd60d6f07d"
        controller_id = "019f8f19-3dab-7300-a270-f18441347b44"
        with tempfile.TemporaryDirectory() as temporary_directory:
            codex_root = Path(temporary_directory) / ".codex"
            sessions_root = codex_root / "sessions"
            workspace = Path(temporary_directory) / "workspace"
            workspace.mkdir()
            other = Path(temporary_directory) / "other"
            other.mkdir()
            self.write_session(sessions_root, first_id, str(workspace), mtime=300)
            self.write_session(sessions_root, second_id, str(workspace), mtime=200)
            self.write_session(sessions_root, excluded_id, str(workspace), mtime=400)
            self.write_session(
                sessions_root,
                controller_id,
                str(workspace),
                originator="telegram-control",
                mtime=500,
            )
            self.write_session(
                sessions_root,
                "019f8f31-df53-76a1-9877-090add1fe8a0",
                str(other),
                mtime=600,
            )
            (codex_root / "session_index.jsonl").write_text(
                json.dumps(
                    {
                        "id": first_id,
                        "thread_name": "Investigate the journal layout",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            sessions = codex_sessions.discover_sessions(
                str(workspace),
                sessions_root=sessions_root,
                excluded_session_ids={excluded_id},
            )

            self.assertEqual(
                [session.session_id for session in sessions],
                [first_id, second_id],
            )
            self.assertEqual(
                sessions[0].title,
                "Investigate the journal layout",
            )
            self.assertIn("Investigate the journal layout", sessions[0].button_label())

    def test_resolve_revalidates_id_directory_and_origin(self):
        session_id = "019f94f0-6c2d-7871-9456-88008cca34da"
        controller_id = "019f94e5-f33b-7e63-83e2-c31554531867"
        with tempfile.TemporaryDirectory() as temporary_directory:
            sessions_root = Path(temporary_directory) / ".codex" / "sessions"
            workspace = Path(temporary_directory) / "workspace"
            workspace.mkdir()
            other = Path(temporary_directory) / "other"
            other.mkdir()
            self.write_session(sessions_root, session_id, str(workspace))
            self.write_session(
                sessions_root,
                controller_id,
                str(workspace),
                originator="telegram-control",
            )

            self.assertEqual(
                codex_sessions.resolve_session(
                    session_id,
                    str(workspace),
                    sessions_root=sessions_root,
                ).session_id,
                session_id,
            )
            self.assertIsNone(
                codex_sessions.resolve_session(
                    session_id,
                    str(other),
                    sessions_root=sessions_root,
                )
            )
            self.assertIsNone(
                codex_sessions.resolve_session(
                    controller_id,
                    str(workspace),
                    sessions_root=sessions_root,
                )
            )
            self.assertIsNone(
                codex_sessions.resolve_session(
                    "../../not-a-session",
                    str(workspace),
                    sessions_root=sessions_root,
                )
            )

    def test_discovery_caps_files_and_rejects_oversized_metadata(self):
        ids = [
            "019f94f0-6c2d-7871-9456-88008cca34da",
            "019f94e5-f33b-7e63-83e2-c31554531867",
            "019f90ed-ca65-7510-b018-1fbd60d6f07d",
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            sessions_root = Path(temporary_directory) / ".codex" / "sessions"
            workspace = Path(temporary_directory) / "workspace"
            workspace.mkdir()
            for index, session_id in enumerate(ids):
                self.write_session(
                    sessions_root,
                    session_id,
                    str(workspace),
                    mtime=100 + index,
                )
            oversized = (
                sessions_root
                / "2026"
                / "07"
                / "24"
                / "rollout-zzzz-019f8f19-3dab-7300-a270-f18441347b44.jsonl"
            )
            oversized.write_bytes(
                b'{"type":"session_meta","padding":"'
                + (b"x" * (codex_sessions.MAX_METADATA_BYTES + 1))
                + b'"}\n'
            )
            with mock.patch.object(
                codex_sessions,
                "MAX_SESSION_FILES",
                2,
            ):
                sessions = codex_sessions.discover_sessions(
                    str(workspace),
                    sessions_root=sessions_root,
                    limit=5,
                )
            self.assertLessEqual(len(sessions), 2)
            self.assertNotIn(
                "019f8f19-3dab-7300-a270-f18441347b44",
                {session.session_id for session in sessions},
            )


if __name__ == "__main__":
    unittest.main()
