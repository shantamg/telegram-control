import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "herdr_host", Path(__file__).resolve().parents[1] / "deploy/herdr-host.py"
)
herdr_host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(herdr_host)


class HerdrHostInstallTest(unittest.TestCase):
    def test_install_keeps_relay_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            binary = home / ".local/bin"
            binary.mkdir(parents=True)
            hc = binary / "hc"
            hc.write_text("#!/bin/sh\nexit 0\n")
            hc.chmod(0o755)
            cli = home / ".local/share/herdr-tools/bin/codex"
            cli.parent.mkdir(parents=True)
            cli.write_text("#!/bin/sh\necho --no-daemon\n")
            cli.chmod(0o755)

            with patch.dict("os.environ", {"XDG_CONFIG_HOME": "", "XDG_DATA_HOME": ""}):
                _, deploy, bridge = herdr_host.base_paths(home)
                bridge.mkdir(parents=True)
                relay = bridge / "herdr-relay.json"
                relay.write_text('{"hub":"trusted-hub"}')
                first = herdr_host.install(home)
                self.assertTrue(all(first["tools"].values()))
                self.assertEqual(first, herdr_host.install(home))
                self.assertEqual(first["hub"], "trusted-hub")
                self.assertEqual(relay.read_text(), '{"hub":"trusted-hub"}')
                self.assertEqual((deploy / "app").resolve(), herdr_host.ROOT)
                skill = home / ".agents/skills/herdr-telegram-handoff/SKILL.md"
                self.assertIn("herdr-telegram link", skill.read_text())
                self.assertNotIn("{{TELEGRAM_CONTROL_ROOT}}", skill.read_text())

    def test_normal_command_installs_new_main_and_works_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / "home"
            deploy = home / ".local/share/telegram-control"
            deploy.mkdir(parents=True)
            repo = root / "upstream"
            repo.mkdir()
            env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME="", XDG_DATA_HOME="")
            binary = home / ".local/bin"
            binary.mkdir(parents=True)
            env["PATH"] = str(binary) + os.pathsep + env["PATH"]
            for name, body in (("hc", "exit 0"), ("systemctl", "exit 1")):
                (binary / name).write_text("#!/bin/sh\n" + body + "\n")
                (binary / name).chmod(0o755)
            codex = home / ".local/share/herdr-tools/bin/codex"
            codex.parent.mkdir(parents=True)
            codex.write_text("#!/bin/sh\necho --no-daemon\n")
            codex.chmod(0o755)

            def git(*args):
                return subprocess.check_output(["git", *args], cwd=repo, env=env, text=True,
                                               stderr=subprocess.PIPE).strip()

            git("init", "-b", "main")
            git("config", "user.email", "host@example.test")
            git("config", "user.name", "Host test")
            for folder in ("deploy", "skills"):
                shutil.copytree(herdr_host.ROOT / folder, repo / folder)
            vendor = repo / "vendor/ai-accounts"
            vendor.mkdir(parents=True)
            (vendor / "README").touch()
            (repo / "telegram_control.py").touch()
            helper = repo / "herdr_telegram.py"
            helper.write_text("print('old')\n")
            git("add", ".")
            git("commit", "-m", "First release")
            first = deploy / "app-first"
            git("clone", str(repo), str(first))
            subprocess.run([sys.executable, str(first / "deploy/herdr-host.py"), "install"],
                           check=True, env=env, stdout=subprocess.PIPE)
            helper.write_text("import sys\nprint(sys.argv[1], sys.stdin.read())\n")
            git("commit", "-am", "Second release")
            revision = git("rev-parse", "HEAD")

            def invoke():
                return subprocess.run([str(binary / "herdr-telegram"), "new"], input="voice",
                                      text=True, capture_output=True, env=env, check=True)

            self.assertEqual(invoke().stdout, "new voice\n")
            self.assertEqual((deploy / "app").resolve(), deploy / ("app-" + revision))
            self.assertEqual(invoke().stderr, "")
            repo.rename(root / "offline")
            result = invoke()
            self.assertEqual(result.stdout, "new voice\n")
            self.assertIn("Using the installed release", result.stderr)
