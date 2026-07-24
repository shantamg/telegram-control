import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import on_message
import telegram_bridge
from durable_store import CallbackActionError, DurableStore, StoreError


def callback_update(update_id: int, token: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": 123, "username": "tester"},
            "data": f"a:{token}",
            "message": {
                "message_id": 800,
                "chat": {"id": 123, "type": "private"},
            },
        },
    }


class TelegramMutationSagaTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "project"
        self.root.mkdir()
        initialized = subprocess.run(
            ["git", "init", str(self.root)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(initialized.returncode, 0)
        self.root = Path(os.path.realpath(self.root))
        self.database_path = (
            Path(self.temporary_directory.name) / "controller.sqlite3"
        )
        on_message.OUTPUT_SEQUENCE = 0

    def tearDown(self):
        self.temporary_directory.cleanup()

    def project_action(self):
        with DurableStore(self.database_path) as store:
            action = store.create_callback_action(
                operation_id="router:99:project:0",
                action_type="router_project_confirm",
                payload={
                    "router_mailbox_id": 99,
                    "label": "Create project agent",
                    "slug": "sample-project",
                    "display_name": "Sample Project",
                    "provider": "codex",
                    "project_path": str(self.root),
                    "working_directory": str(self.root),
                    "topic_name": "Sample Project",
                    "provider_config": {
                        "model": "gpt-5.6-sol",
                        "effort": "high",
                    },
                    "provenance": [
                        {
                            "value": str(self.root),
                            "source": "read_only_discovery",
                            "derived_from": "sample project",
                        }
                    ],
                },
                chat_id=123,
                authorized_user_id=123,
            )
            update = callback_update(11, action.token)
            store.ingest_update(update, now=100)
            job_id = int(
                store.connection.execute(
                    "SELECT job_id FROM inbox_jobs WHERE update_id = 11"
                ).fetchone()["job_id"]
            )
        return action, update, job_id

    def rename_action(self):
        with DurableStore(self.database_path) as store:
            binding = store.ensure_surface_binding(
                chat_id=123,
                message_thread_id=62,
                surface_type="project",
                display_name="Old Name",
                target_type="controller",
                target_id="control",
            )
            action = store.create_callback_action(
                operation_id="router:100:topic-rename:0",
                action_type="router_topic_rename_confirm",
                payload={
                    "router_mailbox_id": 100,
                    "label": "Rename topic",
                    "binding_id": binding.binding_id,
                    "chat_id": 123,
                    "message_thread_id": 62,
                    "old_name": "Old Name",
                    "new_name": "New Name",
                },
                chat_id=123,
                authorized_user_id=123,
            )
            update = callback_update(12, action.token)
            store.ingest_update(update, now=100)
            job_id = int(
                store.connection.execute(
                    "SELECT job_id FROM inbox_jobs WHERE update_id = 12"
                ).fetchone()["job_id"]
            )
        return action, update, job_id

    def environment(self, job_id: int) -> dict[str, str]:
        return {
            "TELEGRAM_CONTROL_DB": str(self.database_path),
            "TELEGRAM_CONTROL_JOB_ID": str(job_id),
            "TELEGRAM_CHAT_ID": "123",
            "TELEGRAM_FROM_ID": "123",
            "TELEGRAM_MESSAGE_ID": "800",
            "TELEGRAM_MESSAGE_THREAD_ID": "",
        }

    def test_project_restarts_after_durable_result_without_second_api_call(self):
        action, update, job_id = self.project_action()
        with mock.patch.dict(
            os.environ,
            self.environment(job_id),
            clear=False,
        ):
            with mock.patch.object(
                on_message.bridge, "read_token", return_value="test-token"
            ), mock.patch.object(
                on_message.bridge,
                "api_call",
                return_value={"message_thread_id": 77},
            ) as first_api, mock.patch.object(
                DurableStore,
                "ensure_surface_binding",
                side_effect=StoreError("simulated local crash"),
            ):
                with self.assertRaisesRegex(StoreError, "simulated local crash"):
                    on_message.handle_callback(
                        update,
                        update["callback_query"],
                    )
            first_api.assert_called_once()

            with DurableStore(self.database_path) as store:
                mutation = store.resolve_telegram_mutation(action.operation_id)
                self.assertEqual(mutation.state, "external_succeeded")
                self.assertEqual(
                    mutation.external_result,
                    {"message_thread_id": 77},
                )

            with mock.patch.object(
                on_message.bridge,
                "api_call",
                side_effect=AssertionError("Telegram call repeated"),
            ) as replay_api:
                on_message.handle_callback(
                    update,
                    update["callback_query"],
                )
            replay_api.assert_not_called()

        with DurableStore(self.database_path) as store:
            self.assertEqual(
                store.resolve_telegram_mutation(action.operation_id).state,
                "applied",
            )
            self.assertIsNotNone(store.resolve_project_agent("sample-project"))
            self.assertEqual(
                [
                    str(row["kind"])
                    for row in store.connection.execute(
                        """
                        SELECT kind FROM events
                        WHERE subject_type = 'telegram_mutation'
                            AND subject_id = ?
                        ORDER BY event_id
                        """,
                        (action.operation_id,),
                    ).fetchall()
                ],
                [
                    "telegram_mutation_prepared",
                    "telegram_mutation_external_in_flight",
                    "telegram_mutation_external_succeeded",
                    "telegram_mutation_applied",
                ],
            )

    def test_project_lost_api_result_is_not_repeated_on_restart(self):
        action, update, job_id = self.project_action()
        with mock.patch.dict(
            os.environ,
            self.environment(job_id),
            clear=False,
        ):
            with mock.patch.object(
                on_message.bridge, "read_token", return_value="test-token"
            ), mock.patch.object(
                on_message.bridge,
                "api_call",
                return_value={"message_thread_id": 77},
            ) as first_api, mock.patch.object(
                DurableStore,
                "record_telegram_mutation_result",
                side_effect=StoreError("simulated crash before result commit"),
            ):
                with self.assertRaisesRegex(
                    StoreError, "simulated crash before result commit"
                ):
                    on_message.handle_callback(
                        update,
                        update["callback_query"],
                    )
            first_api.assert_called_once()

            with DurableStore(self.database_path) as store:
                self.assertEqual(
                    store.resolve_telegram_mutation(action.operation_id).state,
                    "external_in_flight",
                )

            with mock.patch.object(
                on_message.bridge,
                "api_call",
                side_effect=AssertionError("Telegram call repeated"),
            ) as replay_api:
                on_message.handle_callback(
                    update,
                    update["callback_query"],
                )
            replay_api.assert_not_called()

        with DurableStore(self.database_path) as store:
            mutation = store.resolve_telegram_mutation(action.operation_id)
            self.assertEqual(mutation.state, "reconciliation_required")
            self.assertIsNone(store.resolve_project_agent("sample-project"))

    def test_rename_restarts_local_apply_without_repeating_telegram(self):
        action, update, job_id = self.rename_action()
        with mock.patch.dict(
            os.environ,
            self.environment(job_id),
            clear=False,
        ):
            with mock.patch.object(
                on_message.bridge, "read_token", return_value="test-token"
            ), mock.patch.object(
                on_message.bridge,
                "api_call",
                return_value=True,
            ) as first_api, mock.patch.object(
                DurableStore,
                "complete_telegram_mutation",
                side_effect=StoreError("simulated crash after local rename"),
            ):
                with self.assertRaisesRegex(
                    StoreError, "simulated crash after local rename"
                ):
                    on_message.handle_callback(
                        update,
                        update["callback_query"],
                    )
            first_api.assert_called_once()

            with mock.patch.object(
                on_message.bridge,
                "api_call",
                side_effect=AssertionError("Telegram call repeated"),
            ) as replay_api:
                on_message.handle_callback(
                    update,
                    update["callback_query"],
                )
            replay_api.assert_not_called()

        with DurableStore(self.database_path) as store:
            self.assertEqual(
                store.resolve_telegram_mutation(action.operation_id).state,
                "applied",
            )
            self.assertEqual(
                store.resolve_surface_binding(123, 62).display_name,
                "New Name",
            )

    def test_ambiguous_telegram_failure_is_durable_and_not_retried(self):
        action, update, job_id = self.rename_action()
        with mock.patch.dict(
            os.environ,
            self.environment(job_id),
            clear=False,
        ):
            with mock.patch.object(
                on_message.bridge, "read_token", return_value="test-token"
            ), mock.patch.object(
                on_message.bridge,
                "api_call",
                side_effect=telegram_bridge.BridgeError("connection lost"),
            ) as first_api:
                on_message.handle_callback(
                    update,
                    update["callback_query"],
                )
            first_api.assert_called_once()

            with mock.patch.object(
                on_message.bridge,
                "api_call",
                side_effect=AssertionError("Telegram call repeated"),
            ) as replay_api:
                on_message.handle_callback(
                    update,
                    update["callback_query"],
                )
            replay_api.assert_not_called()

        with DurableStore(self.database_path) as store:
            mutation = store.resolve_telegram_mutation(action.operation_id)
            self.assertEqual(mutation.state, "reconciliation_required")
            self.assertEqual(
                store.resolve_surface_binding(123, 62).display_name,
                "Old Name",
            )

    def test_consumed_confirmation_can_resume_nonterminal_operation(self):
        action, update, _ = self.project_action()
        with DurableStore(self.database_path) as store:
            consumed = store.consume_callback_action(
                f"a:{action.token}",
                chat_id=123,
                authorized_user_id=123,
                update_id=update["update_id"],
            )
            store.prepare_telegram_mutation(
                consumed.operation_id,
                "project_create",
                consumed.payload,
            )
            replay = store.consume_callback_action(
                f"a:{action.token}",
                chat_id=123,
                authorized_user_id=123,
                update_id=999,
                now=10**12,
            )
            self.assertEqual(replay.operation_id, action.operation_id)
            _, acquired = store.begin_telegram_mutation_external(
                action.operation_id
            )
            self.assertTrue(acquired)
            with self.assertRaises(CallbackActionError):
                store.consume_callback_action(
                    f"a:{action.token}",
                    chat_id=123,
                    authorized_user_id=123,
                    update_id=1000,
                    now=10**12,
                )

    def assert_single_external_claim(
        self,
        action,
        update,
        mutation_type: str,
    ) -> None:
        with DurableStore(self.database_path) as store:
            consumed = store.consume_callback_action(
                f"a:{action.token}",
                chat_id=123,
                authorized_user_id=123,
                update_id=update["update_id"],
            )
            store.prepare_telegram_mutation(
                consumed.operation_id,
                mutation_type,
                consumed.payload,
            )

        barrier = threading.Barrier(3)
        results = []
        failures = []
        result_lock = threading.Lock()

        def contender() -> None:
            try:
                with DurableStore(self.database_path) as store:
                    barrier.wait()
                    mutation, acquired = (
                        store.begin_telegram_mutation_external(
                            action.operation_id
                        )
                    )
                with result_lock:
                    results.append((mutation.state, acquired))
            except BaseException as exc:
                with result_lock:
                    failures.append(exc)

        threads = [threading.Thread(target=contender) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(
            sorted(acquired for _, acquired in results),
            [False, True],
        )
        self.assertEqual(
            {state for state, _ in results},
            {"external_in_flight"},
        )
        with DurableStore(self.database_path) as store:
            mutation = store.resolve_telegram_mutation(action.operation_id)
            self.assertEqual(mutation.attempts, 1)

    def test_concurrent_project_confirmations_have_one_external_owner(self):
        action, update, _ = self.project_action()
        self.assert_single_external_claim(
            action,
            update,
            "project_create",
        )

    def test_concurrent_rename_confirmations_have_one_external_owner(self):
        action, update, _ = self.rename_action()
        self.assert_single_external_claim(
            action,
            update,
            "topic_rename",
        )


if __name__ == "__main__":
    unittest.main()
