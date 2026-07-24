import unittest

from durable_store import ManagedProject
from router_contract import (
    RouterContractError,
    build_router_prompt,
    parse_router_decision,
)


class RouterContractTests(unittest.TestCase):
    def setUp(self):
        self.project = ManagedProject(
            project_id="project-safe",
            slug="telegram-control",
            display_name="Telegram Control",
            provider="codex",
            project_path="/secret/local/path",
            state="active",
        )

    def test_prompt_contains_catalog_without_local_path(self):
        prompt = build_router_prompt("fix the bot", [self.project])
        self.assertIn('"slug":"telegram-control"', prompt)
        self.assertIn('"name":"Telegram Control"', prompt)
        self.assertNotIn("/secret/local/path", prompt)

    def test_accepts_strict_route(self):
        decision = parse_router_decision(
            '{"action":"route","project_slug":"telegram-control",'
            '"message":"fix the bot","confidence":0.98}',
            {"telegram-control"},
        )
        self.assertEqual(decision.action, "route")
        self.assertEqual(decision.project_slug, "telegram-control")
        self.assertEqual(decision.message, "fix the bot")

    def test_rejects_unknown_project_and_extra_fields(self):
        with self.assertRaisesRegex(RouterContractError, "unknown project"):
            parse_router_decision(
                '{"action":"route","project_slug":"invented",'
                '"message":"task","confidence":0.9}',
                {"telegram-control"},
            )
        with self.assertRaisesRegex(RouterContractError, "fields"):
            parse_router_decision(
                '{"action":"reject","message":"no","confidence":0.1,'
                '"shell":"rm -rf /"}',
                {"telegram-control"},
            )

    def test_accepts_bounded_clarification(self):
        decision = parse_router_decision(
            '{"action":"clarify","question":"Which project?",'
            '"options":["telegram-control"],"confidence":0.4}',
            {"telegram-control"},
        )
        self.assertEqual(decision.question, "Which project?")
        self.assertEqual(decision.options, ("telegram-control",))

    def test_rejects_markdown_invalid_confidence_and_duplicate_options(self):
        with self.assertRaisesRegex(RouterContractError, "valid JSON"):
            parse_router_decision(
                '```json\n{"action":"reject"}\n```',
                {"telegram-control"},
            )
        with self.assertRaisesRegex(RouterContractError, "confidence"):
            parse_router_decision(
                '{"action":"reject","message":"no","confidence":2}',
                {"telegram-control"},
            )
        with self.assertRaisesRegex(RouterContractError, "clarification"):
            parse_router_decision(
                '{"action":"clarify","question":"Which?",'
                '"options":["telegram-control","telegram-control"],'
                '"confidence":0.3}',
                {"telegram-control"},
            )


if __name__ == "__main__":
    unittest.main()
