import tempfile
import unittest
from pathlib import Path

from durable_store import DurableStore


class TopicCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.store = DurableStore(self.root / "controller.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temporary_directory.cleanup()

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
            SELECT slug, hierarchical_name, surface_binding_id,
                lifecycle_state, provider_session_id
            FROM agents WHERE agent_id = ?
            """,
            (subject.agent_id,),
        ).fetchone()
        self.assertTrue(str(agent["slug"]).startswith("retired_"))
        self.assertTrue(str(agent["hierarchical_name"]).startswith("retired--"))
        self.assertIsNone(agent["surface_binding_id"])
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

if __name__ == "__main__":
    unittest.main()
