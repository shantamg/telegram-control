import unittest

import telegram_formatting


class TelegramFormattingTests(unittest.TestCase):
    def test_provider_markdown_compiles_to_explicit_entities(self):
        chunks = telegram_formatting.render_markdown_chunks(
            "# Result\n\n**Bold** and *italic* with `code`.\n"
            "- first\n"
            "> quoted\n"
            "```python\nprint('ok')\n```\n",
            speaker="Project",
        )

        self.assertEqual(len(chunks), 1)
        chunk = chunks[0]
        self.assertEqual(
            chunk.text,
            "Project\n\nResult\n\nBold and italic with code.\n"
            "• first\nquoted\nprint('ok')\n",
        )
        self.assertNotIn("parse_mode", chunk.__dict__)
        self.assertEqual(
            [entity["type"] for entity in chunk.entities],
            [
                "bold",
                "bold",
                "bold",
                "italic",
                "code",
                "blockquote",
                "pre",
            ],
        )
        self.assertEqual(chunk.entities[-1]["language"], "python")

    def test_entity_offsets_use_utf16_code_units(self):
        chunk = telegram_formatting.render_markdown_chunks(
            "😀 **bold**",
            speaker="🎛 Control",
        )[0]

        self.assertEqual(
            chunk.entities,
            (
                {"type": "bold", "offset": 0, "length": 10},
                {"type": "bold", "offset": 15, "length": 4},
            ),
        )

    def test_ambiguous_identifier_underscores_remain_literal(self):
        chunk = telegram_formatting.render_markdown_chunks(
            "Use snake_case and project_slug."
        )[0]

        self.assertEqual(chunk.text, "Use snake_case and project_slug.")
        self.assertEqual(chunk.entities, ())

    def test_links_tasks_strikethrough_and_dividers_use_safe_subset(self):
        chunk = telegram_formatting.render_markdown_chunks(
            "[Docs](https://example.com/docs) and ~~old~~\n"
            "- [x] shipped\n"
            "- [ ] pending\n"
            "---\n"
            "[unsafe](javascript:alert(1))"
        )[0]

        self.assertEqual(
            chunk.text,
            "Docs and old\n☑ shipped\n☐ pending\n────────\n"
            "[unsafe](javascript:alert(1))",
        )
        self.assertEqual(
            [entity["type"] for entity in chunk.entities],
            ["text_link", "strikethrough"],
        )
        self.assertEqual(
            chunk.entities[0]["url"],
            "https://example.com/docs",
        )

    def test_unclosed_fence_falls_back_to_exact_plain_source(self):
        source = "Before\n```python\nprint('unfinished')"

        chunk = telegram_formatting.render_markdown_chunks(source)[0]

        self.assertEqual(chunk.text, source)
        self.assertEqual(chunk.entities, ())
        self.assertTrue(chunk.used_plain_fallback)

    def test_chunking_rebases_and_splits_entities_safely(self):
        chunks = telegram_formatting.render_markdown_chunks(
            "**" + ("word " * 900).rstrip() + "**",
            speaker="Agent",
            limit=1100,
        )

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.text), 1100)
            self.assertEqual(chunk.entities[0]["type"], "bold")
            self.assertEqual(chunk.entities[0]["offset"], 0)
            body_entities = [
                entity
                for entity in chunk.entities
                if entity["offset"] != 0
            ]
            self.assertEqual(len(body_entities), 1)
            self.assertEqual(body_entities[0]["type"], "bold")
            self.assertLessEqual(
                body_entities[0]["offset"] + body_entities[0]["length"],
                len(chunk.text),
            )

    def test_chunk_limit_counts_non_bmp_text_as_utf16(self):
        chunks = telegram_formatting.render_markdown_chunks(
            "😀" * 1200,
            limit=1100,
        )

        self.assertEqual(len(chunks), 3)
        for chunk in chunks:
            self.assertLessEqual(
                len(chunk.text.encode("utf-16-le")) // 2,
                1100,
            )

    def test_controller_html_escapes_dynamic_text(self):
        rendered = telegram_formatting.controller_html_title(
            "Workspace <one>",
            "Use A & B",
        )

        self.assertEqual(
            rendered,
            "<b>Workspace &lt;one&gt;</b>\n\nUse A &amp; B",
        )

    def test_plain_controller_text_only_formats_the_speaker(self):
        chunk = telegram_formatting.render_plain_chunks(
            "Literal **asterisks** stay literal.",
            speaker="🎛 Control",
        )[0]

        self.assertEqual(
            chunk.text,
            "🎛 Control\n\nLiteral **asterisks** stay literal.",
        )
        self.assertEqual(
            chunk.entities,
            ({"type": "bold", "offset": 0, "length": 10},),
        )

    def test_controller_html_chunking_keeps_tags_balanced(self):
        chunks = telegram_formatting.render_controller_html_chunks(
            "<b>" + ("safe " * 900) + "</b>",
            speaker="🎛 Control",
            limit=1100,
        )

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 1100)
            self.assertTrue(chunk.startswith("<b>🎛 Control</b>\n\n<b>"))
            self.assertTrue(chunk.endswith("</b>"))


if __name__ == "__main__":
    unittest.main()
