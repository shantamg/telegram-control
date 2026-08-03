import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import detached_worker
import on_message
import provider_adapters
import telegram_control
import voice_settings
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

    def test_worker_is_findable_from_a_previous_report_topic_alias(self):
        worker = self._create()
        alias_binding_id = self._active_binding(
            chat_id=-100888,
            thread_id=77,
        )
        self.store.connection.execute(
            """
            UPDATE surface_bindings
            SET target_type = 'detached_worker', target_id = ?
            WHERE binding_id = ?
            """,
            (worker.name, alias_binding_id),
        )
        self.store.connection.commit()
        found = self.store.detached_worker_for_thread(-100888, 77)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, worker.name)

    def test_voice_report_uses_the_confirmed_global_voice_configuration(self):
        self._create()
        self.store.set_voice_configuration(
            voice_settings.VoiceConfiguration(
                voice_name="en-US-AndrewNeural",
                rate="-10%",
            )
        )
        voice_path = Path(self.directory.name) / "report.ogg"
        with mock.patch.object(
            detached_worker.voice_responses,
            "synthesize_voice",
            return_value=voice_path,
        ) as synthesize, mock.patch.object(
            detached_worker.telegram_bridge,
            "read_token",
            return_value="token",
        ), mock.patch.object(
            detached_worker.telegram_bridge,
            "api_call",
        ) as api_call:
            detached_worker.report(
                self.store,
                "rails-fix",
                key="milestone",
                text="The migration is complete.",
            )

        synthesize.assert_called_once_with(
            "The migration is complete.",
            "detached-rails-fix-milestone",
            voice_name="en-US-AndrewNeural",
            rate="-10%",
        )
        self.assertEqual(api_call.call_args.args[:2], ("token", "sendVoice"))

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

    def test_recovery_metadata_and_handshake_are_durable(self):
        worker = self._create(
            provider_session_id="session-123",
            provider_config={"model": "opus", "effort": "high"},
            working_directory="/tmp/project/worktree",
            recovery_file_path="/tmp/recovery.md",
            recovery_prompt="Read the recovery file.",
        )
        self.assertEqual(worker.provider_session_id, "session-123")
        self.assertEqual(worker.provider_config["model"], "opus")
        self.assertEqual(worker.working_directory, "/tmp/project/worktree")
        self.assertEqual(worker.recovery_file_path, "/tmp/recovery.md")

        recovering = self.store.begin_detached_worker_recovery(
            worker.name,
            now=1_700_000_100,
        )
        self.assertEqual(recovering.recovery_generation, 1)
        self.assertEqual(recovering.recovery_state, "recovering")
        self.assertEqual(recovering.observed_state, "starting")
        self.assertEqual(recovering.restart_count, 1)

        recovered = self.store.complete_detached_worker_recovery(
            worker.name,
            1,
            now=1_700_000_200,
        )
        self.assertEqual(recovered.recovery_state, "succeeded")
        self.assertEqual(recovered.observed_state, "running")
        self.assertEqual(recovered.last_recovered_at, 1_700_000_200)
        with self.assertRaisesRegex(StoreError, "stale"):
            self.store.complete_detached_worker_recovery(worker.name, 1)

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

    def test_claude_launch_uses_the_preassigned_recovery_session(self):
        adapter = provider_adapters.ClaudePrintAdapter(binary="/bin/claude")
        command = adapter.detached_launch_command(
            "/tmp/project",
            {},
            "a8813c38-8a89-49de-ab5e-003e48a1814c",
        )
        self.assertEqual(
            command[-2:],
            ["--session-id", "a8813c38-8a89-49de-ab5e-003e48a1814c"],
        )

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

    def test_brief_waits_for_bracketed_paste_before_submitting(self):
        with (
            mock.patch.object(
                detached_worker.tmux_console,
                "has_tmux_session",
                return_value=True,
            ),
            mock.patch.object(
                detached_worker.tmux_console,
                "tmux_binary",
                return_value="/bin/tmux",
            ),
            mock.patch.object(detached_worker.subprocess, "run") as run,
            mock.patch.object(detached_worker.time, "sleep") as sleep,
        ):
            with mock.patch.object(
                detached_worker,
                "pane_text",
                return_value="⏺ Working on it.\n❯ ",
            ):
                detached_worker.send_brief("rails-fix", "Do the work now.")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            sleep.call_args_list[0].args,
            (detached_worker.BRIEF_SUBMIT_DELAY_SECONDS,),
        )
        self.assertEqual(run.call_args_list[0].args[0][-1], "Do the work now.")
        self.assertEqual(run.call_args_list[1].args[0][-1], "Enter")

    def test_brief_left_in_the_composer_gets_another_bare_enter(self):
        # First check still shows the brief typed but unsent; the second is
        # clear, so exactly one extra Enter should have been needed.
        panes = ["❯ Do the work now.", "⏺ Working on it.\n❯ "]
        with (
            mock.patch.object(
                detached_worker.tmux_console,
                "has_tmux_session",
                return_value=True,
            ),
            mock.patch.object(
                detached_worker.tmux_console,
                "tmux_binary",
                return_value="/bin/tmux",
            ),
            mock.patch.object(detached_worker.subprocess, "run") as run,
            mock.patch.object(detached_worker.time, "sleep"),
            mock.patch.object(
                detached_worker,
                "pane_text",
                side_effect=panes,
            ),
        ):
            detached_worker.send_brief("rails-fix", "Do the work now.")
        enters = [
            call for call in run.call_args_list if call.args[0][-1] == "Enter"
        ]
        self.assertEqual(len(enters), 2)

    def test_delivered_brief_echoed_in_the_transcript_is_not_read_as_stuck(self):
        # Claude echoes a submitted message back behind "> ", so scanning every
        # prompt-marked line mistook a successful delivery for a failed one.
        # Only the last such line is the composer.
        brief = "Write the single word delivered into the proof file."
        pane = (
            "> Write the single word delivered into the proof file.\n"
            "⏺ Done.\n"
            "❯ "
        )
        self.assertFalse(detached_worker._still_in_composer(pane, brief))
        self.assertTrue(
            detached_worker._still_in_composer(f"⏺ idle\n❯ {brief}", brief)
        )

    def test_brief_that_never_submits_is_reported_not_claimed(self):
        with (
            mock.patch.object(
                detached_worker.tmux_console,
                "has_tmux_session",
                return_value=True,
            ),
            mock.patch.object(
                detached_worker.tmux_console,
                "tmux_binary",
                return_value="/bin/tmux",
            ),
            mock.patch.object(detached_worker.subprocess, "run"),
            mock.patch.object(detached_worker.time, "sleep"),
            mock.patch.object(
                detached_worker,
                "pane_text",
                return_value="❯ Do the work now.",
            ),
            self.assertRaises(Exception) as caught,
        ):
            detached_worker.send_brief("rails-fix", "Do the work now.")
        self.assertIn("Nothing was delivered", str(caught.exception))


class ReportOnlyNoticeTests(unittest.TestCase):
    def test_notice_names_the_worker_and_points_at_the_main_agent(self):
        worker = mock.Mock(name="worker")
        worker.name = "rails-fix"
        notice = detached_worker.report_only_notice(worker, "reservations")
        self.assertIn("report-only", notice)
        self.assertIn("rails-fix", notice)
        self.assertIn("reservations", notice)

    def test_notice_includes_a_direct_main_topic_link(self):
        worker = mock.Mock(name="worker")
        worker.name = "rails-fix"
        notice = detached_worker.report_only_notice(
            worker,
            "reservations",
            "https://t.me/c/4363256963/20",
        )
        self.assertIn("https://t.me/c/4363256963/20", notice)

    def test_notice_still_works_without_a_known_main_topic(self):
        worker = mock.Mock()
        worker.name = "rails-fix"
        notice = detached_worker.report_only_notice(worker, None)
        self.assertIn("report-only", notice)
        self.assertNotIn("None", notice)


class DetachedWorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database_path = Path(self.directory.name) / "controller.sqlite3"
        self.store = DurableStore(self.database_path)
        self.store.__enter__()
        self.addCleanup(lambda: self.store.__exit__(None, None, None))
        self.binding = self.store.ensure_surface_binding(
            chat_id=-1004472153577,
            message_thread_id=220,
            surface_type="task",
            display_name="recover-me updates",
            target_type="detached_worker",
            target_id="recover-me",
        )
        self.recovery_file = detached_worker.ensure_recovery_file(
            self.store,
            "recover-me",
        )
        self.worker = self.store.create_detached_worker(
            name="recover-me",
            binding_id=self.binding.binding_id,
            project_path=self.directory.name,
            provider="claude",
            provider_session_id="a8813c38-8a89-49de-ab5e-003e48a1814c",
            provider_config={"model": "opus"},
            tmux_session_name="detached--recover-me",
            working_directory=self.directory.name,
            recovery_file_path=str(self.recovery_file),
            recovery_prompt=detached_worker.DEFAULT_RECOVERY_PROMPT,
        )
        self.store.set_detached_worker_states(
            self.worker.name,
            observed_state="stopped",
        )

    def test_recovery_file_is_created_outside_tmp(self):
        self.assertTrue(self.recovery_file.is_file())
        self.assertEqual(
            self.recovery_file,
            Path(self.directory.name)
            / "detached-workers"
            / "recover-me"
            / "RECOVERY.md",
        )

    def test_launch_preamble_asks_for_no_bookkeeping(self):
        preamble = detached_worker.launch_preamble(self.worker.name)
        self.assertIn(self.worker.name, preamble)
        self.assertIn("native scheduling", preamble)
        # The worker is never handed a file to maintain: resuming its session
        # restores its scheduled work, so an inventory would duplicate state
        # the harness already keeps.
        self.assertNotIn("RECOVERY.md", preamble)
        self.assertNotIn(str(self.recovery_file), preamble)

    def test_worker_teardown_removes_only_its_managed_recovery_file(self):
        companion = self.recovery_file.parent / "operator-note.txt"
        companion.write_text("preserve me", encoding="utf-8")
        removed = detached_worker.remove_recovery_file(self.store, self.worker)
        self.assertTrue(removed)
        self.assertFalse(self.recovery_file.exists())
        self.assertTrue(companion.exists())

    def test_stopped_worker_resumes_exact_session_without_a_prompt(self):
        with (
            mock.patch.object(
                detached_worker.tmux_console,
                "has_tmux_session",
                # Stopped when reconciled, up again once the resume has run.
                side_effect=[False, True],
            ),
            mock.patch.object(
                detached_worker,
                "validate_provider_session",
                return_value=True,
            ),
            mock.patch.object(
                detached_worker,
                "resume_command_for",
                return_value=["claude", "--resume", "session", "prompt"],
            ) as resume_command,
            mock.patch.object(detached_worker, "_start_session") as start_session,
        ):
            result, worker = detached_worker.recover_worker(
                self.store,
                self.worker.name,
                now=1_700_000_100,
            )

        # Recovery completes in one pass: resuming the session ID is what
        # restores the worker, so there is nothing to wait for an agent to say.
        self.assertEqual(result, "recovered")
        self.assertEqual(worker.recovery_state, "succeeded")
        self.assertEqual(worker.recovery_generation, 1)
        # Succeeding clears the attempt counter, so the backoff ladder starts
        # fresh for the next unrelated crash rather than inheriting this one.
        self.assertEqual(worker.restart_count, 0)
        self.assertEqual(worker.observed_state, "running")
        # One argument: the worker, and no prompt after it.
        self.assertEqual(len(resume_command.call_args.args), 1)
        self.assertEqual(resume_command.call_args.args[0].name, worker.name)
        self.assertEqual(resume_command.call_args.kwargs, {})
        start_session.assert_called_once_with(
            "detached--recover-me",
            self.directory.name,
            ["claude", "--resume", "session", "prompt"],
        )
        report = self.store.connection.execute(
            """
            SELECT method, params_json
            FROM outbox_messages
            WHERE operation_id =
                'detached-worker:1:recovery:1:succeeded'
            """
        ).fetchone()
        self.assertEqual(report["method"], "sendMessage")
        self.assertIn("running again", report["params_json"])

    def test_agent_confirmation_marks_success_and_queues_verified_report(self):
        self.store.begin_detached_worker_recovery(
            self.worker.name,
            now=1_700_000_100,
        )
        with mock.patch.object(
            detached_worker.tmux_console,
            "has_tmux_session",
            return_value=True,
        ):
            recovered = detached_worker.confirm_recovery(
                self.store,
                self.worker.name,
                1,
                "Restored the native Monday wakeup and verified the monitor.",
            )
        self.assertEqual(recovered.recovery_state, "succeeded")
        report = self.store.connection.execute(
            """
            SELECT params_json
            FROM outbox_messages
            WHERE operation_id =
                'detached-worker:1:recovery:1:succeeded'
            """
        ).fetchone()
        self.assertIn("recovered successfully", report["params_json"])
        self.assertIn("native Monday wakeup", report["params_json"])

    def test_missing_session_is_reported_without_starting_a_replacement(self):
        self.store.connection.execute(
            """
            UPDATE detached_workers
            SET provider_session_id = NULL
            WHERE name = 'recover-me'
            """
        )
        with mock.patch.object(
            detached_worker.tmux_console,
            "has_tmux_session",
            return_value=False,
        ):
            result, worker = detached_worker.recover_worker(
                self.store,
                self.worker.name,
                now=1_700_000_100,
            )
        self.assertEqual(result, "unavailable")
        self.assertEqual(worker.restart_count, 0)
        self.assertIn("No provider session ID", worker.last_recovery_error)


class DetachedWorkerTopicTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database_path = Path(self.directory.name) / "controller.sqlite3"
        self.store = DurableStore(self.database_path)
        self.store.__enter__()
        self.addCleanup(lambda: self.store.__exit__(None, None, None))
        self.origin_binding = self.store.ensure_surface_binding(
            chat_id=-1004472153577,
            message_thread_id=103,
            surface_type="task",
            display_name="Test detach",
            target_type="agent",
            target_id="agent_origin",
        )
        now = 1_700_000_000.0
        self.store.connection.execute(
            """
            INSERT INTO agents(
                agent_id, parent_agent_id, role, slug, hierarchical_name,
                provider, project_path, provider_session_id,
                surface_binding_id, lifecycle_state, created_at, updated_at
            )
            VALUES (
                'agent_origin', NULL, 'project', 'test-detach',
                'root/test-detach', 'claude', '/tmp/project', NULL, ?,
                'running', ?, ?
            )
            """,
            (self.origin_binding.binding_id, now, now),
        )
        self.store.connection.commit()

    def test_managed_turn_origin_uses_its_group_not_the_configured_chat(self):
        with mock.patch.dict(
            "os.environ",
            {"TELEGRAM_CONTROL_AGENT_ID": "agent_origin"},
            clear=False,
        ):
            chat_id, agent_id = telegram_control._worker_origin_context(self.store)
        self.assertEqual(chat_id, -1004472153577)
        self.assertEqual(agent_id, "agent_origin")

    def test_worker_start_records_origin_and_creates_topic_in_its_group(self):
        worker = SimpleNamespace(
            name="haiku-poems",
            provider="claude",
            project_path=self.directory.name,
            tmux_session_name="detached--haiku-poems",
            provider_session_id="session-123",
            recovery_file_path="/tmp/RECOVERY.md",
        )
        args = SimpleNamespace(
            name="haiku-poems",
            provider="claude",
            model="haiku",
            effort=None,
            project_path=self.directory.name,
            db=self.database_path,
        )
        with (
            mock.patch.dict(
                "os.environ",
                {"TELEGRAM_CONTROL_AGENT_ID": "agent_origin"},
                clear=False,
            ),
            mock.patch.object(
                telegram_control,
                "open_store",
                return_value=contextlib.nullcontext(self.store),
            ),
            mock.patch.object(
                telegram_control,
                "_ensure_worker_topic",
                return_value=self.origin_binding,
            ) as ensure_topic,
            mock.patch.object(
                telegram_control.detached_worker,
                "create_worker",
                return_value=worker,
            ) as create_worker,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            telegram_control.worker_start_command(args)

        ensure_topic.assert_called_once_with(
            self.store,
            "haiku-poems",
            -1004472153577,
        )
        self.assertEqual(
            create_worker.call_args.kwargs["origin_agent_id"],
            "agent_origin",
        )

    def test_supergroup_topic_creation_does_not_require_private_threaded_mode(self):
        with (
            mock.patch.object(
                telegram_control.bridge,
                "read_token",
                return_value="token",
            ),
            mock.patch.object(
                telegram_control.bridge,
                "api_call",
                return_value={"message_thread_id": 220},
            ) as api_call,
        ):
            binding = telegram_control._ensure_worker_topic(
                self.store,
                "haiku-poems",
                -1004472153577,
            )
        self.assertEqual(binding.chat_id, -1004472153577)
        self.assertEqual(binding.message_thread_id, 220)
        self.assertEqual(binding.target_type, "detached_worker")
        self.assertEqual(binding.target_id, "haiku-poems")
        api_call.assert_called_once_with(
            "token",
            "createForumTopic",
            chat_id=-1004472153577,
            name="haiku-poems updates",
        )

    def test_topic_url_points_to_the_origin_topic_root(self):
        self.assertEqual(
            detached_worker.telegram_topic_url(self.origin_binding),
            "https://t.me/c/4472153577/103",
        )

    def test_inbound_worker_topic_message_gets_notice_not_a_router_turn(self):
        worker_binding = self.store.ensure_surface_binding(
            chat_id=-1004472153577,
            message_thread_id=220,
            surface_type="task",
            display_name="haiku-poems updates",
            target_type="controller",
            target_id="control",
        )
        self.store.create_detached_worker(
            name="haiku-poems",
            binding_id=worker_binding.binding_id,
            origin_agent_id="agent_origin",
            project_path="/tmp/project",
            provider="claude",
            tmux_session_name="detached--haiku-poems",
        )
        update = {
            "update_id": 10,
            "message": {
                "message_id": 221,
                "message_thread_id": 220,
                "is_topic_message": True,
                "from": {"id": 123, "is_bot": False},
                "chat": {
                    "id": -1004472153577,
                    "type": "supergroup",
                    "title": "Life",
                    "is_forum": True,
                },
                "text": "Hi",
            },
        }
        self.store.ingest_update(update, now=100)
        job = self.store.claim_job("worker", now=100)
        telegram_control.process_inbox_job(
            self.store,
            {
                "chat_id": 123,
                "owner_user_id": 123,
                "handler_path": str(Path(on_message.__file__).resolve()),
            },
            job,
            "worker",
        )
        response = self.store.claim_outbox("sender", now=10**12)
        self.assertEqual(response.params["message_thread_id"], 220)
        self.assertIn("report-only", response.params["text"])
        self.assertIn("haiku-poems", response.params["text"])
        self.assertIn("Test detach", response.params["text"])
        self.assertIn("https://t.me/c/4472153577/103", response.params["text"])
        router_count = self.store.connection.execute(
            "SELECT COUNT(*) FROM router_mailbox"
        ).fetchone()[0]
        self.assertEqual(router_count, 0)


if __name__ == "__main__":
    unittest.main()
