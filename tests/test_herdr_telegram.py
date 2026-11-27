import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import herdr_telegram as handoff
import on_message
import telegram_control
from durable_store import DurableStore, HerdrVoiceStage, StoreError


class HerdrTelegramTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = DurableStore(self.root / "controller.sqlite3")
        self.addCleanup(self.store.close)
        self.store.authorize_and_bind_forum_workspace(
            chat_id=-10012345,
            display_name="Example project",
            project_path=str(self.root),
        )
        self.terminal = handoff.Terminal("t1", "t1:agent-one", self.root, "idle")

    def create_topic(self):
        with patch.object(handoff.bridge, "read_token", return_value="fake"), patch.object(
            handoff.bridge, "api_call", side_effect=[
                {"type": "supergroup", "is_forum": True},
                {"message_thread_id": 7},
            ],
        ) as api:
            binding = handoff.link_topic(self.store, self.terminal, "Example", None)
            self.assertEqual(binding.target_type, "herdr")
            self.assertEqual(api.call_count, 2)
        return binding

    def test_terminal_identity_is_independent_of_caller_timezone(self):
        row = {
            "address": "t1",
            "state": "live",
            "pid": os.getpid(),
            "created_ms": 1,
            "cwd": str(self.root),
            "status": {"app": "claude-code", "state": "idle"},
        }
        with patch.object(
            handoff, "hc", side_effect=[row, {"core": {"id": "core-one"}}] * 2
        ), patch.object(handoff, "agent_process", return_value=os.getpid()), patch.object(
            handoff, "provider_session_id", return_value="session-one"
        ):
            with patch.dict(os.environ, {"TZ": "America/Los_Angeles"}):
                pacific = handoff.inspect_terminal("t1")
            with patch.dict(os.environ, {"TZ": "UTC"}):
                utc = handoff.inspect_terminal("t1")

        self.assertEqual(pacific.target_id, utc.target_id)

    def test_repeated_handoff_reuses_topic_without_creating_managed_agent(self):
        binding = self.create_topic()
        with patch.object(handoff.bridge, "api_call") as api:
            again = handoff.link_topic(self.store, self.terminal, "Different title", None)
        self.assertEqual(again, binding)
        api.assert_not_called()
        self.assertIsNone(self.store.resolve_forum_subject(binding.chat_id, 7))
        self.assertEqual(handoff.detached_worker.telegram_topic_url(binding), "https://t.me/c/12345/7")
        self.assertEqual(
            self.store.outbox_operation_state(f"herdr-topic:{binding.binding_id}:intro"),
            "queued",
        )

    def test_uncertain_topic_create_is_not_retried(self):
        with patch.object(handoff.bridge, "read_token", return_value="fake"), patch.object(
            handoff.bridge, "api_call", side_effect=[
                {"type": "supergroup", "is_forum": True},
                handoff.bridge.BridgeError("Could not reach Telegram."),
            ],
        ) as api:
            with self.assertRaises(handoff.bridge.BridgeError):
                handoff.link_topic(self.store, self.terminal, "Example", None)
            with self.assertRaisesRegex(StoreError, "unknown result"):
                handoff.link_topic(self.store, self.terminal, "Example", None)
        self.assertEqual(api.call_count, 2)

    def herdr_env(self, binding, job_id, attempt):
        return {
            "TELEGRAM_CONTROL_DB": str(self.store.path),
            "TELEGRAM_CONTROL_JOB_ID": str(job_id),
            "TELEGRAM_CONTROL_JOB_ATTEMPT": str(attempt),
            "TELEGRAM_CHAT_ID": str(binding.chat_id),
            "TELEGRAM_CHAT_TYPE": "supergroup",
            "TELEGRAM_MESSAGE_THREAD_ID": "7",
            "TELEGRAM_FROM_ID": "12",
        }

    def test_working_agent_receives_prompt_without_idle_guard(self):
        binding = self.create_topic()
        working = handoff.Terminal("t1", "t1:agent-one", self.root, "working")
        update = {"message": {"message_id": 51, "text": "Also check the logs."}}
        with patch.dict(os.environ, self.herdr_env(binding, 31, 1)), patch.object(
            on_message, "send_message"
        ), patch.object(handoff, "inspect_terminal", return_value=working), patch.object(
            handoff, "hc"
        ) as hc, patch("sys.stdin", io.StringIO(json.dumps(update))):
            self.assertEqual(on_message.main(), 0)
        hc.assert_called_once()
        self.assertEqual(hc.call_args.args[:2], ("send", "t1"))

    def test_blocked_agent_retries_quietly_and_delivers_later(self):
        binding = self.create_topic()
        blocked = handoff.Terminal("t1", "t1:agent-one", self.root, "blocked")
        update = {"message": {"message_id": 52, "voice": {"duration": 3}}}
        notices = []
        for attempt in (1, 2):
            with patch.dict(os.environ, self.herdr_env(binding, 32, attempt)), patch.object(
                on_message, "send_message", side_effect=lambda text, **_: notices.append(text)
            ), patch.object(
                on_message, "transcribe_voice_note", return_value="Ship it."
            ), patch.object(handoff, "inspect_terminal", return_value=blocked), patch.object(
                handoff, "hc"
            ) as hc, patch("sys.stdin", io.StringIO(json.dumps(update))):
                self.assertEqual(on_message.main(), handoff.bridge.HERDR_BUSY_HANDLER_EXIT)
            hc.assert_not_called()
        self.assertEqual(notices, [])
        methods = []
        while (item := self.store.claim_outbox("test")) is not None:
            methods.append((item.method, item.params["text"]))
            self.store.complete_outbox(item.message_id, "test", {"message_id": 93})
        self.assertEqual(len([body for _, body in methods if "Transcribing" in body]), 1)
        self.assertEqual(len([body for method, body in methods
                              if method == "editMessageText" and "Waiting for Herdr" in body]), 1)
        # Once the agent is free, the same job delivers instead of being skipped.
        with patch.dict(os.environ, self.herdr_env(binding, 32, 3)), patch.object(
            on_message, "send_message"
        ), patch.object(
            on_message, "transcribe_voice_note", return_value="Ship it."
        ), patch.object(handoff, "inspect_terminal", return_value=self.terminal), patch.object(
            handoff, "hc"
        ) as hc, patch("sys.stdin", io.StringIO(json.dumps(update))):
            self.assertEqual(on_message.main(), 0)
        hc.assert_called_once()
        self.assertIn("Ship it.", hc.call_args.args[-1])

    def test_core_status_mismatch_releases_turn_for_retry(self):
        binding = self.create_topic()
        with patch.object(handoff, "inspect_terminal", return_value=self.terminal), patch.object(
            handoff, "hc", side_effect=handoff.HerdrBusy("not ready for input")
        ):
            with self.assertRaises(handoff.HerdrBusy):
                handoff.forward_input(self.store, binding, 33, 53, "Hi", voice=False)
        self.assertIsNone(handoff.read_state(self.store, "herdr-turn:33"))

    def test_voice_routes_to_existing_terminal_and_replies_once(self):
        binding = self.create_topic()
        env = {
            "TELEGRAM_CONTROL_DB": str(self.store.path),
            "TELEGRAM_CONTROL_JOB_ID": "23",
            "TELEGRAM_CHAT_ID": str(binding.chat_id),
            "TELEGRAM_CHAT_TYPE": "supergroup",
            "TELEGRAM_MESSAGE_THREAD_ID": "7",
            "TELEGRAM_FROM_ID": "12",
        }
        update = {"message": {"message_id": 41, "voice": {"duration": 3}}}
        with patch.dict(os.environ, env), patch.object(
            on_message, "send_message"
        ), patch.object(
            on_message, "transcribe_voice_note", return_value="Run the checks."
        ), patch.object(
            handoff, "inspect_terminal", return_value=self.terminal
        ), patch.object(handoff, "hc") as hc, patch(
            "sys.stdin", io.StringIO(json.dumps(update))
        ):
            self.assertEqual(on_message.main(), 0)
            self.assertTrue(on_message.handle_herdr_topic(update["message"]))
        hc.assert_called_once()
        args = hc.call_args.args
        self.assertEqual(args[:4], ("send", "--if", "idle", "t1"))
        self.assertTrue(args[-1].startswith("Run the checks.\n\n"))
        ticket = re.search(r"reply (23:[a-f0-9]{32})", args[-1])[1]
        intro = self.store.claim_outbox("test")
        self.store.complete_outbox(intro.message_id, "test", {"message_id": 90})
        receipt = self.store.claim_outbox("test")
        self.assertIn("Transcribing", receipt.params["text"])
        self.store.complete_outbox(receipt.message_id, "test", {"message_id": 91})
        sending = self.store.claim_outbox("test")
        self.assertEqual(sending.method, "editMessageText")
        self.assertEqual(sending.params["message_id"], 91)
        self.assertTrue(sending.params["text"].endswith("You said: “Run the checks.”"))
        self.assertIsNone(self.store.claim_outbox("other-sender"))
        self.store.complete_outbox(sending.message_id, "test", True)
        working = self.store.claim_outbox("test")
        self.assertEqual(working.method, "editMessageText")
        self.assertEqual(working.params["message_id"], 91)
        self.assertIn("Herdr is working", working.params["text"])
        self.assertTrue(working.params["text"].endswith("You said: “Run the checks.”"))
        with patch.object(
            handoff.voice_responses, "synthesize_voice", return_value=self.root / "speech.ogg"
        ) as speech:
            wrong_session = handoff.Terminal("t1", "t1:agent-two", self.root, "idle")
            with self.assertRaisesRegex(StoreError, "changed or closed"):
                handoff.queue_reply(self.store, ticket, "Wrong session", wrong_session)
            handoff.queue_reply(self.store, ticket, "The checks passed.", self.terminal)
            handoff.queue_reply(self.store, ticket, "Repeated answer", self.terminal)
        speech.assert_called_once()
        self.store.complete_outbox(working.message_id, "test", True)
        deliveries = []
        while (item := self.store.claim_outbox("test")) is not None:
            deliveries.append(item)
            self.store.complete_outbox(item.message_id, "test", {"message_id": 92})
        self.assertCountEqual([item.method for item in deliveries], ["editMessageText", "sendVoice"])
        answer = next(item for item in deliveries if item.method == "editMessageText")
        self.assertEqual(answer.params, {"chat_id": binding.chat_id, "message_id": 91,
                                         "text": "The checks passed."})
        audio = next(item for item in deliveries if item.method == "sendVoice")
        self.assertEqual(audio.params["message_thread_id"], 7)
        self.assertEqual(audio.params["reply_parameters"]["message_id"], 41)
        self.store.stage_herdr_voice_edit(23, HerdrVoiceStage.WORKING, {"text": "late working status"})
        self.assertIsNone(self.store.claim_outbox("test"))

    def test_herdr_only_receiver_holds_old_topics_and_dispatches_linked_ones(self):
        binding = self.create_topic()
        updates = [
            {"update_id": update_id, "message": {
                "chat": {"id": binding.chat_id, "type": "supergroup", "is_forum": True},
                "message_thread_id": thread_id,
                "from": {"id": 12},
                "text": "Test routing",
            }}
            for update_id, thread_id in [(71, 6), (72, 7)]
        ]
        config = {"chat_id": 12, "owner_user_id": 12, "herdr_only_receiver": True}
        with patch.object(telegram_control.bridge, "api_call", return_value=updates), patch.object(
            telegram_control.bridge, "save_offset"
        ):
            telegram_control.collect_once(self.store, "fake", config)
        job = self.store.claim_job("inbox")
        self.assertEqual(job.update_id, 72)
        self.assertIsNone(self.store.claim_job("inbox"))
        record = self.store.connection.execute(
            "SELECT raw_json FROM telegram_updates WHERE update_id = 71"
        ).fetchone()
        self.assertEqual(json.loads(record["raw_json"])["held_for_mac"], updates[0])

    def test_remote_prompt_uses_peer_script_and_terminal_identity(self):
        remote = handoff.Terminal("peer/t1", "peer/t1:agent-one", self.root, "idle")
        binding = self.store.ensure_surface_binding(
            -10012345, "task", "Remote", "herdr", remote.target_id, 8
        )
        relay_config = {"peers": {"peer": {"ssh": "peer-alias", "script": "/srv/peer/herdr_telegram.py"}}}
        with patch.object(handoff, "linked_terminal", return_value=remote), patch.object(
            handoff, "relay_configuration", return_value=relay_config
        ), patch.object(handoff, "relay_call") as relay:
            handoff.forward_input(self.store, binding, 42, 17, "Check this.", voice=True)

        alias, request = relay.call_args.args
        self.assertEqual(alias, "peer-alias")
        self.assertEqual(request["target_id"], "t1:agent-one")
        self.assertIn("/srv/peer/herdr_telegram.py reply 42:", request["prompt"])
        self.assertIn("Check this.", request["prompt"])

    def test_explicit_herdr_voice_uses_the_linked_topic_once(self):
        binding = self.create_topic()
        with patch.object(
            handoff.voice_responses, "synthesize_voice", return_value=self.root / "speech.ogg"
        ) as speech:
            self.assertEqual(handoff.queue_voice(self.store, self.terminal, "voice-test", "Hello"), "queued")
            self.assertEqual(handoff.queue_voice(self.store, self.terminal, "voice-test", "Hello"), "queued")
        speech.assert_called_once()
        voice = self.store.connection.execute(
            "SELECT params_json FROM outbox_messages WHERE method = 'sendVoice'"
        ).fetchone()
        self.assertEqual(json.loads(voice["params_json"])["message_thread_id"], binding.message_thread_id)

    def test_requested_audio_completes_turn_once(self):
        binding = self.create_topic()
        for job, voice in ((25, True), (26, False)):
            with patch.object(handoff, "linked_terminal", return_value=self.terminal), patch.object(
                handoff, "hc"
            ) as hc:
                handoff.forward_input(self.store, binding, job, 49, "Send the report as audio.", voice=voice)
            ticket = re.search(r"reply (\d+:[a-f0-9]{32})", hc.call_args.args[-1])[1]
            with patch.object(handoff.voice_responses, "synthesize_voice",
                              return_value=self.root / "answer.ogg") as speech:
                with self.assertRaisesRegex(StoreError, "open Telegram turn"):
                    handoff.queue_voice(self.store, self.terminal, "report", "The report")
                handoff.queue_reply(self.store, ticket if voice else ticket + ":voice", "The report", self.terminal)
                handoff.queue_reply(self.store, ticket, "Sent a voice note.", self.terminal)
                speech.assert_called_once()
                self.assertEqual(speech.call_args.args[0], "The report")
            rows = self.store.connection.execute(
                "SELECT method, params_json FROM outbox_messages WHERE operation_id GLOB ? ORDER BY message_id",
                (f"herdr-turn:{job}:reply:*",),
            ).fetchall()
            self.assertEqual([row["method"] for row in rows], ["sendMessage", "sendVoice"])
            self.assertEqual(json.loads(rows[0]["params_json"])["text"], "The report")

    def test_changed_terminal_never_receives_a_prompt(self):
        binding = self.create_topic()
        replacement = handoff.Terminal("t1", "t1:agent-two", self.root, "idle")
        with patch.object(handoff, "inspect_terminal", return_value=replacement), patch.object(
            handoff, "hc"
        ) as hc:
            with self.assertRaisesRegex(StoreError, "changed or closed"):
                handoff.forward_input(self.store, binding, 23, 41, "Check it.", voice=False)
        hc.assert_not_called()

    def test_text_turn_queues_a_text_reply(self):
        binding = self.create_topic()
        with patch.object(handoff, "inspect_terminal", return_value=self.terminal), patch.object(
            handoff, "hc"
        ) as hc:
            handoff.forward_input(self.store, binding, 28, 47, "Review this.", voice=False)
        ticket = re.search(r"reply (28:[a-f0-9]{32})", hc.call_args.args[-1])[1]
        with patch.object(handoff.voice_responses, "synthesize_voice") as speech:
            handoff.queue_reply(self.store, ticket, "Reviewed.", self.terminal)
        speech.assert_not_called()
        self.assertEqual(self.store.outbox_operation_state("herdr-turn:28:reply:text:0"), "queued")
        self.assertIsNone(self.store.outbox_operation_state("herdr-turn:28:reply:voice"))

    def test_teardown_queues_topic_deletion_and_skips_agent_provisioning(self):
        binding = self.create_topic()
        handoff.tear_down_topic(self.store, binding)
        self.assertIsNone(self.store.resolve_surface_binding(binding.chat_id, 7))
        self.assertEqual(
            self.store.outbox_operation_state(f"herdr-topic:{binding.binding_id}:delete"),
            "queued",
        )
        with patch.dict(os.environ, {
            "TELEGRAM_CONTROL_DB": str(self.store.path),
            "TELEGRAM_CHAT_ID": str(binding.chat_id),
            "TELEGRAM_MESSAGE_THREAD_ID": "7",
        }):
            self.assertTrue(on_message.handle_herdr_topic({"text": "Late message"}))
        self.assertIsNone(self.store.resolve_forum_subject(binding.chat_id, 7))

    def test_herdr_teardown_preview_does_not_delete(self):
        binding = self.create_topic()
        preview = handoff.preview_topic_teardown(self.store, self.terminal)
        self.assertEqual(preview["topic"], binding.display_name)
        self.assertEqual(preview["confirmation_id"], binding.binding_id)
        self.assertEqual(self.store.resolve_surface_binding_by_id(binding.binding_id), binding)
        self.assertIsNone(self.store.outbox_operation_state(f"herdr-topic:{binding.binding_id}:delete"))

    def test_herdr_teardown_confirmation_requires_the_same_agent(self):
        binding = self.create_topic()
        other = handoff.Terminal("t1", "t1:different-agent", self.root, "idle")
        with self.assertRaisesRegex(StoreError, "different or inactive"):
            handoff.confirm_topic_teardown(self.store, other, binding.binding_id)
        self.assertEqual(self.store.resolve_surface_binding_by_id(binding.binding_id), binding)

    def test_repeated_herdr_confirmation_cannot_delete_a_new_topic(self):
        binding = self.create_topic()
        self.assertEqual(
            handoff.confirm_topic_teardown(self.store, self.terminal, binding.binding_id),
            "queued",
        )
        newer = self.store.ensure_surface_binding(
            binding.chat_id, "task", "New handoff", "herdr", self.terminal.target_id, 8
        )
        self.assertEqual(
            handoff.confirm_topic_teardown(self.store, self.terminal, binding.binding_id),
            "queued",
        )
        self.assertEqual(self.store.resolve_surface_binding_by_id(newer.binding_id), newer)
        deletions = self.store.connection.execute(
            "SELECT count(*) FROM outbox_messages WHERE method = 'deleteForumTopic'"
        ).fetchone()[0]
        self.assertEqual(deletions, 1)
        intro = self.store.claim_outbox("sender")
        self.store.complete_outbox(intro.message_id, "sender", {})
        deletion = self.store.claim_outbox("sender")
        self.assertEqual(deletion.method, "deleteForumTopic")
        self.store.complete_outbox(deletion.message_id, "sender", {})
        self.assertEqual(
            handoff.confirm_topic_teardown(self.store, self.terminal, binding.binding_id),
            "sent",
        )
