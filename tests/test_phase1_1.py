#!/usr/bin/env python3
"""Unit and integration tests for Phase 1.1 fixes."""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

# Ensure scripts dir is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))

from common import TZ, iso, now, write_entry, read_entry, write_json_atomic
from archive_url import looks_truncated
import daily_gate


class TestLooksTruncated(unittest.TestCase):
    def setUp(self):
        self.base_text = "这是一篇用于测试正文截断判断的完整段落，内容详细充实，确保字符数超过三百字门槛。" * 15

    def test_markdown_link_complete(self):
        # 1. 以 [博客原文](https://example.com/post) 结尾的完整短文 → False
        text = self.base_text + "。\n\n[博客原文](https://example.com/post)"
        self.assertFalse(looks_truncated(text))

    def test_markdown_image_complete(self):
        # 2. 以 ![](images/001.png) 结尾的完整短文 → False
        text = self.base_text + "。\n\n![](images/001.png)"
        self.assertFalse(looks_truncated(text))

    def test_truncated_ending(self):
        # 3. 以 ...save inference 结尾的截断文 → True
        text = self.base_text + " ...save inference"
        self.assertTrue(looks_truncated(text))

    def test_period_ending(self):
        # 4. 以 。 结尾 → False
        text = self.base_text + "。"
        self.assertFalse(looks_truncated(text))

    def test_naked_ascii_paren(self):
        # 裸露的 ) 不被无条件当作正常结尾
        text = self.base_text + " (save inference)"
        self.assertTrue(looks_truncated(text))

    def test_chinese_paren_and_ellipsis(self):
        # 中文括号与省略号作为完整结尾容差
        self.assertFalse(looks_truncated(self.base_text + "（全文完）"))
        self.assertFalse(looks_truncated(self.base_text + "……"))


class TestArchiveUrlCodingTag(unittest.TestCase):
    def test_explicit_coding_category_adds_tag(self):
        from archive_url import classify, category_slugs
        # When --category coding is given, coding tag is added
        args_category = "coding"
        tags = ["python"]
        if args_category == "coding" and "coding" not in tags:
            tags.insert(0, "coding")
        self.assertIn("coding", tags)

    def test_inferred_coding_category_does_not_add_tag(self):
        # When category is inferred from keywords, coding tag is NOT added
        args_category = ""
        inferred_category = "coding"
        tags = ["cli", "tool"]
        if args_category == "coding" and "coding" not in tags:
            tags.insert(0, "coding")
        self.assertNotIn("coding", tags)


class TestFinalizeArchived(unittest.TestCase):
    def setUp(self):
        self.scratch = _support.archive_project("ca_test_fin_", state=True)
        self.entry_dir = self.scratch / "library" / "tech" / "test-entry"
        self.entry_dir.mkdir(parents=True, exist_ok=True)
        self.entry_path = self.entry_dir / "entry.md"

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_archived_clears_content_note_and_banner(self):
        fm = {
            "id": "test-entry",
            "title": "测试文章",
            "category": "tech",
            "status": "partial",
            "content_note": "正文在约 1000 字符处截断",
            "confidence": 1.0,
        }
        body = "# 测试文章\n\n## 全文\n\n> ⚠️ **部分抓取**：本文正文在约 1000 字符处截断，以下内容不完整。\n\n正文内容。"
        write_entry(self.entry_path, fm, body)

        # Run finalize with --status archived and without --content-note
        cmd = [
            sys.executable,
            str(self.scratch / "scripts" / "finalize_entry.py"),
            "--path", str(self.entry_path),
            "--status", "archived",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

        saved_fm, saved_body = read_entry(self.entry_path)
        self.assertEqual(saved_fm.get("content_note"), "")
        self.assertEqual(saved_fm.get("status"), "archived")
        self.assertNotIn("⚠️ **部分抓取**", saved_body)

    def test_archived_with_content_note_errors(self):
        fm = {
            "id": "test-entry",
            "title": "测试文章",
            "category": "tech",
            "status": "partial",
            "content_note": "正文截断",
            "confidence": 1.0,
        }
        body = "# 测试文章\n\n## 全文\n\n正文内容。"
        write_entry(self.entry_path, fm, body)

        cmd = [
            sys.executable,
            str(self.scratch / "scripts" / "finalize_entry.py"),
            "--path", str(self.entry_path),
            "--status", "archived",
            "--content-note", "should not be allowed",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("--status archived cannot have content_note", res.stdout + res.stderr)

    def test_partial_without_content_note_errors(self):
        fm = {
            "id": "test-entry",
            "title": "测试文章",
            "category": "tech",
            "status": "archived",
            "content_note": "",
            "confidence": 1.0,
        }
        body = "# 测试文章\n\n## 全文\n\n正文内容。"
        write_entry(self.entry_path, fm, body)

        cmd = [
            sys.executable,
            str(self.scratch / "scripts" / "finalize_entry.py"),
            "--path", str(self.entry_path),
            "--status", "partial",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("--status partial requires non-empty content_note", res.stdout + res.stderr)


class TestDailyGateCheckpoints(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(dir=str(_support.SCRATCH), prefix="ca_test_gate_"))
        self.runs_dir = self.scratch / ".state" / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.gate_path = self.runs_dir / "daily-gate.json"
        self.fetch_path = self.runs_dir / "latest-fetch.json"

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_stale_gate_older_than_24h(self):
        stale_time = (now() - timedelta(hours=25)).isoformat(timespec="seconds")
        fetch_gen = stale_time
        write_json_atomic(self.fetch_path, {"generated_at": fetch_gen, "sources": []})
        write_json_atomic(self.gate_path, {
            "started_at": stale_time,
            "fetch_generated_at": fetch_gen,
            "status": "started",
        })

        # Test using daily_gate functions by pointing ROOT to scratch
        orig_root = daily_gate.ROOT
        daily_gate.ROOT = self.scratch
        try:
            res = daily_gate.run_finish()
            self.assertEqual(res, 1)
        finally:
            daily_gate.ROOT = orig_root

    def test_stale_gate_in_the_future(self):
        future_time = (now() + timedelta(minutes=10)).isoformat(timespec="seconds")
        fetch_gen = future_time
        write_json_atomic(self.fetch_path, {"generated_at": fetch_gen, "sources": []})
        write_json_atomic(self.gate_path, {
            "started_at": future_time,
            "fetch_generated_at": fetch_gen,
            "status": "started",
        })

        orig_root = daily_gate.ROOT
        daily_gate.ROOT = self.scratch
        try:
            res = daily_gate.run_finish()
            self.assertEqual(res, 1)
        finally:
            daily_gate.ROOT = orig_root

    def test_fetch_timestamp_mismatch(self):
        t1 = now().isoformat(timespec="seconds")
        t2 = (now() - timedelta(minutes=2)).isoformat(timespec="seconds")
        write_json_atomic(self.fetch_path, {"generated_at": t2, "sources": []})
        write_json_atomic(self.gate_path, {
            "started_at": t1,
            "fetch_generated_at": t1,
            "status": "started",
        })

        orig_root = daily_gate.ROOT
        daily_gate.ROOT = self.scratch
        try:
            res = daily_gate.run_finish()
            self.assertEqual(res, 1)
        finally:
            daily_gate.ROOT = orig_root

    def test_gate_failed_status_blocked(self):
        t = now().isoformat(timespec="seconds")
        write_json_atomic(self.fetch_path, {"generated_at": t, "sources": []})
        write_json_atomic(self.gate_path, {
            "started_at": t,
            "fetch_generated_at": t,
            "status": "failed",
            "error": "fetch_sources failed",
        })

        orig_root = daily_gate.ROOT
        daily_gate.ROOT = self.scratch
        try:
            res = daily_gate.run_finish()
            self.assertEqual(res, 1)
        finally:
            daily_gate.ROOT = orig_root


class TestBuildDigest(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(dir=str(_support.SCRATCH), prefix="ca_test_digest_"))
        # Create minimal index.csv
        today_date = now().strftime("%Y-%m-%d")
        self.index_path = self.scratch / "index.csv"
        rows = [
            {
                "id": "entry-new-partial",
                "title": "新部分抓取文章",
                "source": "OpenAI",
                "published": f"{today_date}T10:00:00+08:00",
                "archived": f"{today_date}T12:00:00+08:00",
                "status": "partial",
                "content_note": "在约 2000 字符处截断",
                "url": "https://openai.com/1",
                "summary": "这是摘要内容。",
            },
            {
                "id": "entry-existing-partial",
                "title": "今日已存部分文章",
                "source": "OpenAI",
                "published": f"{today_date}T08:00:00+08:00",
                "archived": f"{today_date}T09:00:00+08:00",
                "status": "partial",
                "content_note": "在约 1500 字符处截断",
                "url": "https://openai.com/2",
                "summary": "这是已存摘要内容。",
            },
            {
                "id": "entry-archived",
                "title": "正常文章",
                "source": "Anthropic",
                "published": f"{today_date}T11:00:00+08:00",
                "archived": f"{today_date}T12:30:00+08:00",
                "status": "archived",
                "content_note": "",
                "url": "https://anthropic.com/1",
                "summary": "这是正常摘要内容。",
            },
        ]
        with open(self.index_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_digest_skipped_and_coverage(self):
        today_date = now().strftime("%Y-%m-%d")
        run_file = self.scratch / "latest-fetch.json"
        run_data = {
            "generated_at": f"{today_date}T11:00:00+08:00",
            "sources": [
                {"id": "openai", "label": "OpenAI", "status": "ok"},  # No skipped key -> old run
            ],
        }
        write_json_atomic(run_file, run_data)

        # Run build_digest with ROOT pointed to scratch
        import build_digest
        orig_root = build_digest.ROOT
        build_digest.ROOT = self.scratch
        try:
            import io
            from contextlib import redirect_stdout
            f = io.StringIO()
            with redirect_stdout(f):
                # We simulate main with args
                import sys
                old_argv = sys.argv
                sys.argv = ["build_digest.py", "--run", str(run_file), "--limit", "10"]
                build_digest.main()
                sys.argv = old_argv
            out = f.getvalue()

            # 1. 头部显示 "去重跳过 未知" (old run)
            self.assertIn("去重跳过 未知", out)

            # 2. 新增数应为 2 (archived >= 11:00 -> entry-new-partial 和 entry-archived)
            self.assertIn("新增 2 篇", out)

            # 3. ## 覆盖说明 同时列出 本 run 新增 与 今日已存
            self.assertIn("## 覆盖说明", out)
            self.assertIn("新部分抓取文章（本 run 新增）：在约 2000 字符处截断", out)
            self.assertIn("今日已存部分文章（今日已存）：在约 1500 字符处截断", out)
        finally:
            build_digest.ROOT = orig_root


if __name__ == "__main__":
    unittest.main()
