#!/usr/bin/env python3
"""Manual path independence from automatic selection (repair contract D).

A deliberate user save must remain available even when the automatic policy or
ledger is missing, disabled, invalid or corrupt. Synthetic only.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_subscription_selection as fx  # noqa: E402

sel = fx.sel


class ManualPathTests(fx.ProjectCase):
    def add(self, root: Path, url: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [fx.PY, str(root / "scripts" / "archive_url.py"), url, *args],
            capture_output=True, text=True, cwd=str(root), timeout=120,
        )

    def test_invalid_auto_policy_does_not_block_a_manual_duplicate(self):
        url = "https://example.com/manual-existing"
        root = self.project("schema: malformed\nenabled: true\n",
                            seen={url: {"id": "synthetic-existing", "path": ""}})
        result = self.add(root, url, "--collection", "manual")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "duplicate")

    def test_invalid_policy_still_blocks_automatic_add(self):
        url = "https://example.com/auto-blocked"
        root = self.project("schema: malformed\nenabled: true\n")
        result = self.add(root, url, "--collection", "auto")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("policy", payload["error"].lower())
        self.assertEqual(list((root / "library").rglob("entry.md")), [])

    def test_manual_add_reports_the_automation_warning_without_repairing_the_ledger(self):
        url = "https://example.com/manual-warning"
        root = self.project(fx.base_policy())
        ledger = root / ".state" / "selection" / "ledger.json"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text("{broken", encoding="utf-8")
        content = root / ".state" / "manual.md"
        content.write_text(
            "合成正文：用于验证手动保存不受选题账本损坏影响。"
            "这是一段完全虚构的合成文章正文。\n",
            encoding="utf-8",
        )
        result = self.add(
            root, url, "--collection", "manual", "--content-file", str(content),
            "--original-title", "Manual synthetic item",
            "--published", "2026-11-10", "--language", "en", "--category", "tech",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("automation", (result.stderr + result.stdout).lower())
        self.assertEqual(ledger.read_text(encoding="utf-8"), "{broken",
                         "a manual save must not repair or reset a corrupt ledger")
        self.assertEqual(len(list((root / "library").rglob("entry.md"))), 1)

    def test_manual_save_closes_an_undecided_automatic_record(self):
        url = "https://example.com/manual-undecided"
        root = self.project(fx.base_policy())
        cand = fx.candidate(url)
        self.register(root, [cand])
        content = root / ".state" / "undecided.md"
        content.write_text(
            "合成正文：用于验证手动保存关闭未决自动队列。完全虚构。\n",
            encoding="utf-8",
        )
        result = self.add(
            root, url, "--collection", "manual", "--content-file", str(content),
            "--original-title", "Undecided synthetic item",
            "--published", "2026-11-10", "--language", "en", "--category", "tech",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.ledger(root)["records"][sel.identity_for_url(url)]
        self.assertEqual(record["state"], sel.STATE_ARCHIVED)
        self.assertEqual(record["outcome"], "")
        actions = [h.get("action") for h in record.get("history") or []]
        self.assertIn("manual_save", actions)


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
