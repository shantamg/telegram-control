import tempfile
import unittest
from pathlib import Path

import turn_guidance


class TurnGuidanceTests(unittest.TestCase):
    def test_custom_guidance_is_appended_after_core_contract(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            guidance = turn_guidance.effective_turn_guidance(
                str(workspace),
                {
                    "telegram_control": {
                        "prompts": {
                            "preamble": "Treat examples as illustrative.",
                            "response_style": "Use concise paragraphs.",
                        }
                    }
                },
            )

        self.assertTrue(guidance.startswith(turn_guidance.TURN_GUIDANCE))
        self.assertIn(
            "User-configured standing context:\n"
            "Treat examples as illustrative.",
            guidance,
        )
        self.assertTrue(guidance.endswith("Use concise paragraphs."))

    def test_default_guidance_is_byte_for_byte_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            guidance = turn_guidance.effective_turn_guidance(
                temporary_directory,
                {},
            )

        self.assertEqual(guidance, turn_guidance.TURN_GUIDANCE)


if __name__ == "__main__":
    unittest.main()
