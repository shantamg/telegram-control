import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import discovery
import provider_adapters
import router_contract
import telegram_control
from durable_store import DurableStore, StoreError, chunk_telegram_text


def make_git_repo(base: Path, name: str = "repo") -> Path:
    repo = base / name
    repo.mkdir(parents=True)
    result = subprocess.run(
        ["git", "init", str(repo)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return Path(os.path.realpath(repo))


class DiscoveryBoundRegressionTests(unittest.TestCase):
    def test_find_directory_caps_streamed_entries_before_sorting(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for index in range(30):
                (root / f"candidate-{index:02d}").mkdir()
            with mock.patch.object(
                discovery,
                "DISCOVERY_MAX_SCANNED_ENTRIES",
                7,
            ):
                result = discovery.find_directory("candidate", [root])
            self.assertTrue(result["truncated"])
            self.assertEqual(result["scanned_entries"], 7)
            self.assertLessEqual(
                len(result["candidates"]),
                discovery.DISCOVERY_MAX_RESULTS,
            )

    def test_git_probes_share_the_discovery_deadline(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "target").mkdir()
            root = Path(os.path.realpath(root))
            observed = []

            def fake_run(*args, **kwargs):
                observed.append(float(kwargs["timeout"]))
                return subprocess.CompletedProcess(args[0], 1, "", "")

            deadline = time.monotonic() + 2.0
            with mock.patch.object(discovery.subprocess, "run", fake_run):
                discovery.find_directory("target", [root], deadline=deadline)
            self.assertTrue(observed)
            self.assertTrue(all(0 < value <= 2.0 for value in observed))


class PathAuthorizationRegressionTests(unittest.TestCase):
    def test_exact_path_accepts_safe_prose_delimiters(self):
        path = "/Users/example/project"
        accepted = [
            path,
            f"Add {path}.",
            f"`{path}`",
            f'"{path}"',
            f"({path})",
            f"{path}, using Codex",
        ]
        for text in accepted:
            with self.subTest(text=text):
                self.assertTrue(
                    telegram_control.explicit_absolute_path_in_input(path, text)
                )

    def test_exact_path_rejects_prefixes_subpaths_and_dot(self):
        path = "/Users/example/project"
        rejected = [
            "/Users/example/project-two",
            "/Users/example/project/subdir",
            "x/Users/example/project",
            "ordinary punctuation.",
        ]
        for text in rejected:
            with self.subTest(text=text):
                self.assertFalse(
                    telegram_control.explicit_absolute_path_in_input(path, text)
                )
        self.assertFalse(
            telegram_control.explicit_absolute_path_in_input(
                ".",
                "Inspect this.",
            )
        )

    def test_inspection_requires_exact_authorized_path_or_trusted_ref(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            authorized = base / "authorized"
            authorized.mkdir()
            repo = make_git_repo(authorized)
            outside = make_git_repo(base, "outside")
            roots = [Path(os.path.realpath(authorized))]
            with DurableStore(base / "controller.sqlite3") as store:
                self.assertIsNone(
                    telegram_control.project_inspection_text(
                        store,
                        ".",
                        "Inspect this.",
                        roots=roots,
                    )
                )
                self.assertIsNone(
                    telegram_control.project_inspection_text(
                        store,
                        str(repo),
                        f"Inspect {repo}-other",
                        roots=roots,
                    )
                )
                self.assertIsNone(
                    telegram_control.project_inspection_text(
                        store,
                        str(outside),
                        f"Inspect {outside}",
                        roots=roots,
                    )
                )
                inspected = telegram_control.project_inspection_text(
                    store,
                    "loc_test",
                    "Inspect the discovered project",
                    discovery_refs={
                        "loc_test": {
                            "path": str(repo),
                            "source": "read_only_discovery",
                        }
                    },
                    roots=roots,
                )
                self.assertIsNotNone(inspected)
                self.assertIn(repo.name.title(), inspected)


class RouterContractRegressionTests(unittest.TestCase):
    def test_control_header_budget_is_reserved(self):
        allowed = set()
        parsed = router_contract.parse_router_tool_call(
            json.dumps(
                {
                    "tool": "respond",
                    "arguments": {"message": "x" * 3700},
                }
            ),
            allowed,
        )
        self.assertEqual(len(parsed.arguments["message"]), 3700)
        with self.assertRaises(router_contract.RouterContractError):
            router_contract.parse_router_tool_call(
                json.dumps(
                    {
                        "tool": "respond",
                        "arguments": {"message": "x" * 3701},
                    }
                ),
                allowed,
            )

    def test_discovery_metadata_associates_enrolled_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            repo = make_git_repo(base)
            workdir = repo / "app"
            workdir.mkdir()
            with DurableStore(base / "controller.sqlite3") as store:
                store.enroll_project(
                    "demo",
                    "Demo",
                    "codex",
                    str(repo),
                    working_directory=str(workdir),
                )
                result = telegram_control.annotate_enrollment_metadata(
                    store,
                    {
                        "candidates": [
                            {
                                "path": str(workdir),
                                "containing_git_root": str(repo),
                            }
                        ]
                    },
                )
            association = result["candidates"][0]["enrolled_projects"][0]
            self.assertEqual(association["project_slug"], "demo")
            self.assertEqual(
                association["path_roles"],
                ["working_directory"],
            )
            self.assertNotIn("repository_root", association)

    def test_agent_chunking_preserves_provider_payload_exactly(self):
        payload = (
            "leading spaces stay\n"
            "```python\n"
            "    indented = True\n"
            "```\n\n"
            + ("word " * 900)
            + "\ntrailing whitespace  \n"
        )
        chunks = chunk_telegram_text(payload, limit=211)
        self.assertEqual("".join(chunks), payload)
        self.assertTrue(all(0 < len(chunk) <= 211 for chunk in chunks))


class RouterLeaseRegressionTests(unittest.TestCase):
    def test_silent_provider_turn_keeps_short_lease_owned(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "controller.sqlite3"
            with DurableStore(database_path) as setup:
                setup.ensure_surface_binding(
                    chat_id=123,
                    message_thread_id=None,
                    surface_type="control",
                    display_name="Control",
                    target_type="controller",
                    target_id="control",
                )
                setup.ingest_update(
                    {
                        "update_id": 1,
                        "message": {
                            "message_id": 1,
                            "from": {"id": 123},
                            "chat": {"id": 123, "type": "private"},
                            "text": "hello",
                        },
                    }
                )
                inbox = setup.connection.execute(
                    "SELECT job_id FROM inbox_jobs WHERE update_id = 1"
                ).fetchone()
                setup.enqueue_router_message_with_receipt(
                    source_inbox_job_id=int(inbox["job_id"]),
                    input_text="hello",
                    chat_id=123,
                    message_thread_id=None,
                    authorized_user_id=123,
                    receipt_text="🎛 Control\n\nRouting…",
                )
                job = setup.claim_router_mailbox(
                    "worker-one",
                    lease_seconds=0.25,
                )

            started = threading.Event()

            class SilentAdapter:
                timeout_seconds = 30

                def run_turn(
                    self,
                    agent,
                    prompt,
                    mailbox_session_id,
                    on_session,
                    heartbeat,
                ):
                    on_session("silent-session")
                    started.set()
                    time.sleep(0.55)
                    return provider_adapters.ProviderTurnResult(
                        "silent-session",
                        json.dumps(
                            {
                                "tool": "respond",
                                "arguments": {"message": "done"},
                            }
                        ),
                        {},
                    )

            def run_worker():
                with DurableStore(database_path) as worker_store:
                    with mock.patch.object(
                        provider_adapters,
                        "adapter_for",
                        return_value=SilentAdapter(),
                    ):
                        telegram_control.process_router_mailbox_job(
                            worker_store,
                            job,
                            "worker-one",
                            lease_seconds=0.25,
                        )

            thread = threading.Thread(target=run_worker)
            thread.start()
            self.assertTrue(started.wait(2))
            time.sleep(0.35)
            with DurableStore(database_path) as contender:
                self.assertIsNone(
                    contender.claim_router_mailbox(
                        "worker-two",
                        lease_seconds=0.25,
                    )
                )
            thread.join(3)
            self.assertFalse(thread.is_alive())
            with DurableStore(database_path) as verifier:
                row = verifier.connection.execute(
                    "SELECT state FROM router_mailbox WHERE mailbox_id = ?",
                    (job.mailbox_id,),
                ).fetchone()
                self.assertEqual(row["state"], "succeeded")


if __name__ == "__main__":
    unittest.main()
