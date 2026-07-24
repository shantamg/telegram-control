import unittest

from durable_store import ManagedProject
from router_contract import (
    RouterContractError,
    build_main_agent_prompt,
    build_router_prompt,
    parse_router_tool_call,
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

    def test_main_agent_prompt_exposes_tools_without_paths(self):
        prompt = build_main_agent_prompt(
            "create an agent for my project",
            [self.project],
            [
                {
                    "project_slug": "telegram-control",
                    "state": "registered",
                    "session": True,
                }
            ],
        )
        self.assertIn('"name":"create_project_agent"', prompt)
        self.assertIn('"project_slug":"telegram-control"', prompt)
        self.assertNotIn("/secret/local/path", prompt)

    def test_main_agent_prompt_exposes_alias_with_canonical_slug(self):
        prompt = build_main_agent_prompt(
            "inspect TC",
            [self.project],
            [],
            {"telegram-control": ["TC"]},
        )
        self.assertIn('"aliases":["TC"]', prompt)
        self.assertIn("canonical project slug", prompt)
        self.assertNotIn("/secret/local/path", prompt)

    def test_tool_call_normalizes_safe_dispatch(self):
        call = parse_router_tool_call(
            '{"tool":"send_to_agent","arguments":{'
            '"project_slug":"telegram-control","message":" fix it "}}',
            {"telegram-control"},
        )
        self.assertEqual(call.tool, "send_to_agent")
        self.assertEqual(call.arguments["message"], "fix it")
        self.assertFalse(call.requires_confirmation)

    def test_create_agent_tool_always_requires_confirmation(self):
        call = parse_router_tool_call(
            '{"tool":"create_project_agent","arguments":{'
            '"project":"~/Code/new-project","topic_name":"New Project"}}',
            {"telegram-control"},
        )
        self.assertTrue(call.requires_confirmation)
        self.assertEqual(call.arguments["project"], "~/Code/new-project")
        self.assertIsNone(call.arguments["provider"])
        self.assertIsNone(call.arguments["model"])
        self.assertIsNone(call.arguments["effort"])

        claude = parse_router_tool_call(
            '{"tool":"create_project_agent","arguments":{'
            '"project":"/tmp/new-project","topic_name":"New Project",'
            '"provider":"claude","model":"sonnet","effort":"high"}}',
            {"telegram-control"},
        )
        self.assertEqual(claude.arguments["provider"], "claude")
        self.assertEqual(claude.arguments["model"], "sonnet")
        self.assertEqual(claude.arguments["effort"], "high")
        with self.assertRaisesRegex(RouterContractError, "provider"):
            parse_router_tool_call(
                '{"tool":"create_project_agent","arguments":{'
                '"project":"/tmp/new-project","topic_name":null,'
                '"provider":"invented"}}',
                {"telegram-control"},
            )

    def test_configure_agent_tool_supports_patch_and_reset(self):
        configured = parse_router_tool_call(
            '{"tool":"configure_agent","arguments":{'
            '"project_slug":"telegram-control","model":"gpt-5.6-sol",'
            '"effort":"high"}}',
            {"telegram-control"},
        )
        self.assertEqual(configured.arguments["model"], "gpt-5.6-sol")
        self.assertEqual(configured.arguments["effort"], "high")
        self.assertFalse(configured.requires_confirmation)

        reset = parse_router_tool_call(
            '{"tool":"configure_agent","arguments":{'
            '"project_slug":"telegram-control","effort":null}}',
            {"telegram-control"},
        )
        self.assertEqual(
            reset.arguments,
            {"project_slug": "telegram-control", "effort": None},
        )

    def test_alias_tools_are_strict_and_alias_dispatch_is_canonicalized(self):
        call = parse_router_tool_call(
            '{"tool":"set_project_alias","arguments":{'
            '"project_slug":"telegram-control","alias":"TC"}}',
            {"telegram-control"},
        )
        self.assertEqual(call.arguments["alias"], "TC")
        self.assertFalse(call.requires_confirmation)

        dispatch = parse_router_tool_call(
            '{"tool":"send_to_agent","arguments":{'
            '"project_slug":"TC","message":"inspect it"}}',
            {"telegram-control"},
            {"tc": "telegram-control"},
        )
        self.assertEqual(dispatch.arguments["project_slug"], "telegram-control")

        removal = parse_router_tool_call(
            '{"tool":"remove_project_alias","arguments":{"alias":"TC"}}',
            {"telegram-control"},
        )
        self.assertEqual(removal.arguments, {"alias": "TC"})

    def test_tool_call_rejects_unknown_tools_projects_and_fields(self):
        with self.assertRaisesRegex(RouterContractError, "unknown tool"):
            parse_router_tool_call(
                '{"tool":"run_shell","arguments":{"command":"rm -rf /"}}',
                {"telegram-control"},
            )
        with self.assertRaisesRegex(RouterContractError, "unknown project"):
            parse_router_tool_call(
                '{"tool":"send_to_agent","arguments":{'
                '"project_slug":"invented","message":"task"}}',
                {"telegram-control"},
            )
        with self.assertRaisesRegex(RouterContractError, "arguments"):
            parse_router_tool_call(
                '{"tool":"list_projects","arguments":{"path":"/"}}',
                {"telegram-control"},
            )

    def test_ask_user_tool_bounds_button_options(self):
        call = parse_router_tool_call(
            '{"tool":"ask_user","arguments":{'
            '"question":"Which project?","options":["One","Two"]}}',
            {"telegram-control"},
        )
        self.assertEqual(call.arguments["options"], ["One", "Two"])
        with self.assertRaisesRegex(RouterContractError, "options"):
            parse_router_tool_call(
                '{"tool":"ask_user","arguments":{'
                '"question":"Which?","options":["1","2","3","4","5"]}}',
                {"telegram-control"},
            )


if __name__ == "__main__":
    unittest.main()
