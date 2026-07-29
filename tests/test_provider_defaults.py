import json
import tempfile
import unittest
from pathlib import Path

import provider_defaults


class ProviderDefaultsTests(unittest.TestCase):
    def test_codex_defaults_follow_local_config(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            config = home / ".codex" / "config.toml"
            config.parent.mkdir()
            config.write_text(
                'model = "gpt-5.6-sol"\n'
                'model_reasoning_effort = "high"\n'
                "\n[projects.\"/tmp/example\"]\n"
                'trust_level = "trusted"\n',
                encoding="utf-8",
            )

            model, effort = provider_defaults.describe_provider_config(
                "codex",
                {},
                "/tmp/example",
                home_directory=home,
            )

            self.assertEqual(model, "Default (currently gpt-5.6-sol)")
            self.assertEqual(effort, "Default (currently high)")
            self.assertEqual(
                provider_defaults.provider_turn_summary(
                    "codex",
                    {},
                    "/tmp/example",
                    home_directory=home,
                ),
                "Codex · gpt-5.6-sol · high effort",
            )

    def test_ollama_backend_uses_its_configured_model_without_cloud_effort(self):
        config = {
            "model_provider": "ollama",
            "model": "small:1b",
        }

        self.assertEqual(
            provider_defaults.describe_provider_config("codex", config),
            ("small:1b", "Model-controlled"),
        )
        self.assertEqual(
            provider_defaults.provider_turn_summary("codex", config),
            "Codex · Ollama · small:1b",
        )
        self.assertEqual(
            provider_defaults.provider_display_name("codex", config),
            "Codex (Ollama)",
        )

    def test_claude_defaults_follow_user_and_project_settings(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            user_settings = home / ".claude" / "settings.json"
            user_settings.parent.mkdir()
            user_settings.write_text(
                json.dumps({"model": "opus"}),
                encoding="utf-8",
            )
            project = home / "workspace"
            project_settings = project / ".claude" / "settings.local.json"
            project_settings.parent.mkdir(parents=True)
            project_settings.write_text(
                json.dumps({"model": "fable", "effort": "xhigh"}),
                encoding="utf-8",
            )

            model, effort = provider_defaults.describe_provider_config(
                "claude",
                {},
                str(project),
                home_directory=home,
            )

            self.assertEqual(model, "Default (currently fable)")
            self.assertEqual(effort, "Default (currently xhigh)")

    def test_explicit_fields_mix_with_resolved_claude_defaults(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            settings = home / ".claude" / "settings.json"
            settings.parent.mkdir()
            settings.write_text(
                json.dumps({"model": "opus"}),
                encoding="utf-8",
            )

            model, effort = provider_defaults.describe_provider_config(
                "claude",
                {"model": "sonnet"},
                home_directory=home,
            )
            self.assertEqual(model, "sonnet")
            self.assertEqual(effort, "Default (currently high)")

            model, effort = provider_defaults.describe_provider_config(
                "claude",
                {"effort": "medium"},
                home_directory=home,
            )
            self.assertEqual(model, "Default (currently opus)")
            self.assertEqual(effort, "medium")


if __name__ == "__main__":
    unittest.main()
