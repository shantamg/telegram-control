import tempfile
import unittest
from pathlib import Path
from unittest import mock

import telegram_bridge
import telegram_control
from durable_store import DurableStore


class TopicCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.store = DurableStore(self.root / "controller.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temporary_directory.cleanup()

    def create_topic(self, *, chat_id=-100777, thread_id=62, now=100):
        return self.store.ensure_surface_binding(
            chat_id=chat_id,
            message_thread_id=thread_id,
            surface_type="task",
            display_name="Journal",
            target_type="controller",
            target_id="control",
            now=now,
        )

    def create_forum_subject(self, *, now=100):
        root = self.store.ensure_surface_binding(
            chat_id=-100777,
            surface_type="control",
            display_name="Life",
            target_type="controller",
            target_id="control",
            now=now,
        )
        self.store.bind_forum_workspace(
            chat_id=-100777,
            forum_binding_id=root.binding_id,
            project_path=str(self.root),
            provider="codex",
            now=now,
        )
        return self.store.ensure_forum_subject(
            chat_id=-100777,
            message_thread_id=62,
            display_name="Journal",
            now=now,
        )[0]

    def test_topic_probe_schedule_is_persistent_and_bounded(self):
        topic = self.create_topic(now=100)

        self.assertEqual(
            self.store.list_due_topic_probes(
                now=199,
                interval_seconds=100,
            ),
            [],
        )
        due = self.store.list_due_topic_probes(
            now=200,
            interval_seconds=100,
        )
        self.assertEqual([candidate.binding_id for candidate in due], [topic.binding_id])

        self.assertTrue(
            self.store.record_topic_probe(topic.binding_id, now=200)
        )
        self.assertEqual(
            self.store.list_due_topic_probes(
                now=299,
                interval_seconds=100,
            ),
            [],
        )
        self.assertEqual(
            [
                candidate.binding_id
                for candidate in self.store.list_due_topic_probes(
                    now=300,
                    interval_seconds=100,
                )
            ],
            [topic.binding_id],
        )

    def test_missing_topic_retires_route_subject_and_session(self):
        subject = self.create_forum_subject()
        self.store.connection.execute(
            """
            UPDATE agents
            SET provider_session_id = 'session-to-forget',
                lifecycle_state = 'running'
            WHERE agent_id = ?
            """,
            (subject.agent_id,),
        )

        self.assertTrue(
            self.store.retire_missing_topic(
                subject.surface_binding_id,
                reason="Bad Request: message thread not found",
                now=200,
            )
        )

        self.assertIsNone(self.store.resolve_surface_binding(-100777, 62))
        self.assertIsNone(self.store.resolve_forum_subject(-100777, 62))
        archived = self.store.connection.execute(
            "SELECT state FROM forum_subjects WHERE subject_id = ?",
            (subject.subject_id,),
        ).fetchone()
        self.assertEqual(archived["state"], "archived")
        agent = self.store.connection.execute(
            """
            SELECT lifecycle_state, provider_session_id
            FROM agents WHERE agent_id = ?
            """,
            (subject.agent_id,),
        ).fetchone()
        self.assertEqual(agent["lifecycle_state"], "stopped")
        self.assertIsNone(agent["provider_session_id"])
        event = self.store.connection.execute(
            """
            SELECT kind FROM events
            WHERE subject_type = 'surface'
                AND subject_id = ?
            ORDER BY event_id DESC LIMIT 1
            """,
            (str(subject.surface_binding_id),),
        ).fetchone()
        self.assertEqual(event["kind"], "telegram_topic_retired")

    def test_missing_topic_waits_for_an_active_console(self):
        subject = self.create_forum_subject()
        self.store.connection.execute(
            """
            INSERT INTO agent_consoles(
                agent_id, tmux_session_name, state, created_at, updated_at
            )
            VALUES (?, 'live-console', 'running', 100, 100)
            """,
            (subject.agent_id,),
        )

        self.assertFalse(
            self.store.retire_missing_topic(
                subject.surface_binding_id,
                reason="Bad Request: message thread not found",
                now=200,
            )
        )
        self.assertIsNotNone(self.store.resolve_surface_binding(-100777, 62))
        row = self.store.connection.execute(
            """
            SELECT last_probe_at, last_probe_error
            FROM surface_bindings WHERE binding_id = ?
            """,
            (subject.surface_binding_id,),
        ).fetchone()
        self.assertEqual(row["last_probe_at"], 200)
        self.assertIn("waiting for active work", row["last_probe_error"])

    def test_maintenance_retires_only_definitively_missing_topics(self):
        missing = self.create_topic(thread_id=62, now=100)
        inconclusive = self.create_topic(thread_id=63, now=100)
        live = self.create_topic(thread_id=64, now=100)

        def api_call(_token, method, **params):
            if method == "deleteMessage":
                return True
            thread_id = params["message_thread_id"]
            if thread_id == 62:
                raise telegram_bridge.BridgeError(
                    "Bad Request: message thread not found"
                )
            if thread_id == 63:
                raise telegram_bridge.BridgeError(
                    "Could not reach Telegram. Check this Mac's internet connection."
                )
            return {"message_id": 700 + thread_id}

        with mock.patch.object(telegram_control.bridge, "api_call", side_effect=api_call):
            counts = telegram_control.probe_due_topics_once(
                self.store,
                "token",
                interval_seconds=100,
                now=200,
            )

        self.assertEqual(
            counts,
            {"probed": 3, "alive": 1, "retired": 1, "deferred": 1},
        )
        self.assertIsNone(
            self.store.resolve_surface_binding(
                missing.chat_id,
                missing.message_thread_id,
            )
        )
        self.assertIsNotNone(
            self.store.resolve_surface_binding(
                inconclusive.chat_id,
                inconclusive.message_thread_id,
            )
        )
        self.assertIsNotNone(
            self.store.resolve_surface_binding(
                live.chat_id,
                live.message_thread_id,
            )
        )

    def test_probe_delete_failure_does_not_retire_a_live_topic(self):
        topic = self.create_topic(now=100)

        def api_call(_token, method, **_params):
            if method == "sendMessage":
                return {"message_id": 700}
            raise telegram_bridge.BridgeError("Could not delete probe")

        with mock.patch.object(telegram_control.bridge, "api_call", side_effect=api_call):
            counts = telegram_control.probe_due_topics_once(
                self.store,
                "token",
                interval_seconds=100,
                now=200,
            )

        self.assertEqual(counts["alive"], 1)
        self.assertIsNotNone(
            self.store.resolve_surface_binding(
                topic.chat_id,
                topic.message_thread_id,
            )
        )


if __name__ == "__main__":
    unittest.main()
