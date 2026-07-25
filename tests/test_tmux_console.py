import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import provider_adapters
import tmux_console
from durable_store import DurableStore, StoreError


class TmuxConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        # An isolated workspace. Git happens to be present, but registration
        # deliberately records no Git metadata so console launch exercises
        # the optional-Git workspace path.
        created = Path(self.temporary_directory.name) / "project"
        created.mkdir()
        subprocess.run(
            ["git", "init", str(created)],
            capture_output=True,
            check=True,
        )
        self.project_path = Path(os.path.realpath(created))
        self.database_path = Path(self.temporary_directory.name) / "controller.sqlite3"
        self.store = DurableStore(self.database_path)
        self.store.ensure_surface_binding(
            chat_id=123,
            message_thread_id=62,
            surface_type="project",
            display_name="Project",
            target_type="controller",
            target_id="control",
        )
        self.agent, _ = self.store.register_project_agent(
            chat_id=123,
            surface_name="Project",
            slug="project",
            provider="codex",
            project_path=str(self.project_path),
        )
        self.session_id = "019f924b-bbbf-7080-b778-a52e3e1bf4cc"
        self.store.connection.execute(
            "UPDATE agents SET provider_session_id = ? WHERE agent_id = ?",
            (self.session_id, self.agent.agent_id),
        )
        self.agent = self.store.resolve_agent(self.agent.agent_id)

    def tearDown(self):
        self.store.close()
        self.temporary_directory.cleanup()

    @mock.patch.object(
        tmux_console.discovery,
        "validate_agent_workspace",
        side_effect=lambda root, workdir=None, git_root=None: (
            root,
            workdir or root,
            git_root,
        ),
    )
    @mock.patch.object(tmux_console, "has_tmux_session", return_value=False)
    @mock.patch.object(
        tmux_console.provider_adapters,
        "adapter_for",
        side_effect=lambda agent: provider_adapters.CodexExecAdapter(binary="/bin/codex"),
    )
    @mock.patch.object(tmux_console, "tmux_binary", return_value="/bin/tmux")
    @mock.patch.object(tmux_console.subprocess, "run")
    def test_open_uses_direct_tmux_argv_and_reserves_console(
        self,
        run,
        _tmux_binary,
        _adapter_for,
        _has_session,
        _validate,
    ):
        run.return_value = mock.Mock(returncode=0, stderr="")

        console = tmux_console.open_agent_console(self.store, self.agent)

        self.assertEqual(console.state, "running")
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["/bin/tmux", "new-session", "-d", "-s", "tc--root--project"])
        self.assertIn("--include-non-interactive", command)
        self.assertIn("--sandbox", command)
        self.assertIn("danger-full-access", command)
        self.assertEqual(command[-1], self.session_id)
        _validate.assert_called_once_with(
            str(self.project_path),
            str(self.project_path),
            None,
        )

    @mock.patch.object(
        tmux_console.discovery,
        "validate_agent_workspace",
        side_effect=lambda root, workdir=None, git_root=None: (
            root,
            workdir or root,
            git_root,
        ),
    )
    @mock.patch.object(tmux_console, "has_tmux_session", return_value=False)
    @mock.patch.object(
        tmux_console.provider_adapters,
        "adapter_for",
        side_effect=lambda agent: provider_adapters.CodexExecAdapter(binary="/bin/codex"),
    )
    @mock.patch.object(tmux_console, "tmux_binary", return_value="/bin/tmux")
    @mock.patch.object(tmux_console.subprocess, "run")
    def test_failed_start_releases_console_reservation(
        self,
        run,
        _tmux_binary,
        _adapter_for,
        _has_session,
        _validate,
    ):
        run.return_value = mock.Mock(returncode=1, stderr="start failed")

        with self.assertRaisesRegex(StoreError, "start failed"):
            tmux_console.open_agent_console(self.store, self.agent)

        self.assertEqual(
            self.store.resolve_agent_console(self.agent.agent_id).state,
            "stopped",
        )

    @mock.patch.object(
        tmux_console.discovery,
        "validate_agent_workspace",
        side_effect=lambda root, workdir=None, git_root=None: (
            root,
            workdir or root,
            git_root,
        ),
    )
    @mock.patch.object(tmux_console, "has_tmux_session", return_value=False)
    @mock.patch.object(
        tmux_console.provider_adapters,
        "adapter_for",
        side_effect=lambda agent: provider_adapters.ClaudePrintAdapter(binary="/bin/claude"),
    )
    @mock.patch.object(tmux_console, "tmux_binary", return_value="/bin/tmux")
    @mock.patch.object(tmux_console.subprocess, "run")
    def test_claude_console_resumes_same_session(
        self,
        run,
        _tmux_binary,
        _adapter_for,
        _has_session,
        _validate,
    ):
        run.return_value = mock.Mock(returncode=0, stderr="")
        claude_agent = replace(
            self.agent,
            provider="claude",
            provider_config={
                "model": "sonnet",
                "effort": "high",
                "permission_mode": "acceptEdits",
            },
        )

        console = tmux_console.open_agent_console(self.store, claude_agent)

        self.assertEqual(console.state, "running")
        command = run.call_args.args[0]
        self.assertIn("/bin/claude", command)
        self.assertIn("--resume", command)
        self.assertIn(self.session_id, command)
        self.assertIn("--permission-mode", command)
        self.assertIn("acceptEdits", command)
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertIn("--model", command)
        self.assertIn("sonnet", command)
        self.assertIn("--effort", command)
        self.assertIn("high", command)

    @mock.patch.object(tmux_console, "has_tmux_session", return_value=True)
    def test_unmanaged_name_collision_fails_closed(self, _has_session):
        with self.assertRaisesRegex(StoreError, "unmanaged tmux session"):
            tmux_console.open_agent_console(self.store, self.agent)
        self.assertIsNone(self.store.resolve_agent_console(self.agent.agent_id))


if __name__ == "__main__":
    unittest.main()
