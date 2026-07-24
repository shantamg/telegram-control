import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import discovery
import on_message
import provider_adapters
import router_contract
import telegram_control
from durable_store import (
    MIGRATION_1,
    MIGRATION_2,
    MIGRATION_3,
    MIGRATION_4,
    MIGRATION_5,
    MIGRATION_6,
    MIGRATION_7,
    MIGRATION_8,
    MIGRATION_9,
    MIGRATION_10,
    MIGRATION_11,
    MIGRATION_12,
    MIGRATION_13,
    DurableStore,
    StoreError,
    validate_workspace_paths,
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


def callback_update(update_id, data, message_id=700):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": 123, "username": "tester"},
            "data": data,
            "message": {
                "message_id": int(message_id),
                "chat": {"id": 123, "type": "private"},
            },
        },
    }


class ScriptedRouterAdapter:
    """Feeds a scripted sequence of tool-call outputs to the router loop."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def run_turn(self, agent, prompt, mailbox_session_id, on_session, heartbeat):
        self.prompts.append(prompt)
        on_session("router-session-loop")
        heartbeat()
        if not self.outputs:
            raise AssertionError("scripted adapter ran out of outputs")
        return provider_adapters.ProviderTurnResult(
            provider_session_id="router-session-loop",
            final_text=json.dumps(self.outputs.pop(0)),
            usage={"input_tokens": 1, "output_tokens": 1},
        )


def make_lovely_home(base: Path) -> tuple[Path, Path, Path]:
    """Create home/software/lovely (a Git root) with a peter-app subdir."""
    home = base / "home"
    software = home / "software"
    lovely = software / "lovely"
    peter = lovely / "peter-app"
    peter.mkdir(parents=True)
    initialized = subprocess.run(
        ["git", "init", str(lovely)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stderr
    return home, Path(os.path.realpath(lovely)), Path(os.path.realpath(peter))


class DiscoveryToolTests(unittest.TestCase):
    def test_find_directory_is_bounded_and_ranked(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home, lovely, _ = make_lovely_home(Path(temporary_directory))
            roots = [Path(os.path.realpath(home))]
            result = discovery.find_directory("lovely", roots)
            self.assertEqual(len(result["candidates"]), 1)
            candidate = result["candidates"][0]
            self.assertEqual(candidate["path"], str(lovely))
            self.assertTrue(candidate["is_git_root"])
            self.assertEqual(candidate["containing_git_root"], str(lovely))

    def test_find_directory_matches_normalized_names(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home, lovely, peter = make_lovely_home(Path(temporary_directory))
            roots = [Path(os.path.realpath(home))]
            result = discovery.find_directory("peter app", roots)
            self.assertEqual(
                [candidate["path"] for candidate in result["candidates"]],
                [str(peter)],
            )

    def test_inspect_directory_reports_git_and_subdirectories(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home, lovely, _ = make_lovely_home(Path(temporary_directory))
            roots = [Path(os.path.realpath(home))]
            result = discovery.inspect_directory(str(lovely), roots)
            self.assertTrue(result["is_git_root"])
            self.assertEqual(result["subdirectories"], ["peter-app"])

    def test_inspect_outside_roots_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home, _, _ = make_lovely_home(Path(temporary_directory))
            roots = [Path(os.path.realpath(home))]
            with self.assertRaisesRegex(StoreError, "authorized"):
                discovery.inspect_directory("/etc", roots)

    def test_symlink_escape_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            home, lovely, _ = make_lovely_home(base)
            outside = base / "outside-secret"
            outside.mkdir()
            (lovely / "escape").symlink_to(outside)
            roots = [Path(os.path.realpath(home))]
            inspected = discovery.inspect_directory(str(lovely), roots)
            self.assertNotIn("escape", inspected["subdirectories"])
            with self.assertRaisesRegex(StoreError, "authorized"):
                discovery.inspect_directory(str(lovely / "escape"), roots)

    def test_symlink_cycle_terminates(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home, lovely, _ = make_lovely_home(Path(temporary_directory))
            # A cycle: lovely/loop -> software (an ancestor inside roots).
            (lovely / "loop").symlink_to(home / "software")
            roots = [Path(os.path.realpath(home))]
            result = discovery.find_directory("lovely", roots)
            self.assertTrue(
                any(
                    candidate["path"] == str(lovely)
                    for candidate in result["candidates"]
                )
            )

    def test_hidden_directories_are_skipped(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home, lovely, _ = make_lovely_home(Path(temporary_directory))
            roots = [Path(os.path.realpath(home))]
            inspected = discovery.inspect_directory(str(lovely), roots)
            self.assertNotIn(".git", inspected["subdirectories"])

    def test_direct_hidden_directory_inspection_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home, lovely, _ = make_lovely_home(Path(temporary_directory))
            roots = [Path(os.path.realpath(home))]
            with self.assertRaisesRegex(StoreError, "Hidden or excluded"):
                discovery.inspect_directory(str(lovely / ".git"), roots)

    def test_no_ref_for_containing_git_root_outside_roots(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home, lovely, peter = make_lovely_home(Path(temporary_directory))
            # Authorize only the subdirectory: its containing Git root lies
            # outside the discovery roots and must never receive a ref.
            roots = [peter]
            result = discovery.inspect_directory(str(peter), roots)
            annotated, issued = telegram_control.annotate_discovery_refs(
                result,
                {},
                "the peter app",
                roots,
                10,
            )
            self.assertIn("ref", annotated)
            self.assertNotIn("git_root_ref", annotated)
            issued_paths = {info["path"] for info in issued.values()}
            self.assertNotIn(str(lovely), issued_paths)

    def test_workspace_validation_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            _, lovely, peter = make_lovely_home(base)
            outside = base / "outside-secret"
            outside.mkdir()
            escaped = lovely / "escaped"
            escaped.symlink_to(outside)
            root, workdir = validate_workspace_paths(str(lovely), str(peter))
            self.assertEqual((root, workdir), (str(lovely), str(peter)))
            with self.assertRaisesRegex(StoreError, "inside the workspace"):
                validate_workspace_paths(str(lovely), str(escaped))
            with self.assertRaisesRegex(StoreError, "not the root"):
                discovery.validate_repository_workspace(str(peter), None)


class ConversationalControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        base = Path(self.temporary_directory.name)
        self.home, self.lovely, self.peter = make_lovely_home(base)
        self.database_path = base / "controller.sqlite3"
        self.store = DurableStore(self.database_path)
        self.store.ensure_surface_binding(
            chat_id=123,
            message_thread_id=None,
            surface_type="control",
            display_name="Control",
            target_type="controller",
            target_id="control",
            now=100,
        )

    def tearDown(self):
        self.store.close()
        self.temporary_directory.cleanup()

    def enqueue_router_turn(self, text, update_id=10):
        self.store.ingest_update(message_update(update_id, text), now=100)
        job_row = self.store.connection.execute(
            "SELECT job_id FROM inbox_jobs WHERE update_id = ?",
            (update_id,),
        ).fetchone()
        self.store.enqueue_router_message_with_receipt(
            source_inbox_job_id=int(job_row["job_id"]),
            input_text=text,
            chat_id=123,
            message_thread_id=None,
            authorized_user_id=123,
            receipt_text="🧭 Routing…",
            now=101,
        )
        return self.store.claim_router_mailbox("router", now=102)

    def run_router(self, adapter, job):
        roots = [Path(os.path.realpath(self.home))]
        with mock.patch.object(
            telegram_control.discovery,
            "load_discovery_roots",
            return_value=roots,
        ):
            with mock.patch.object(
                telegram_control.provider_adapters,
                "adapter_for",
                return_value=adapter,
            ):
                telegram_control.process_router_mailbox_job(
                    self.store,
                    job,
                    "router",
                )

    def test_non_git_directory_can_become_agent_workspace(self):
        life = Path(os.path.realpath(self.home / "life"))
        notes = life / "notes"
        notes.mkdir(parents=True)
        response, plan = telegram_control.project_creation_proposal(
            self.store,
            "Add a workspace called Life for my life notes directory.",
            {
                "project": "loc_life",
                "name": "Life",
                "working_directory": "loc_notes",
                "topic_name": "Life",
                "provider": "codex",
                "model": None,
                "effort": None,
            },
            {
                "loc_life": {
                    "path": str(life),
                    "derived_from": "my life directory",
                },
                "loc_notes": {
                    "path": str(notes),
                    "derived_from": "life notes directory",
                },
            },
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan["project_path"], str(life))
        self.assertEqual(plan["working_directory"], str(notes))
        self.assertIsNone(plan["git_repository_root"])
        self.assertIn("Git: not required", response)

        action = self.store.create_callback_action(
            operation_id="router:41:workspace:0",
            action_type="router_project_confirm",
            payload={
                "router_mailbox_id": 41,
                "label": "Create workspace agent",
                **plan,
            },
            chat_id=123,
            authorized_user_id=123,
            ttl_seconds=10**12,
            now=100,
        )
        confirm_update = callback_update(41, f"a:{action.token}")
        self.store.ingest_update(confirm_update, now=101)
        job_row = self.store.connection.execute(
            "SELECT job_id FROM inbox_jobs WHERE update_id = 41"
        ).fetchone()
        environment = {
            "TELEGRAM_CONTROL_DB": str(self.database_path),
            "TELEGRAM_CONTROL_JOB_ID": str(int(job_row["job_id"])),
            "TELEGRAM_CHAT_ID": "123",
            "TELEGRAM_FROM_ID": "123",
            "TELEGRAM_MESSAGE_ID": "700",
            "TELEGRAM_MESSAGE_THREAD_ID": "",
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            with mock.patch.object(
                on_message.bridge,
                "read_token",
                return_value="test-token",
            ), mock.patch.object(
                on_message.bridge,
                "api_call",
                return_value={"message_thread_id": 91},
            ):
                on_message.handle_callback(
                    confirm_update,
                    confirm_update["callback_query"],
                )
        project = self.store.resolve_project("life")
        self.assertIsNotNone(project)
        self.assertEqual(project.workspace_root, str(life))
        self.assertEqual(project.working_directory, str(notes))
        self.assertIsNone(project.git_repository_root)
        agent = self.store.resolve_project_agent("life")
        self.assertIsNotNone(agent)
        self.assertEqual(agent.workspace_root, str(life))
        self.assertEqual(agent.working_directory, str(notes))
        self.assertIsNone(agent.git_repository_root)

    def test_lovely_request_resolves_workdir_and_proposes_creation(self):
        user_text = (
            "Can we add a project called Lovely, which is actually the "
            "Peter app subdirectory of the lovely repo in software inside "
            "my user directory."
        )
        job = self.enqueue_router_turn(user_text)
        adapter = ScriptedRouterAdapter(
            [
                {"tool": "find_directory", "arguments": {"query": "lovely"}},
                {"tool": "inspect_directory", "arguments": {"path": "@ref"}},
                {
                    "tool": "create_project_agent",
                    "arguments": {
                        "project": "@root_ref",
                        "name": "Lovely",
                        "working_directory": "@workdir_ref",
                        "topic_name": "Lovely",
                    },
                },
            ]
        )

        original_run = adapter.run_turn

        def scripted_run(agent, prompt, session, on_session, heartbeat):
            # Substitute controller-issued refs discovered in prior steps.
            state = self.store.load_router_discovery(job.mailbox_id)
            path_to_ref = {
                info["path"]: ref for ref, info in state["refs"].items()
            }
            lovely_ref = path_to_ref.get(str(self.lovely))
            peter_ref = path_to_ref.get(str(self.peter))
            for output in adapter.outputs:
                arguments = output.get("arguments", {})
                if arguments.get("path") == "@ref" and lovely_ref:
                    arguments["path"] = lovely_ref
                if arguments.get("project") == "@root_ref" and lovely_ref:
                    arguments["project"] = lovely_ref
                if (
                    arguments.get("working_directory") == "@workdir_ref"
                    and peter_ref
                ):
                    arguments["working_directory"] = peter_ref
            return original_run(agent, prompt, session, on_session, heartbeat)

        adapter.run_turn = scripted_run
        self.run_router(adapter, job)

        row = self.store.connection.execute(
            "SELECT state, tool_name, preview_text, discovery_json "
            "FROM router_mailbox"
        ).fetchone()
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(row["tool_name"], "create_project_agent")
        preview = str(row["preview_text"])
        self.assertTrue(preview.startswith("🎛 Control"))
        self.assertIn(str(self.lovely), preview)
        self.assertIn(f"Working directory: {self.peter}", preview)
        self.assertIn("Nothing will be created until you confirm", preview)
        state = json.loads(row["discovery_json"])
        self.assertEqual(len(state["steps"]), 2)
        self.assertGreaterEqual(len(state["refs"]), 2)
        # Provenance traces to the user's own request, never to an absolute
        # path produced by inspection.
        for info in state["refs"].values():
            self.assertTrue(
                user_text.startswith(str(info["derived_from"])[:40])
                or str(info["derived_from"]) in user_text
            )
            self.assertFalse(str(info["derived_from"]).startswith("/"))
        # Nothing was mutated before confirmation.
        self.assertIsNone(self.store.resolve_project("peter-app"))
        self.assertIsNone(self.store.resolve_project("lovely"))
        buttons = self.store.connection.execute(
            """
            SELECT action_type, payload_json FROM callback_actions
            WHERE state = 'active'
            ORDER BY action_id
            """
        ).fetchall()
        self.assertEqual(
            [str(button["action_type"]) for button in buttons],
            ["router_project_confirm", "router_project_cancel"],
        )
        payload = json.loads(buttons[0]["payload_json"])
        self.assertEqual(payload["project_path"], str(self.lovely))
        self.assertEqual(payload["working_directory"], str(self.peter))
        # The project identity is Lovely — from the user's words — not the
        # working-directory basename.
        self.assertEqual(payload["slug"], "lovely")
        self.assertEqual(payload["display_name"], "Lovely")
        self.assertEqual(payload["topic_name"], "Lovely")
        self.assertNotEqual(payload["slug"], "peter-app")
        sources = {entry["source"] for entry in payload["provenance"]}
        self.assertEqual(sources, {"read_only_discovery"})

        # Confirmation creates the project with the working directory.
        token_row = self.store.connection.execute(
            "SELECT token FROM callback_actions "
            "WHERE action_type = 'router_project_confirm'"
        ).fetchone()
        confirm_update = callback_update(11, f"a:{token_row['token']}")
        self.store.ingest_update(confirm_update, now=103)
        job_row = self.store.connection.execute(
            "SELECT job_id FROM inbox_jobs WHERE update_id = 11"
        ).fetchone()
        environment = {
            "TELEGRAM_CONTROL_DB": str(self.database_path),
            "TELEGRAM_CONTROL_JOB_ID": str(int(job_row["job_id"])),
            "TELEGRAM_CHAT_ID": "123",
            "TELEGRAM_FROM_ID": "123",
            "TELEGRAM_MESSAGE_ID": "700",
            "TELEGRAM_MESSAGE_THREAD_ID": "",
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            with mock.patch.object(
                on_message.bridge,
                "read_token",
                return_value="test-token",
            ):
                with mock.patch.object(
                    on_message.bridge,
                    "api_call",
                    return_value={"message_thread_id": 88},
                ):
                    on_message.handle_callback(
                        confirm_update,
                        confirm_update["callback_query"],
                    )
        project = self.store.resolve_project("lovely")
        self.assertIsNotNone(project)
        self.assertEqual(project.display_name, "Lovely")
        self.assertEqual(project.project_path, str(self.lovely))
        self.assertEqual(project.working_directory, str(self.peter))
        agent = self.store.resolve_project_agent("lovely")
        self.assertEqual(agent.working_directory, str(self.peter))
        self.assertEqual(agent.hierarchical_name, "tc--root--lovely")
        # peter-app never became a project identity.
        self.assertIsNone(self.store.resolve_project("peter-app"))

    def test_ambiguous_reference_produces_candidate_clarification(self):
        second = self.home / "software" / "lovely-legacy"
        second.mkdir()
        job = self.enqueue_router_turn("set up the lovely project")
        adapter = ScriptedRouterAdapter(
            [
                {"tool": "find_directory", "arguments": {"query": "lovely"}},
                {
                    "tool": "ask_user",
                    "arguments": {
                        "question": (
                            "I found two candidates: lovely and "
                            "lovely-legacy. Which one?"
                        ),
                        "options": ["lovely", "lovely-legacy"],
                    },
                },
            ]
        )
        self.run_router(adapter, job)
        row = self.store.connection.execute(
            "SELECT state, tool_name, preview_text, discovery_json "
            "FROM router_mailbox"
        ).fetchone()
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(row["tool_name"], "ask_user")
        state = json.loads(row["discovery_json"])
        self.assertEqual(
            len(state["steps"][0]["result"]["candidates"]),
            2,
        )
        self.assertEqual(
            self.store.status_counts().get("agent_mailbox", {}),
            {},
        )

    def test_forged_path_and_forged_ref_fail_closed(self):
        for forged in (str(self.peter), "loc_deadbeef"):
            job = self.enqueue_router_turn(
                "make me a project please",
                update_id=20 + (0 if forged.startswith("/") else 1),
            )
            adapter = ScriptedRouterAdapter(
                [
                    {
                        "tool": "create_project_agent",
                        "arguments": {
                            "project": forged,
                            "topic_name": "Forged",
                        },
                    }
                ]
            )
            self.run_router(adapter, job)
            row = self.store.connection.execute(
                "SELECT state, last_error FROM router_mailbox "
                "WHERE mailbox_id = ?",
                (job.mailbox_id,),
            ).fetchone()
            self.assertEqual(row["state"], "queued")
            self.assertIn("discovery ref ID", str(row["last_error"]))
        self.assertEqual(len(self.store.list_projects()), 0)

    def test_discovery_loop_is_bounded_with_precise_message(self):
        job = self.enqueue_router_turn("look around for something")
        adapter = ScriptedRouterAdapter(
            [
                {"tool": "find_directory", "arguments": {"query": f"q{i}"}}
                for i in range(telegram_control.ROUTER_MAX_DISCOVERY_STEPS + 2)
            ]
        )
        self.run_router(adapter, job)
        row = self.store.connection.execute(
            "SELECT state, tool_name, preview_text, discovery_json "
            "FROM router_mailbox"
        ).fetchone()
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(row["tool_name"], "respond")
        self.assertIn("investigation limit", str(row["preview_text"]))
        self.assertTrue(str(row["preview_text"]).startswith("🎛 Control"))
        state = json.loads(row["discovery_json"])
        self.assertEqual(
            len(state["steps"]),
            telegram_control.ROUTER_MAX_DISCOVERY_STEPS,
        )

    def test_crash_retry_resumes_from_persisted_discovery_steps(self):
        job = self.enqueue_router_turn("find the lovely repo")

        class CrashingAdapter(ScriptedRouterAdapter):
            def __init__(self, outputs):
                super().__init__(outputs)
                self.calls = 0

            def run_turn(self, agent, prompt, session, on_session, heartbeat):
                self.calls += 1
                if self.calls == 3:
                    raise provider_adapters.ProviderAdapterError(
                        "simulated crash"
                    )
                return super().run_turn(
                    agent, prompt, session, on_session, heartbeat
                )

        crashing = CrashingAdapter(
            [
                {"tool": "find_directory", "arguments": {"query": "lovely"}},
                {"tool": "find_directory", "arguments": {"query": "peter"}},
                {"tool": "find_directory", "arguments": {"query": "unused"}},
            ]
        )
        self.run_router(crashing, job)
        state = self.store.load_router_discovery(job.mailbox_id)
        self.assertEqual(len(state["steps"]), 2)
        row = self.store.connection.execute(
            "SELECT state FROM router_mailbox WHERE mailbox_id = ?",
            (job.mailbox_id,),
        ).fetchone()
        self.assertEqual(row["state"], "queued")

        retry_job = self.store.claim_router_mailbox("router", now=10**12)
        self.assertEqual(retry_job.attempts, 2)
        resumed = ScriptedRouterAdapter(
            [
                {
                    "tool": "respond",
                    "arguments": {"message": "resumed after crash"},
                }
            ]
        )
        self.run_router(resumed, retry_job)
        # The retry prompt carried the persisted steps as a recovery recap.
        self.assertIn("Recovery:", resumed.prompts[0])
        self.assertIn("lovely", resumed.prompts[0])
        row = self.store.connection.execute(
            "SELECT state, preview_text FROM router_mailbox "
            "WHERE mailbox_id = ?",
            (job.mailbox_id,),
        ).fetchone()
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(
            row["preview_text"],
            "🎛 Control\n\nresumed after crash",
        )

    def test_confirmation_toctou_symlink_swap_fails_closed(self):
        import shutil

        real_peter = str(self.peter)
        outside = Path(self.temporary_directory.name) / "outside-secret"
        outside.mkdir()
        payload = {
            "router_mailbox_id": 5,
            "label": "Create project agent",
            "slug": "peter-app",
            "display_name": "Peter App",
            "provider": "codex",
            "project_path": str(self.lovely),
            "working_directory": real_peter,
            "git_repository_root": str(self.lovely),
            "topic_name": "Peter App",
            "provider_config": {},
            "provenance": [
                {
                    "value": real_peter,
                    "source": "read_only_discovery",
                    "derived_from": "peter app",
                }
            ],
        }
        action = self.store.create_callback_action(
            operation_id="router:5:project:0",
            action_type="router_project_confirm",
            payload=payload,
            chat_id=123,
            authorized_user_id=123,
            ttl_seconds=10**12,
            now=100,
        )
        # Between proposal and confirmation, the working directory is
        # swapped for a symlink escaping the repository.
        shutil.rmtree(real_peter)
        Path(real_peter).symlink_to(outside)
        update = callback_update(30, f"a:{action.token}")
        self.store.ingest_update(update, now=101)
        job_row = self.store.connection.execute(
            "SELECT job_id FROM inbox_jobs WHERE update_id = 30"
        ).fetchone()
        environment = {
            "TELEGRAM_CONTROL_DB": str(self.database_path),
            "TELEGRAM_CONTROL_JOB_ID": str(int(job_row["job_id"])),
            "TELEGRAM_CHAT_ID": "123",
            "TELEGRAM_FROM_ID": "123",
            "TELEGRAM_MESSAGE_ID": "700",
            "TELEGRAM_MESSAGE_THREAD_ID": "",
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            with self.assertRaises(StoreError):
                on_message.handle_callback(
                    update,
                    update["callback_query"],
                )
        self.assertEqual(len(self.store.list_projects()), 0)


class SchemaFourteenMigrationTests(unittest.TestCase):
    def test_v13_database_migrates_preserving_identity(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "schema-thirteen.sqlite3"
            connection = sqlite3.connect(str(path), isolation_level=None)
            connection.execute("BEGIN")
            for migration in (
                MIGRATION_1,
                MIGRATION_2,
                MIGRATION_3,
                MIGRATION_4,
                MIGRATION_5,
                MIGRATION_6,
                MIGRATION_7,
                MIGRATION_8,
                MIGRATION_9,
                MIGRATION_10,
                MIGRATION_11,
                MIGRATION_12,
                MIGRATION_13,
            ):
                for statement in migration:
                    connection.execute(statement)
            connection.execute(
                """
                INSERT INTO managed_projects(
                    project_id, slug, display_name, provider, project_path,
                    state, created_at, updated_at
                )
                VALUES ('project_legacy', 'legacy', 'Legacy', 'codex',
                    '/tmp/legacy-repo', 'active', 100, 100)
                """
            )
            connection.execute(
                """
                INSERT INTO agents(
                    agent_id, parent_agent_id, role, slug, hierarchical_name,
                    provider, project_path, provider_session_id,
                    surface_binding_id, lifecycle_state,
                    provider_config_json, created_at, updated_at
                )
                VALUES ('agent_legacy', NULL, 'project', 'legacy',
                    'tc--root--legacy', 'codex', '/tmp/legacy-repo',
                    'session-legacy', NULL, 'registered', '{}', 100, 100)
                """
            )
            connection.execute(
                """
                INSERT INTO project_aliases(
                    alias_key, alias, project_id, created_at, updated_at
                )
                VALUES ('old name', 'Old Name', 'project_legacy', 100, 100)
                """
            )
            for index, action_type in enumerate(
                ("router_project_confirm", "router_project_cancel")
            ):
                connection.execute(
                    """
                    INSERT INTO callback_actions(
                        operation_id, token, action_type, payload_json,
                        chat_id, message_thread_id, authorized_user_id,
                        one_time, state, expires_at, created_at, updated_at
                    )
                    VALUES (?, ?, ?, '{}', 123, NULL, 123, 1, 'active',
                        9e12, 100, 100)
                    """,
                    (f"router:9:project:{index}", f"legacy{index}", action_type),
                )
            connection.execute("PRAGMA user_version = 13")
            connection.execute("COMMIT")
            connection.close()

            with DurableStore(path) as store:
                self.assertEqual(
                    store.connection.execute(
                        "PRAGMA user_version"
                    ).fetchone()[0],
                    16,
                )
                project = store.resolve_project("legacy")
                self.assertEqual(project.project_id, "project_legacy")
                self.assertEqual(project.project_path, "/tmp/legacy-repo")
                self.assertEqual(
                    project.working_directory,
                    "/tmp/legacy-repo",
                )
                self.assertEqual(
                    project.git_repository_root,
                    "/tmp/legacy-repo",
                )
                agent = store.resolve_agent("agent_legacy")
                self.assertEqual(agent.provider_session_id, "session-legacy")
                self.assertEqual(agent.working_directory, "/tmp/legacy-repo")
                self.assertEqual(
                    agent.git_repository_root,
                    "/tmp/legacy-repo",
                )
                self.assertEqual(
                    store.project_alias_resolution(),
                    {"old name": "legacy"},
                )
                # Legacy in-flight project confirmations expired together.
                legacy_states = [
                    str(row["state"])
                    for row in store.connection.execute(
                        "SELECT state FROM callback_actions "
                        "WHERE operation_id LIKE 'router:9:project:%'"
                    )
                ]
                self.assertEqual(legacy_states, ["expired", "expired"])

    def test_sibling_working_directories_share_one_repository(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            _, lovely, peter = make_lovely_home(base)
            other = lovely / "other-app"
            other.mkdir()
            with DurableStore(base / "controller.sqlite3") as store:
                first, created_first = store.enroll_project(
                    slug="peter-app",
                    display_name="Peter App",
                    provider="codex",
                    project_path=str(lovely),
                    working_directory=str(peter),
                    now=100,
                )
                second, created_second = store.enroll_project(
                    slug="other-app",
                    display_name="Other App",
                    provider="codex",
                    project_path=str(lovely),
                    working_directory=str(other),
                    now=101,
                )
                self.assertTrue(created_first and created_second)
                self.assertEqual(first.project_path, second.project_path)
                # The same working directory cannot be enrolled twice.
                with self.assertRaisesRegex(StoreError, "working"):
                    store.enroll_project(
                        slug="duplicate-app",
                        display_name="Duplicate",
                        provider="codex",
                        project_path=str(lovely),
                        working_directory=str(peter),
                        now=102,
                    )


if __name__ == "__main__":
    unittest.main()
