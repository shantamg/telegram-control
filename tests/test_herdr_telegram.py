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
from durable_store import DurableStore, StoreError


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
        self.assertIn("Run the checks.", args[-1])
        ticket = re.search(r"reply (23:[a-f0-9]{32})", args[-1])[1]
        with patch.object(
            handoff.voice_responses, "synthesize_voice", return_value=self.root / "speech.ogg"
        ) as speech:
            wrong_session = handoff.Terminal("t1", "t1:agent-two", self.root, "idle")
            with self.assertRaisesRegex(StoreError, "changed or closed"):
                handoff.queue_reply(self.store, ticket, "Wrong session", wrong_session)
            handoff.queue_reply(self.store, ticket, "The checks passed.", self.terminal)
            handoff.queue_reply(self.store, ticket, "Repeated answer", self.terminal)
        speech.assert_called_once()
        rows = self.store.connection.execute(
            "SELECT method, params_json FROM outbox_messages ORDER BY message_id"
        ).fetchall()[1:]
        self.assertEqual([row["method"] for row in rows], ["sendMessage", "sendVoice"])
        for row in rows:
            params = json.loads(row["params_json"])
            self.assertEqual(params["message_thread_id"], 7)
            self.assertEqual(params["reply_parameters"]["message_id"], 41)

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
