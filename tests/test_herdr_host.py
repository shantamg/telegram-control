import importlib.util
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
