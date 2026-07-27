import json
import tempfile
import unittest
from pathlib import Path

import app_config


class AppConfigTests(unittest.TestCase):
    def test_defaults_disable_control_and_topic_confirmation(self):
        settings = app_config.effective_settings({})

        self.assertFalse(settings["control_agent"]["enabled"])
        self.assertFalse(settings["topics"]["confirm_agent"])
        self.assertEqual(settings["defaults"]["provider"], "auto")
        self.assertEqual(settings["presentation"]["status_style"], "standard")

    def test_install_shared_and_local_workspace_layers_merge(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / app_config.WORKSPACE_CONFIG_NAME).write_text(
                json.dumps(
                    {
                        "defaults": {"provider": "claude"},
                        "prompts": {"response_style": "Use short paragraphs."},
                    }
                ),
                encoding="utf-8",
            )
            (workspace / app_config.LOCAL_WORKSPACE_CONFIG_NAME).write_text(
                json.dumps(
                    {
                        "prompts": {"preamble": "Call me Sam."},
                        "presentation": {"status_style": "compact"},
                    }
                ),
                encoding="utf-8",
            )

            settings = app_config.effective_settings(
                {
                    "telegram_control": {
                        "control_agent": {"enabled": True},
                        "defaults": {"provider": "codex"},
                    }
                },
                str(workspace),
            )

        self.assertTrue(settings["control_agent"]["enabled"])
        self.assertEqual(settings["defaults"]["provider"], "claude")
        self.assertEqual(settings["prompts"]["preamble"], "Call me Sam.")
        self.assertEqual(
            settings["prompts"]["response_style"],
            "Use short paragraphs.",
        )
        self.assertEqual(settings["presentation"]["status_style"], "compact")

    def test_unknown_and_invalid_settings_fail_closed(self):
        with self.assertRaisesRegex(app_config.ConfigError, "Unknown"):
            app_config.effective_settings(
                {"telegram_control": {"permission_magic": True}}
            )
        with self.assertRaisesRegex(app_config.ConfigError, "auto, codex, or claude"):
            app_config.effective_settings(
                {
                    "telegram_control": {
                        "defaults": {"provider": "gemini"}
                    }
                }
            )

    def test_prompt_addition_keeps_custom_content_separate(self):
        settings = app_config.effective_settings(
            {
                "telegram_control": {
                    "prompts": {
                        "preamble": "Remember the audience.",
                        "response_style": "Be concise.",
                    }
                }
            }
        )

        self.assertEqual(
            app_config.prompt_addition(settings),
            "User-configured standing context:\nRemember the audience.\n\n"
            "User-configured response style:\nBe concise.",
        )


if __name__ == "__main__":
    unittest.main()
