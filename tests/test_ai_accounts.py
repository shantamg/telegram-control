"""Project account selection shared by terminal and Telegram launches."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import ai_accounts
import provider_adapters


class ProjectAccountTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        config = self.root / "config"
        config_patch = mock.patch.object(ai_accounts, "config_dir", return_value=config)
        self.addCleanup(config_patch.stop)
        config_patch.start()
        self.project = self.root / "project"
        self.project.mkdir()
        self.profile = ai_accounts.Profile(
            "Lovely", self.root / "Claude Profile", self.root / "Codex Profile"
        )
        for provider in ai_accounts.PROVIDERS:
            self.profile.home(provider).mkdir()
        path = ai_accounts._profile_path("Lovely")
        path.parent.mkdir(parents=True)
        ai_accounts._write_private(
            path,
            json.dumps({
                "label": self.profile.label,
                "claude": str(self.profile.claude),
                "codex": str(self.profile.codex),
            }),
            replace=False,
        )

    def test_nested_terminal_and_telegram_lookup_use_same_private_binding(self):
        nested = self.project / "src"
        nested.mkdir()
        self.assertIsNone(ai_accounts.project_profile(nested))
        ai_accounts._save_binding(self.project, self.profile, replace=False)
        self.assertEqual(ai_accounts.project_profile(nested), self.profile)
        self.assertEqual(
            provider_adapters.project_session_options("claude", str(nested)),
            {"projects_root": self.profile.claude / "projects"},
        )
        with self.assertRaisesRegex(ai_accounts.AccountError, "already exists"):
            ai_accounts._save_binding(self.project, self.profile, replace=False)

    def test_git_excludes_binding_and_linked_worktree_inherits_it(self):
        def git(*args):
            return subprocess.run(
                ["/usr/bin/git", "-C", str(self.project), *args],
                check=True, capture_output=True, text=True,
            )

        git("init")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "--allow-empty", "-m", "Initial commit")
        worktree = self.root / "worktree"
        git("worktree", "add", "--detach", str(worktree))
        ai_accounts._save_binding(self.project, self.profile, replace=False)
        self.assertEqual(
            git("check-ignore", ai_accounts.BINDING).stdout.strip(),
            ai_accounts.BINDING,
        )
        self.assertEqual(ai_accounts.project_profile(worktree), self.profile)

    def test_invalid_binding_fails_closed(self):
        marker = self.project / ai_accounts.BINDING
        marker.write_text("Lovely\n")
        marker.chmod(0o644)
        with self.assertRaisesRegex(provider_adapters.ProviderAdapterError, "owner-only"):
            provider_adapters.project_account(str(self.project))
        marker.chmod(0o600)
        marker.write_text("Missing\n")
        with self.assertRaisesRegex(provider_adapters.ProviderAdapterError, "is missing"):
            provider_adapters.project_account(str(self.project))

    def test_tmux_applies_profile_inside_pane_and_removes_inherited_api_key(self):
        command = self.profile.tmux_command(
            "claude", ["/usr/bin/printenv", "CLAUDE_CONFIG_DIR"]
        )
        output = subprocess.check_output(
            command, env={**os.environ, "CLAUDE_CONFIG_DIR": "/some/other/account"},
            text=True,
        )
        self.assertEqual(output.strip(), str(self.profile.claude))
        command = self.profile.tmux_command(
            "claude", ["/usr/bin/printenv", "ANTHROPIC_API_KEY"]
        )
        result = subprocess.run(
            command, env={**os.environ, "ANTHROPIC_API_KEY": "inherited"},
            capture_output=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
