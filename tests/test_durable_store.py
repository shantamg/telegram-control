import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

import on_message
import telegram_control
from durable_store import (
    DurableStore,
    IncompatibleSchemaError,
    LeaseLostError,
    StoreError,
)


def message_update(update_id=10, text="hello"):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 99,
            "from": {"id": 123, "username": "tester"},
            "chat": {"id": 123, "type": "private"},
            "text": text,
        },
    }


def callback_update(update_id=11):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "callback-1",
            "from": {"id": 123, "username": "tester"},
            "data": "r:opaque",
            "message": {
                "message_id": 100,
                "chat": {"id": 123, "type": "private"},
            },
        },
    }


class DurableStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "controller.sqlite3"
        self.store = DurableStore(self.database_path)

    def tearDown(self):
        self.store.close()
        self.temporary_directory.cleanup()

    def test_configures_and_migrates_database(self):
        self.assertEqual(self.store.quick_check(), "ok")
        self.assertEqual(
            self.store.connection.execute("PRAGMA user_version").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.store.connection.execute("PRAGMA foreign_keys").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.store.connection.execute("PRAGMA journal_mode").fetchone()[0],
            "wal",
        )

    def test_duplicate_update_has_one_job_and_never_moves_offset_backwards(self):
        self.assertTrue(self.store.ingest_update(message_update(10), now=100))
        self.assertFalse(
            self.store.ingest_update(message_update(10, text="changed"), now=101)
        )
        self.assertTrue(self.store.ingest_update(message_update(8), now=102))

        self.assertEqual(self.store.poll_offset(), 11)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM telegram_updates"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM inbox_jobs"
            ).fetchone()[0],
            2,
        )
        stored = self.store.connection.execute(
            "SELECT raw_json FROM telegram_updates WHERE update_id = 10"
        ).fetchone()[0]
        self.assertIn('"text":"hello"', stored)

    def test_stage_zero_offset_seeding_only_moves_forward(self):
        self.store.initialize_poll_offset(30)
        self.store.initialize_poll_offset(20)
        self.assertEqual(self.store.poll_offset(), 30)
        self.store.initialize_poll_offset(40)
        self.assertEqual(self.store.poll_offset(), 40)

    def test_callback_query_is_stored_as_a_job(self):
        self.assertTrue(self.store.ingest_update(callback_update(), now=100))

        job = self.store.claim_job("worker-a", now=100)

        self.assertIsNotNone(job)
        self.assertEqual(job.kind, "callback_query")
        self.assertEqual(job.payload["callback_query"]["data"], "r:opaque")

    def test_unsupported_update_is_recorded_without_a_job(self):
        self.assertTrue(
            self.store.ingest_update(
                {"update_id": 12, "edited_message": {"text": "ignored"}},
                now=100,
            )
        )

        self.assertEqual(self.store.poll_offset(), 13)
        self.assertIsNone(self.store.claim_job("worker-a", now=100))
        self.assertEqual(
            self.store.status_counts()["updates"],
            {"ignored": 1},
        )

    def test_update_and_offset_rollback_together(self):
        self.store.ingest_update(message_update(10), now=100)
        self.store.connection.execute(
            """
            CREATE TRIGGER reject_offset
            BEFORE UPDATE ON controller_state
            BEGIN
                SELECT RAISE(ABORT, 'injected crash boundary');
            END
            """
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.ingest_update(message_update(11), now=101)

        self.assertEqual(self.store.poll_offset(), 11)
        self.assertIsNone(
            self.store.connection.execute(
                "SELECT update_id FROM telegram_updates WHERE update_id = 11"
            ).fetchone()
        )
        self.assertIsNone(
            self.store.connection.execute(
                "SELECT update_id FROM inbox_jobs WHERE update_id = 11"
            ).fetchone()
        )

    def test_expired_inbox_lease_is_recovered_and_old_owner_is_rejected(self):
        self.store.ingest_update(message_update(), now=100)
        first = self.store.claim_job("worker-a", now=100, lease_seconds=10)
        self.assertIsNotNone(first)
        self.assertIsNone(
            self.store.claim_job("worker-b", now=109, lease_seconds=10)
        )

        recovered = self.store.claim_job("worker-b", now=111, lease_seconds=10)

        self.assertEqual(recovered.job_id, first.job_id)
        self.assertEqual(recovered.attempts, 2)
        with self.assertRaises(LeaseLostError):
            self.store.complete_job(first.job_id, "worker-a", now=112)
        self.store.complete_job(recovered.job_id, "worker-b", now=112)
        self.assertEqual(
            self.store.status_counts()["inbox"],
            {"succeeded": 1},
        )

    def test_failed_job_backs_off_then_dead_letters_and_can_be_retried(self):
        self.store.ingest_update(message_update(), now=100)
        first = self.store.claim_job("worker", now=100)
        state = self.store.fail_job(
            first.job_id,
            "worker",
            "first failure",
            now=100,
            max_attempts=2,
            base_delay=5,
        )
        self.assertEqual(state, "queued")
        self.assertIsNone(self.store.claim_job("worker", now=104))

        second = self.store.claim_job("worker", now=105)
        state = self.store.fail_job(
            second.job_id,
            "worker",
            "second failure",
            now=105,
            max_attempts=2,
        )
        self.assertEqual(state, "dead")
        self.assertEqual(self.store.retry_dead("inbox", now=106), 1)
        retried = self.store.claim_job("worker", now=106)
        self.assertEqual(retried.attempts, 1)

    def test_outbox_operation_is_idempotent_and_recovers_expired_lease(self):
        first_id = self.store.enqueue_api_call(
            "job:1:reply:1",
            "sendMessage",
            {"chat_id": 123, "text": "first"},
            now=100,
        )
        duplicate_id = self.store.enqueue_api_call(
            "job:1:reply:1",
            "sendMessage",
            {"chat_id": 123, "text": "first"},
            now=101,
        )
        self.assertEqual(duplicate_id, first_id)
        with self.assertRaises(StoreError):
            self.store.enqueue_api_call(
                "job:1:reply:1",
                "sendMessage",
                {"chat_id": 123, "text": "changed"},
                now=102,
            )

        first = self.store.claim_outbox("sender-a", now=100, lease_seconds=10)
        self.assertEqual(first.params["text"], "first")
        recovered = self.store.claim_outbox("sender-b", now=111, lease_seconds=10)
        self.assertEqual(recovered.message_id, first.message_id)
        self.assertEqual(recovered.attempts, 2)
        with self.assertRaises(LeaseLostError):
            self.store.complete_outbox(first.message_id, "sender-a", {}, now=112)
        self.store.complete_outbox(
            recovered.message_id,
            "sender-b",
            {"message_id": 501},
            now=112,
        )
        self.assertEqual(self.store.status_counts()["outbox"], {"sent": 1})


class SchemaCompatibilityTests(unittest.TestCase):
    def test_cli_fails_cleanly_for_non_database_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "corrupt.sqlite3"
            path.write_bytes(b"this is not a SQLite database")
            stderr = StringIO()

            with mock.patch.object(
                telegram_control.sys,
                "argv",
                ["telegram_control.py", "--db", str(path), "status"],
            ):
                with redirect_stderr(stderr):
                    return_code = telegram_control.main()

            self.assertEqual(return_code, 1)
            self.assertIn("Error:", stderr.getvalue())

    def test_concurrent_first_open_migrates_once(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "new.sqlite3"

            def open_and_check(_):
                with DurableStore(path) as store:
                    return store.quick_check()

            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(open_and_check, range(16)))

            self.assertEqual(results, ["ok"] * 16)
            with DurableStore(path) as store:
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'inbox_jobs'"
                    ).fetchone()[0],
                    1,
                )

    def test_newer_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "future.sqlite3"
            connection = sqlite3.connect(str(path))
            connection.execute("PRAGMA user_version = 99")
            connection.close()

            with self.assertRaises(IncompatibleSchemaError):
                DurableStore(path)


class DurableIntegrationTests(unittest.TestCase):
    def test_durable_launch_agent_uses_supervisor_and_existing_label(self):
        plist = telegram_control.launch_agent_plist()

        self.assertEqual(
            plist["Label"],
            telegram_control.bridge.LAUNCH_AGENT_LABEL,
        )
        self.assertEqual(
            plist["ProgramArguments"],
            [
                telegram_control.sys.executable,
                str(telegram_control.SCRIPT_PATH),
                "--db",
                str(telegram_control.DATABASE_PATH),
                "run",
            ],
        )
        self.assertTrue(plist["RunAtLoad"])
        self.assertTrue(plist["KeepAlive"])

    def test_inbox_worker_runs_existing_handler_and_creates_durable_reply(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "controller.sqlite3"
            config = {
                "chat_id": 123,
                "handler_path": str(Path(on_message.__file__).resolve()),
            }
            with DurableStore(database_path) as store:
                store.ingest_update(message_update(), now=100)
                job = store.claim_job("worker", now=100, lease_seconds=60)

                telegram_control.process_inbox_job(store, config, job, "worker")

                self.assertEqual(store.status_counts()["inbox"], {"succeeded": 1})
                reply = store.claim_outbox("sender", now=10**12)
                self.assertEqual(reply.method, "sendMessage")
                self.assertEqual(reply.params["chat_id"], 123)
                self.assertEqual(
                    reply.params["text"],
                    "✅ Mac script ran and received: hello",
                )

    def test_handler_queues_reply_instead_of_calling_telegram(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "controller.sqlite3"
            with DurableStore(database_path):
                pass
            environment = {
                "TELEGRAM_CONTROL_DB": str(database_path),
                "TELEGRAM_CONTROL_JOB_ID": "44",
                "TELEGRAM_CHAT_ID": "123",
                "TELEGRAM_MESSAGE_THREAD_ID": "7",
            }
            on_message.OUTPUT_SEQUENCE = 0
            with mock.patch.dict(os.environ, environment, clear=False):
                with mock.patch.object(
                    on_message.bridge,
                    "load_config",
                    return_value={"chat_id": 123},
                ):
                    with mock.patch.object(
                        on_message.bridge,
                        "api_call",
                        side_effect=AssertionError("network call was not expected"),
                    ):
                        on_message.send_message("durable reply")

            with DurableStore(database_path) as store:
                queued = store.claim_outbox("sender", now=10**12)
                self.assertEqual(queued.operation_id, "inbox:44:message:1")
                self.assertEqual(
                    queued.params,
                    {
                        "chat_id": 123,
                        "message_thread_id": 7,
                        "text": "durable reply",
                    },
                )

    def test_collector_commits_each_update_before_using_next_offset(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "controller.sqlite3"
            with DurableStore(path) as store:
                with mock.patch.object(
                    telegram_control.bridge,
                    "api_call",
                    return_value=[message_update(20), callback_update(21)],
                ) as api_call:
                    with mock.patch.object(
                        telegram_control.bridge,
                        "save_offset",
                    ) as save_offset:
                        count = telegram_control.collect_once(store, "secret", timeout=0)

                self.assertEqual(count, 2)
                self.assertEqual(store.poll_offset(), 22)
                self.assertEqual(store.status_counts()["inbox"], {"queued": 2})
                self.assertEqual(
                    [call.args[0] for call in save_offset.call_args_list],
                    [21, 22],
                )
                api_call.assert_called_once_with(
                    "secret",
                    "getUpdates",
                    offset=None,
                    timeout=0,
                    allowed_updates=["message", "callback_query"],
                )


if __name__ == "__main__":
    unittest.main()
