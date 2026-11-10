#!/usr/bin/env python3
"""Regression tests for fence-aware H1 handling (Phase 2.1)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from common import atx_h1_lines
from cleanup_h1 import cleanup_entry_h1


class TestFenceAwareH1(unittest.TestCase):
    def test_code_comment_is_not_h1(self):
        body = (
            "# 标题\n\n## 全文\n\n正文\n\n"
            "```python\n# 这是代码注释，不是标题\nprint(1)\n```\n"
        )
        self.assertEqual(atx_h1_lines(body), [(0, "标题")])
        cleaned, removed = cleanup_entry_h1(body)
        self.assertEqual(removed, [])
        self.assertEqual(cleaned, body)

    def test_tilde_fence_and_duplicate_outside(self):
        body = (
            "# 标题\n\n## 全文\n\n正文\n\n"
            "~~~bash\n# 注释\n~~~\n\n"
            "# 第二个标题\n\n结尾\n"
        )
        headings = atx_h1_lines(body)
        self.assertEqual([text for _, text in headings], ["标题", "第二个标题"])
        cleaned, removed = cleanup_entry_h1(body)
        self.assertEqual(removed, ["# 第二个标题"])
        self.assertIn("# 注释", cleaned)
        self.assertNotIn("# 第二个标题", cleaned)

    def test_blank_lines_not_collapsed(self):
        body = "# 标题\n\n\n\n## 全文\n\n正文\n\n# 第二个标题\n\n结尾\n"
        cleaned, removed = cleanup_entry_h1(body)
        self.assertEqual(removed, ["# 第二个标题"])
        self.assertIn("\n\n\n", cleaned)

    def test_atx_closing_hashes(self):
        self.assertEqual(atx_h1_lines("# 标题 ###\n"), [(0, "标题")])


if __name__ == "__main__":
    unittest.main()
