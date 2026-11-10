#!/usr/bin/env python3
"""Regression tests for finalize CLI input hardening (Phase 3 review fixes)."""
from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))

from common import count_summary_chars, read_entry, write_entry


class TestFinalizeInputs(unittest.TestCase):
    def setUp(self):
        self.scratch = _support.archive_project("p3c_finalize_", state=True)
        self.entry_dir = self.scratch / "library" / "tech" / "2026-10-27-input-test"
        self.entry_dir.mkdir(parents=True, exist_ok=True)
        self.entry_path = self.entry_dir / "entry.md"
        summary = "这是一段用于测试输入校验的中文摘要。" * 8
        fm = {
            "id": "2026-10-27-input-test",
            "title": "输入校验测试",
            "category": "tech",
            "context": "work",
            "status": "archived",
            "summary_chars": count_summary_chars(summary),
            "url": "https://example.com/input-test",
            "canonical_url": "https://example.com/input-test",
        }
        body = f"# 输入校验测试\n\n## 摘要\n\n{summary}\n\n## 全文\n\n正文内容。"
        write_entry(self.entry_path, fm, body)
        self.before = self.entry_path.read_bytes()

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(self.scratch / "scripts" / "finalize_entry.py"), *args],
            capture_output=True,
            text=True,
        )

    def test_path_outside_library_rejected(self):
        res = self._run("--path", "/etc/passwd", "--category", "tech")
        self.assertEqual(res.returncode, 1)
        self.assertIn("entry path not allowed", res.stdout)
        self.assertEqual(self.entry_path.read_bytes(), self.before)

    def test_summary_file_outside_allowlist_rejected(self):
        res = self._run("--path", str(self.entry_path), "--summary-file", "/etc/hostname")
        self.assertEqual(res.returncode, 1)
        self.assertIn("summary-file path not allowed", res.stdout)
        self.assertEqual(self.entry_path.read_bytes(), self.before)

    def test_subcategory_traversal_rejected(self):
        res = self._run("--path", str(self.entry_path), "--subcategory", "../../evil")
        self.assertEqual(res.returncode, 1)
        self.assertIn("invalid subcategory", res.stdout)
        self.assertEqual(self.entry_path.read_bytes(), self.before)
        self.assertTrue(self.entry_dir.exists())

    def test_archived_removes_summary_banner(self):
        fm, body = read_entry(self.entry_path)
        body = body.replace("## 摘要", "> ⚠️ **摘要基于部分抓取正文**，以下摘要可能不完整。\n\n## 摘要", 1)
        write_entry(self.entry_path, fm, body)
        res = self._run("--path", str(self.entry_path), "--status", "archived")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        _, saved = read_entry(self.entry_path)
        self.assertNotIn("摘要基于部分抓取", saved)


if __name__ == "__main__":
    unittest.main()
