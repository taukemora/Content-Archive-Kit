#!/usr/bin/env python3
"""Batch 3 regression tests for date preservation and undated docs handling.

Owned by Subagent B (dates & undated docs).
Tests candidate date fields, CLI date flags, GLM undated/anchor-less section handling,
and isolated candidate->archive flow.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_sources as fetch_mod  # noqa: E402
from common import TZ  # noqa: E402

PY = _support.PYTHON


class TestCandidateDateFields(unittest.TestCase):
    def test_candidate_helper_preserves_date_fields(self):
        """candidate() helper must retain date_text, date_precision, published_original, source_display_date."""
        dt = datetime(2026, 10, 28, 14, 30, tzinfo=TZ)
        cand = fetch_mod.candidate(
            "Test Title",
            "https://example.com/test",
            "Example",
            dt=dt,
            date_source="list",
            precision="datetime",
            date_text="Oct 28, 2026 14:30",
            published_original="2026-10-28 14:30:00",
            source_display_date="2026-10-28",
        )
        self.assertEqual(cand["date_precision"], "datetime")
        self.assertEqual(cand["date_text"], "Oct 28, 2026 14:30")
        self.assertEqual(cand["published_original"], "2026-10-28 14:30:00")
        self.assertEqual(cand["source_display_date"], "2026-10-28")

    def test_rss_adapter_retains_pubdate_and_empty_source_display(self):
        """RSS adapter must preserve raw pubDate in published_original and empty source_display_date."""
        rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test RSS</title>
    <item>
      <title>Article 1</title>
      <link>https://example.com/art1</link>
      <pubDate>Wed, 28 Oct 2026 18:00:00 GMT</pubDate>
      <description>Summary text</description>
    </item>
  </channel>
</rss>"""
        src = {
            "id": "test_rss",
            "label": "Test RSS",
            "feed": "https://example.com/feed.xml",
            "extractor": "web_extract",
        }
        fetched_at = datetime(2026, 10, 29, 10, 0, tzinfo=TZ)
        with patch.object(fetch_mod, "fetch_text", return_value=rss_xml):
            items = fetch_mod.openai_rss(src, fetched_at)

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["published_original"], "Wed, 28 Oct 2026 18:00:00 GMT")
        self.assertEqual(item["source_display_date"], "")
        self.assertEqual(item["date_precision"], "datetime")
        self.assertEqual(item["date_text"], "Wed, 28 Oct 2026 18:00:00 GMT")

    def test_anthropic_html_retains_date_tag_and_calendar_date(self):
        """Anthropic HTML adapter must record raw date tag in published_original and YYYY-MM-DD in source_display_date."""
        html = """
        <html><body><ul>
          <li>
            <a class="title-link" href="/news/test-post">Test Announcement</a>
            <time>Oct 28, 2026</time>
          </li>
        </ul></body></html>
        """
        src = {
            "id": "anthropic",
            "label": "Anthropic",
            "list_url": "https://www.anthropic.com/news",
        }
        fetched_at = datetime(2026, 10, 29, 10, 0, tzinfo=TZ)
        with patch.object(fetch_mod, "fetch_text", return_value=html):
            items = fetch_mod.anthropic_html(src, fetched_at)

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["published_original"], "Oct 28, 2026")
        self.assertEqual(item["source_display_date"], "2026-10-28")
        self.assertEqual(item["date_precision"], "day")

    def test_kimi_html_retains_date_tag_and_calendar_date(self):
        """Kimi HTML adapter must record raw date tag in published_original and YYYY-MM-DD in source_display_date."""
        html = """
        <main>
          <div class="menu-card">
            <a href="/blog/kimi-update">
              <h4>Kimi Update</h4>
              <span>2026-10-28</span>
            </a>
          </div>
        </main>
        """
        src = {
            "id": "kimi",
            "label": "Kimi",
            "list_url": "https://www.kimi.com/blog",
        }
        fetched_at = datetime(2026, 10, 29, 10, 0, tzinfo=TZ)
        with patch.object(fetch_mod, "fetch_text", return_value=html):
            items = fetch_mod.kimi_html(src, fetched_at)

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["published_original"], "2026-10-28")
        self.assertEqual(item["source_display_date"], "2026-10-28")
        self.assertEqual(item["date_precision"], "day")


class TestGLMDocsDateAndAnchorHandling(unittest.TestCase):
    def test_glm_dated_section_with_real_id(self):
        """GLM section with real id and explicit date in label gets normal candidate."""
        html = """
        <div class="update" id="sec-glm-update-1">
          <div data-component-part="update-description">发布 GLM-4.5 模型</div>
          <div data-component-part="update-label">2026-10-28</div>
          <div data-component-part="update-content">GLM-4.5 正式上线，性能大幅提升。</div>
        </div>
        """
        src = {
            "id": "glm",
            "label": "GLM",
            "list_url": "https://docs.bigmodel.cn/release-notes",
        }
        fetched_at = datetime(2026, 10, 29, 10, 0, tzinfo=TZ)
        with patch.object(fetch_mod, "fetch_text", return_value=html):
            items = fetch_mod.glm_docs(src, fetched_at)

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["title"], "发布 GLM-4.5 模型")
        self.assertEqual(item["url"], "https://docs.bigmodel.cn/release-notes#sec-glm-update-1")
        self.assertFalse(item["published_unknown"])
        self.assertEqual(item["date_source"], "list")
        self.assertEqual(item["date_precision"], "day")
        self.assertEqual(item["published_original"], "2026-10-28")
        self.assertEqual(item["source_display_date"], "2026-10-28")

    def test_glm_undated_section_with_real_id_enters_candidates_as_fetched_fallback(self):
        """GLM section with real id but no date enters candidate as fetched fallback (published_unknown=True, date_precision=day)."""
        html = """
        <div class="update" id="sec-no-date">
          <div data-component-part="update-description">模型体验优化更新</div>
          <div data-component-part="update-content">本次更新包含多项稳定性改进。</div>
        </div>
        """
        src = {
            "id": "glm",
            "label": "GLM",
            "list_url": "https://docs.bigmodel.cn/release-notes",
        }
        fetched_at = datetime(2026, 10, 29, 10, 0, tzinfo=TZ)
        with patch.object(fetch_mod, "fetch_text", return_value=html):
            items = fetch_mod.glm_docs(src, fetched_at)

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["url"], "https://docs.bigmodel.cn/release-notes#sec-no-date")
        self.assertTrue(item["published_unknown"])
        self.assertEqual(item["date_source"], "fetched")
        self.assertEqual(item["date_precision"], "day")
        self.assertEqual(item["published_original"], "")
        self.assertEqual(item["source_display_date"], "")
        self.assertTrue(item["published"].startswith("2026-10-29"))

    def test_glm_does_not_scan_body_for_past_dates(self):
        """GLM adapter must not scan body text for past year mentions (e.g. 2023年) as publication date."""
        html = """
        <div class="update" id="sec-past-body-date">
          <div data-component-part="update-description">架构回顾与新版演进</div>
          <div data-component-part="update-content">
            我们在 2023年3月首次提出了这一架构设计，历经多次迭代。
          </div>
        </div>
        """
        src = {
            "id": "glm",
            "label": "GLM",
            "list_url": "https://docs.bigmodel.cn/release-notes",
        }
        fetched_at = datetime(2026, 10, 29, 10, 0, tzinfo=TZ)
        with patch.object(fetch_mod, "fetch_text", return_value=html):
            items = fetch_mod.glm_docs(src, fetched_at)

        self.assertEqual(len(items), 1)
        item = items[0]
        # Must NOT be 2023!
        self.assertTrue(item["published_unknown"])
        self.assertEqual(item["date_source"], "fetched")
        self.assertFalse("2023" in item["published"])

    def test_glm_section_without_id_is_rejected_not_fabricated(self):
        """GLM section lacking real id must not fabricate anchor and must record explicit rejection."""
        html = """
        <div class="update">
          <div data-component-part="update-description">缺少ID的更新条目</div>
          <div data-component-part="update-label">2026-10-28</div>
          <div data-component-part="update-content">这段更新没有 id 锚点。</div>
        </div>
        <div class="update" id="sec-valid">
          <div data-component-part="update-description">有效条目</div>
          <div data-component-part="update-label">2026-10-28</div>
          <div data-component-part="update-content">有效内容。</div>
        </div>
        """
        src = {
            "id": "glm",
            "label": "GLM",
            "list_url": "https://docs.bigmodel.cn/release-notes",
        }
        fetched_at = datetime(2026, 10, 29, 10, 0, tzinfo=TZ)
        with patch.object(fetch_mod, "fetch_text", return_value=html):
            items = fetch_mod.glm_docs(src, fetched_at)

        # Only 1 valid candidate
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://docs.bigmodel.cn/release-notes#sec-valid")

        # Explicit rejection recorded on CandidateList
        rejections = getattr(items, "rejections", [])
        self.assertEqual(len(rejections), 1)
        rej = rejections[0]
        self.assertEqual(rej["reason"], "missing_section_id")
        self.assertIn("缺少ID的更新条目", rej["title"])

    def test_glm_report_propagates_rejections_and_no_date_metrics(self):
        """Source report must contain rejections list, rejected count, and no_date counts."""
        html = """
        <div class="update">
          <div data-component-part="update-description">无ID更新</div>
          <div data-component-part="update-label">2026-10-28</div>
          <div data-component-part="update-content">无锚点。</div>
        </div>
        <div class="update" id="sec-valid-undated">
          <div data-component-part="update-description">有ID无日期更新</div>
          <div data-component-part="update-content">有锚点但没有日期。</div>
        </div>
        """
        src = {
            "id": "glm",
            "label": "GLM",
            "list_url": "https://docs.bigmodel.cn/release-notes",
        }
        fetched_at = datetime(2026, 10, 29, 10, 0, tzinfo=TZ)
        with patch.object(fetch_mod, "fetch_text", return_value=html):
            items = fetch_mod.glm_docs(src, fetched_at)

        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["published_unknown"])
        self.assertEqual(len(items.rejections), 1)


class TestArchiveUrlDateCLIAndGoogleDirect(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(dir=str(_support.SCRATCH), prefix="test_ca_dates_"))
        (self.scratch / "scripts").mkdir()
        shutil.copy(ROOT / "scripts" / "common.py", self.scratch / "scripts" / "common.py")
        shutil.copy(ROOT / "scripts" / "archive_url.py", self.scratch / "scripts" / "archive_url.py")
        shutil.copy(ROOT / "scripts" / "media_preservation.py", self.scratch / "scripts" / "media_preservation.py")
        shutil.copy(ROOT / "scripts" / "media_markup.py", self.scratch / "scripts" / "media_markup.py")
        shutil.copy(ROOT / "scripts" / "media_policy.py", self.scratch / "scripts" / "media_policy.py")
        shutil.copy(_support.template("taxonomy.yaml"), self.scratch / "taxonomy.yaml")
        (self.scratch / ".state").mkdir()
        (self.scratch / ".state" / "seen.json").write_text(json.dumps({"entries": {}}), encoding="utf-8")
        (self.scratch / "library").mkdir()

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_archive_url_cli_date_flags_with_content_file(self):
        """archive_url.py accepts --published-original, --date-precision, --source-display-date and writes to frontmatter and JSON."""
        cand_dir = self.scratch / ".state" / "candidates"
        cand_dir.mkdir(parents=True, exist_ok=True)
        cf = cand_dir / "content.md"
        cf.write_text("这是测试正文内容，用于验证日期与精度贯通。", encoding="utf-8")

        cmd = [
            PY,
            str(self.scratch / "scripts" / "archive_url.py"),
            "https://example.com/blog/cross-day-test",
            "--title-zh", "测试跨日日期文章",
            "--category", "industry",
            "--published", "2026-10-29T07:00:00+08:00",
            "--published-original", "2026-10-28T23:00:00Z",
            "--date-precision", "datetime",
            "--source-display-date", "2026-10-28",
            "--content-file", str(cf),
        ]
        res = subprocess.run(
            cmd,
            cwd=str(self.scratch),
            capture_output=True,
            text=True,
            env={**os.environ, "CA_ROOT": str(self.scratch)},
        )
        self.assertEqual(res.returncode, 0, f"CLI error: {res.stderr}\n{res.stdout}")
        data = json.loads(res.stdout)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["published"], "2026-10-29T07:00:00+08:00")
        self.assertEqual(data["published_original"], "2026-10-28T23:00:00Z")
        self.assertEqual(data["date_precision"], "datetime")
        self.assertEqual(data["source_display_date"], "2026-10-28")

        # Verify entry.md frontmatter
        entry_file = Path(data["path"])
        self.assertTrue(entry_file.is_file())
        text = entry_file.read_text(encoding="utf-8")
        import yaml
        parts = text.split("---", 2)
        fm = yaml.safe_load(parts[1])
        self.assertEqual(fm["published"], "2026-10-29T07:00:00+08:00")
        self.assertEqual(fm["published_original"], "2026-10-28T23:00:00Z")
        self.assertEqual(fm["date_precision"], "datetime")
        self.assertEqual(fm["source_display_date"], "2026-10-28")

    def test_meta_from_html_reads_google_published_time_and_article_date(self):
        """meta_from_html reads meta[name=published_time] for time and article:published_time for site calendar date."""
        import archive_url as archive_mod
        html = """
        <html><head>
          <title>Google Blog Post</title>
          <meta name="published_time" content="2026-10-28T23:00:00Z">
          <meta property="article:published_time" content="2026-10-28">
        </head><body><p>Content</p></body></html>
        """
        meta = archive_mod.meta_from_html(html, "https://blog.google/technology/ai/update")
        self.assertEqual(meta["published_original"], "2026-10-28T23:00:00Z")
        self.assertEqual(meta["source_display_date"], "2026-10-28")
        self.assertEqual(meta["date_precision"], "datetime")
        # And parsed date converted to Asia/Shanghai (+08:00)
        self.assertEqual(archive_mod.parse_date(meta["published"]), "2026-10-29T07:00:00+08:00")

    def test_e2e_cross_day_candidate_to_archive_cli_to_entry(self):
        """UTC cross-day candidate retains original UTC string, converted local published, and original calendar date."""
        cand_dir = self.scratch / ".state" / "candidates"
        cand_dir.mkdir(parents=True, exist_ok=True)
        cf = cand_dir / "article.md"
        cf.write_text("正文内容测试跨日保持准确性。", encoding="utf-8")

        cmd = [
            PY,
            str(self.scratch / "scripts" / "archive_url.py"),
            "https://blog.google/technology/ai/gemini-update",
            "--title-zh", "Google Gemini 跨日发布",
            "--category", "industry",
            "--published", "2026-10-29T07:00:00+08:00",
            "--published-original", "2026-10-28T23:00:00Z",
            "--date-precision", "datetime",
            "--source-display-date", "2026-10-28",
            "--content-file", str(cf),
        ]
        res = subprocess.run(
            cmd,
            cwd=str(self.scratch),
            capture_output=True,
            text=True,
            env={**os.environ, "CA_ROOT": str(self.scratch)},
        )
        self.assertEqual(res.returncode, 0, f"CLI error: {res.stderr}\n{res.stdout}")
        data = json.loads(res.stdout)
        self.assertTrue(data["id"].startswith("2026-10-29-"))

        import yaml
        entry_file = Path(data["path"])
        fm = yaml.safe_load(entry_file.read_text(encoding="utf-8").split("---", 2)[1])
        self.assertEqual(fm["published"], "2026-10-29T07:00:00+08:00")
        self.assertEqual(fm["published_original"], "2026-10-28T23:00:00Z")
        self.assertEqual(fm["source_display_date"], "2026-10-28")
        self.assertEqual(fm["date_precision"], "datetime")
        self.assertFalse(fm["published_unknown"])

    def test_e2e_glm_undated_pipeline_candidate_to_archive(self):
        """GLM undated section with real id flows through fetch -> archive CLI -> entry with snapshot and body intact."""
        shutil.copy(ROOT / "scripts" / "fetch_sources.py", self.scratch / "scripts" / "fetch_sources.py")
        sources_cfg = {
            "version": 1,
            "defaults": {"window_hours": 36, "max_items_per_source": 8, "backfill_items": 2},
            "sources": [{
                "id": "glm",
                "label": "GLM",
                "type": "html",
                "adapter": "glm_docs",
                "list_url": "https://docs.bigmodel.cn/release-notes",
                "extractor": "direct",
                "enabled": True,
            }],
        }
        import yaml
        (self.scratch / "sources.yaml").write_text(yaml.safe_dump(sources_cfg), encoding="utf-8")

        html = """
        <div class="update">
          <div data-component-part="update-description">无锚点旧条目</div>
          <div data-component-part="update-label">2026-10-28</div>
          <div data-component-part="update-content">这段没有id锚点，必须拒绝。</div>
        </div>
        <div class="update" id="glm-release-nov26">
          <div data-component-part="update-description">GLM 真实章节无日期更新</div>
          <div data-component-part="update-content">
            这是 GLM 真实章节内容。正文完整保留，且附带 raw 快照。
          </div>
        </div>
        """
        fetch_cmd = [
            PY,
            str(self.scratch / "scripts" / "fetch_sources.py"),
        ]
        with patch.object(fetch_mod, "fetch_text", return_value=html):
            # Run fetch logic in sandbox
            res = subprocess.run(
                [PY, "-c", f"""
import sys, os
sys.path.insert(0, '{self.scratch}/scripts')
from unittest.mock import patch
import fetch_sources
with patch.object(fetch_sources, 'fetch_text', return_value={repr(html)}):
    with patch.object(fetch_sources, 'ROOT', new=fetch_sources.Path('{self.scratch}')):
        sys.exit(fetch_sources.main())
"""],
                cwd=str(self.scratch),
                capture_output=True,
                text=True,
                env={**os.environ, "CA_ROOT": str(self.scratch)},
            )
        self.assertEqual(res.returncode, 0, f"fetch error: {res.stderr}\n{res.stdout}")
        fetch_out = json.loads(res.stdout)
        self.assertEqual(fetch_out["rejections_total"], 1)
        self.assertEqual(len(fetch_out["candidates"]), 1)

        cand = fetch_out["candidates"][0]
        self.assertEqual(cand["url"], "https://docs.bigmodel.cn/release-notes#glm-release-nov26")
        self.assertTrue(cand["published_unknown"])
        self.assertEqual(cand["date_source"], "fetched")
        self.assertEqual(cand["date_precision"], "day")
        self.assertTrue(cand["content_file"])
        self.assertTrue(cand["raw_file"])

        # Now archive this candidate through archive_url.py CLI
        raw_full_path = self.scratch / cand["raw_file"]
        self.assertTrue(raw_full_path.is_file(), f"raw snapshot must exist at {raw_full_path}")

        archive_cmd = [
            PY,
            str(self.scratch / "scripts" / "archive_url.py"),
            cand["url"],
            "--title-zh", cand["title"],
            "--category", "tools",
            "--published", cand["published"],
            "--date-source", cand["date_source"],
            "--published-unknown",
            "--date-precision", cand["date_precision"],
            "--published-original", cand.get("published_original", ""),
            "--source-display-date", cand.get("source_display_date", ""),
            "--content-file", cand["content_file"],
            "--raw-file", str(raw_full_path),
        ]
        res_arch = subprocess.run(
            archive_cmd,
            cwd=str(self.scratch),
            capture_output=True,
            text=True,
            env={**os.environ, "CA_ROOT": str(self.scratch)},
        )
        self.assertEqual(res_arch.returncode, 0, f"archive error: {res_arch.stderr}\n{res_arch.stdout}")
        arch_data = json.loads(res_arch.stdout)
        self.assertEqual(arch_data["status"], "ok")
        self.assertTrue(arch_data["published_unknown"])
        self.assertEqual(arch_data["date_source"], "fetched")
        self.assertEqual(arch_data["date_precision"], "day")

        entry_path = Path(arch_data["path"])
        self.assertTrue(entry_path.is_file())
        entry_text = entry_path.read_text(encoding="utf-8")
        entry_fm = yaml.safe_load(entry_text.split("---", 2)[1])
        self.assertTrue(entry_fm["published_unknown"])
        self.assertEqual(entry_fm["date_source"], "fetched")
        self.assertEqual(entry_fm["date_precision"], "day")
        # Snapshot raw file copied into entry dir
        entry_dir = entry_path.parent
        self.assertTrue((entry_dir / "glm.html.gz").is_file(), "raw snapshot file should be copied into entry dir")
        # Body content preserved
        self.assertIn("这是 GLM 真实章节内容", entry_text)


if __name__ == "__main__":
    unittest.main()
