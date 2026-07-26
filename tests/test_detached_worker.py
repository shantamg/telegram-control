import tempfile
import unittest
from pathlib import Path
from unittest import mock

import detached_worker
import provider_adapters
from durable_store import DurableStore, StoreError


class DetachedWorkerStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = DurableStore(Path(self.directory.name) / "controller.sqlite3")
        self.store.__enter__()
        self.addCleanup(lambda: self.store.__exit__(None, None, None))
        self.binding_id = self._active_binding()

    def _active_binding(self, chat_id=-100777, thread_id=42):
        now = 1_700_000_000.0
        self.store.connection.execute(
            """
            INSERT INTO surface_bindings (
                chat_id, message_thread_id, surface_type, display_name,
                target_type, target_id, state, created_at, updated_at
            )
            VALUES (?, ?, 'task', 'worker updates', 'controller', 'control',
                    'active', ?, ?)
            """,
            (chat_id, thread_id, now, now),
        )
        self.store.connection.commit()
        return int(
            self.store.connection.execute(
                "SELECT binding_id FROM surface_bindings ORDER BY binding_id DESC LIMIT 1"
            ).fetchone()[0]
        )

    def _create(self, name="rails-fix", **overrides):
        values = {
            "name": name,
            "binding_id": self.binding_id,
            "project_path": "/tmp/project",
            "provider": "claude",
            "tmux_session_name": detached_worker.tmux_session_name(name),
        }
        values.update(overrides)
        return self.store.create_detached_worker(**values)

    def test_new_worker_is_intended_running_but_not_yet_observed_running(self):
        worker = self._create()
        self.assertEqual(worker.intended_state, "running")
        self.assertEqual(worker.observed_state, "starting")
        self.assertEqual(worker.restart_count, 0)

    def test_names_and_sessions_are_unique(self):
        self._create()
        with self.assertRaisesRegex(StoreError, "already uses that name"):
            self._create()

    def test_worker_is_findable_by_its_topic_thread(self):
        worker = self._create()
        found = self.store.detached_worker_for_thread(-100777, 42)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, worker.name)
        self.assertIsNone(self.store.detached_worker_for_thread(-100777, 999))

    def test_a_crashed_worker_is_distinguishable_from_a_stopped_one(self):
        # The whole reason intent and observation are separate columns.
        self._create()
        crashed = self.store.set_detached_worker_states(
            "rails-fix",
            observed_state="stopped",
        )
        self.assertTrue(crashed.needs_restart)

        stopped = self.store.set_detached_worker_states(
            "rails-fix",
            intended_state="stopped",
        )
        self.assertFalse(stopped.needs_restart)

    def test_restart_count_only_moves_when_asked(self):
        self._create()
        self.store.set_detached_worker_states("rails-fix", observed_state="running")
        self.assertEqual(
            self.store.resolve_detached_worker("rails-fix").restart_count, 0
        )
        bumped = self.store.set_detached_worker_states(
            "rails-fix",
            observed_state="running",
            bump_restart=True,
        )
        self.assertEqual(bumped.restart_count, 1)

    def test_delete_removes_the_worker(self):
        self._create()
        self.store.delete_detached_worker("rails-fix")
        self.assertIsNone(self.store.resolve_detached_worker("rails-fix"))
        with self.assertRaisesRegex(StoreError, "not found"):
            self.store.delete_detached_worker("rails-fix")

    def test_a_worker_needs_a_live_topic_binding(self):
        with self.assertRaisesRegex(StoreError, "topic binding is unavailable"):
            self._create(binding_id=9999)


class DetachedWorkerNameTests(unittest.TestCase):
    def test_rejects_names_that_are_not_slugs(self):
        for bad in ("Rails Fix", "rails_fix", "", "-rails", "a" * 49):
            with self.assertRaises(StoreError):
                detached_worker._validate_name(bad)

    def test_topic_and_session_names_are_derived_from_the_worker_name(self):
        self.assertEqual(detached_worker.topic_name("rails-fix"), "rails-fix updates")
        self.assertEqual(
            detached_worker.tmux_session_name("rails-fix"), "detached--rails-fix"
        )


class DetachedWorkerLaunchTests(unittest.TestCase):
    """The launch argv is the adapter's business, not this module's."""

    def test_claude_launch_starts_a_fresh_session_not_a_resume(self):
        adapter = provider_adapters.ClaudePrintAdapter(binary="/bin/claude")
        command = adapter.detached_launch_command(
            "/tmp/project", {"model": "sonnet", "effort": "high"}
        )
        self.assertEqual(command[0], "/bin/claude")
        self.assertNotIn("--resume", command)
        self.assertIn("--model", command)
        self.assertIn("--effort", command)

    def test_codex_launch_starts_a_fresh_session_not_a_resume(self):
        adapter = provider_adapters.CodexExecAdapter(binary="/bin/codex")
        command = adapter.detached_launch_command(
            "/tmp/project", {"model": "gpt-5.6-sol", "effort": "high"}
        )
        self.assertEqual(command[0], "/bin/codex")
        self.assertNotIn("resume", command)
        self.assertIn("--cd", command)
        self.assertIn('model_reasoning_effort="high"', command)

    def test_unknown_provider_is_reported_not_assumed(self):
        with self.assertRaises(Exception) as caught:
            detached_worker.launch_command_for("someone-else", "/tmp/project", {})
        self.assertIn("someone-else", str(caught.exception))


class ReportOnlyNoticeTests(unittest.TestCase):
    def test_notice_names_the_worker_and_points_at_the_main_agent(self):
        worker = mock.Mock(name="worker")
        worker.name = "rails-fix"
        notice = detached_worker.report_only_notice(worker, "reservations")
        self.assertIn("report-only", notice)
        self.assertIn("rails-fix", notice)
        self.assertIn("reservations", notice)

    def test_notice_still_works_without_a_known_main_topic(self):
        worker = mock.Mock()
        worker.name = "rails-fix"
        notice = detached_worker.report_only_notice(worker, None)
        self.assertIn("report-only", notice)
        self.assertNotIn("None", notice)


if __name__ == "__main__":
    unittest.main()
