import unittest

import router_eval


class RouterEvaluationTests(unittest.TestCase):
    def test_fixture_suite_is_complete_and_contract_valid(self):
        cases = router_eval.load_cases(router_eval.DEFAULT_FIXTURES)
        self.assertEqual(
            {case.expected_tool for case in cases},
            {
                "list_projects",
                "inspect_project",
                "send_to_agent",
                "create_project_agent",
                "bind_forum_workspace",
                "rename_topic",
                "configure_agent",
                "set_project_alias",
                "remove_project_alias",
                "ask_user",
                "respond",
                "find_directory",
                "inspect_directory",
            },
        )
        for case in cases:
            result = router_eval.evaluate_output(case, case.sample_output)
            self.assertTrue(result["passed"], case.name)
            self.assertIsNone(result["parse_error"])

    def test_benchmark_prompt_never_exposes_fixture_path(self):
        prompt = router_eval.router_prompt("Inspect telegram-control")
        self.assertIn('"slug":"telegram-control"', prompt)
        self.assertNotIn("/path/that/must/not/appear", prompt)


if __name__ == "__main__":
    unittest.main()
