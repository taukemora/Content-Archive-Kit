#!/usr/bin/env python3
"""Wave 1C regression tests: audit precision, atomic index rebuild, exact search.

Every test builds an isolated throwaway project under the platform temporary
production library/, .state/seen.json, index.csv and index.md are never touched.
"""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))

from common import count_summary_chars, dedupe_key, write_entry

SCRATCH_BASE = _support.SCRATCH
PY = _support.PYTHON

INDEX_FIELDS = [
    "id", "published", "title", "original_title", "source", "source_type",
    "category", "subcategory", "context", "tags", "url", "summary_chars",
    "images", "status", "content_note", "relevance", "path", "archived",
    "language", "summary",
]

SUMMARY_BASE = "本文讨论模型能力演进与工程实践的若干细节，并给出可复用的判断标准与实施路径。"

LONG_BODY = "正文内容充实详细，用于满足长文判定阈值。" * 80
SHORT_BODY = "短文正文。"


def build_project(prefix: str, scripts: tuple[str, ...]) -> Path:
    scratch = Path(tempfile.mkdtemp(dir=str(SCRATCH_BASE), prefix=prefix))
    (scratch / "scripts").mkdir(parents=True, exist_ok=True)
    for name in scripts:
        shutil.copy(ROOT / "scripts" / name, scratch / "scripts" / name)
    if "rebuild_index.py" in scripts:
        for name in ("build_html.py", "media_markup.py", "archive_page.html"):
            shutil.copy(ROOT / "scripts" / name, scratch / "scripts" / name)
    shutil.copy(_support.template("taxonomy.yaml"), scratch / "taxonomy.yaml")
    return scratch


def make_summary(target: int = 205) -> str:
    text = ""
    while count_summary_chars(text) < target:
        text += SUMMARY_BASE
    return text


def entry_body(
    title: str,
    summary: str,
    full_text: str,
    *,
    summary_banner: bool = False,
    full_banner: bool = False,
    banner_text: str = "> ⚠️ **部分抓取**：本文正文在约 1200 字符处截断，以下内容不完整。",
) -> str:
    parts = [f"# {title}", "", "> 来源：测试", ""]
    if summary_banner:
        parts += ["> ⚠️ **摘要基于部分抓取正文**，以下摘要可能不完整。", ""]
    parts += ["## 摘要", "", summary, "", "## 全文", ""]
    if full_banner:
        parts += [banner_text, ""]
    parts += [full_text, ""]
    return "\n".join(parts)


def write_entry_md(
    root: Path,
    rel_dir: str,
    *,
    entry_id: str,
    title: str,
    status: str = "archived",
    source_type: str = "vendor_blog",
    url: str = "https://example.com/a",
    canonical_url: str = "",
    content_note: str = "",
    subcategory: str = "ai",
    summary: str | None = None,
    summary_banner: bool = False,
    full_banner: bool = False,
    full_text: str = LONG_BODY,
) -> Path:
    summary = summary if summary is not None else make_summary()
    fm = {
        "id": entry_id,
        "title": title,
        "source": "测试来源",
        "source_type": source_type,
        "published": "2026-10-01T00:00:00+08:00",
        "archived": "2026-10-02T03:04:05+08:00",
        "url": url,
        "canonical_url": canonical_url or url,
        "category": "tech",
        "subcategory": subcategory,
        "context": "work",
        "tags": ["AI"],
        "summary_chars": count_summary_chars(summary),
        "status": status,
        "content_note": content_note,
        "raw_file": None,
        "images": 0,
        "language": "en",
        "confidence": 0.95,
    }
    path = root / "library" / rel_dir / "entry.md"
    write_entry(path, fm, entry_body(
        title, summary, full_text, summary_banner=summary_banner, full_banner=full_banner,
    ))
    return path


def write_index_csv(root: Path, rows: list[dict]) -> None:
    with open(root / "index.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in INDEX_FIELDS})


def write_seen(root: Path, mapping: dict[str, tuple[str, str]]) -> None:
    state = root / ".state"
    state.mkdir(parents=True, exist_ok=True)
    entries = {
        dedupe_key(url): {"id": entry_id, "path": path, "first_seen": "2026-10-02T03:04:05+08:00"}
        for url, (entry_id, path) in mapping.items()
    }
    (state / "seen.json").write_text(json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8")


def run_script(root: Path, script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-B", f"scripts/{script}", *args],
        capture_output=True, text=True, cwd=str(root),
    )


class AuditScratchBase(unittest.TestCase):
    def setUp(self):
        self.scratch = build_project("ca_test_w1_audit_", ("common.py", "audit.py"))

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def audit(self) -> tuple[int, dict]:
        p = run_script(self.scratch, "audit.py")
        try:
            return p.returncode, json.loads(p.stdout)
        except Exception:
            self.fail(f"audit.py did not emit JSON: {p.stdout!r} {p.stderr!r}")

    def issue_types(self) -> list[str]:
        code, data = self.audit()
        return [i["type"] for i in data.get("issues", [])]

    def sync_index(self) -> None:
        rows = []
        for p in sorted((self.scratch / "library").rglob("entry.md")):
            rows.append({"id": p.parent.name, "path": str(p.relative_to(self.scratch))})
        write_index_csv(self.scratch, rows)


class TestAuditPartialBanners(AuditScratchBase):
    def test_partial_without_full_banner_detected(self):
        write_entry_md(
            self.scratch, "tech/p1", entry_id="p1", title="部分抓取条目一",
            status="partial", content_note="页面在约 1200 字符处截断",
            summary_banner=True, full_banner=False,
        )
        self.sync_index()
        self.assertIn("partial_without_banner", self.issue_types())

    def test_partial_without_summary_banner_detected(self):
        write_entry_md(
            self.scratch, "tech/p2", entry_id="p2", title="部分抓取条目二",
            status="partial", content_note="页面在约 1200 字符处截断",
            summary_banner=False, full_banner=True,
        )
        self.sync_index()
        self.assertIn("partial_without_banner", self.issue_types())

    def test_partial_with_both_banners_and_note_passes(self):
        write_entry_md(
            self.scratch, "tech/p3", entry_id="p3", title="部分抓取条目三",
            status="partial", content_note="页面在约 1200 字符处截断",
            summary_banner=True, full_banner=True,
        )
        self.sync_index()
        types = self.issue_types()
        self.assertNotIn("partial_without_banner", types)

    def test_partial_without_content_note_detected(self):
        write_entry_md(
            self.scratch, "tech/p4", entry_id="p4", title="部分抓取条目四",
            status="partial", content_note="", summary_banner=True, full_banner=True,
        )
        self.sync_index()
        self.assertIn("partial_without_banner", self.issue_types())

    def test_short_partial_uses_short_summary_range(self):
        """The ⚠️ banner must not be counted as body text for the 800-char gate."""
        short_summary = make_summary(target=110)
        write_entry_md(
            self.scratch, "tech/p5", entry_id="p5", title="短截断条目",
            status="partial", content_note="页面在约 30 字符处截断",
            summary_banner=True, full_banner=True, summary=short_summary,
            full_text=SHORT_BODY + "\n" + "欠。",
        )
        self.sync_index()
        types = self.issue_types()
        self.assertNotIn("summary_out_of_range", types)


class TestAuditArchivedResidue(AuditScratchBase):
    def test_archived_with_content_note_detected(self):
        write_entry_md(
            self.scratch, "tech/a1", entry_id="a1", title="残留备注条目",
            status="archived", content_note="页面在约 1200 字符处截断",
        )
        self.sync_index()
        self.assertIn("archived_has_partial_artifact", self.issue_types())

    def test_archived_with_summary_banner_detected(self):
        write_entry_md(
            self.scratch, "tech/a2", entry_id="a2", title="残留摘要横幅条目",
            status="archived", content_note="", summary_banner=True,
        )
        self.sync_index()
        self.assertIn("archived_has_partial_artifact", self.issue_types())

    def test_archived_with_full_banner_detected(self):
        write_entry_md(
            self.scratch, "tech/a3", entry_id="a3", title="残留正文横幅条目",
            status="archived", content_note="", full_banner=True,
        )
        self.sync_index()
        self.assertIn("archived_has_partial_artifact", self.issue_types())

    def test_clean_archived_has_no_residue_issue(self):
        write_entry_md(
            self.scratch, "tech/a4", entry_id="a4", title="干净归档条目",
            status="archived", content_note="",
        )
        self.sync_index()
        types = self.issue_types()
        self.assertNotIn("archived_has_partial_artifact", types)
        self.assertNotIn("partial_without_banner", types)


class TestAuditStatusAndFields(AuditScratchBase):
    def test_draft_status_detected(self):
        write_entry_md(
            self.scratch, "tech/d1", entry_id="d1", title="未完成条目",
            status="draft",
        )
        self.sync_index()
        self.assertIn("bad_status", self.issue_types())

    def test_needs_review_status_allowed(self):
        write_entry_md(
            self.scratch, "tech/d2", entry_id="d2", title="待复核条目",
            status="needs_review",
        )
        self.sync_index()
        self.assertNotIn("bad_status", self.issue_types())

    def test_bad_subcategory_detected(self):
        write_entry_md(
            self.scratch, "tech/d3", entry_id="d3", title="子类格式条目",
            subcategory="AI 模型",
        )
        self.sync_index()
        self.assertIn("bad_subcategory", self.issue_types())

    def test_bad_source_type_detected(self):
        write_entry_md(
            self.scratch, "tech/d4", entry_id="d4", title="来源类型条目",
            source_type="blog",
        )
        self.sync_index()
        self.assertIn("bad_source_type", self.issue_types())


class TestAuditVendorSourceType(AuditScratchBase):
    def test_vendor_domain_blog_and_report_coexist(self):
        write_entry_md(
            self.scratch, "tech/v1", entry_id="v1", title="厂商博客条目",
            source_type="vendor_blog",
            url="https://openai.com/index/entry-one",
        )
        write_entry_md(
            self.scratch, "tech/v2", entry_id="v2", title="厂商报告条目",
            source_type="report",
            url="https://openai.com/index/entry-two",
        )
        self.sync_index()
        write_seen(self.scratch, {
            "https://openai.com/index/entry-one": ("v1", "library/tech/v1/entry.md"),
            "https://openai.com/index/entry-two": ("v2", "library/tech/v2/entry.md"),
        })
        code, data = self.audit()
        self.assertEqual(data.get("issues"), [], f"unexpected issues: {data.get('issues')}")
        self.assertTrue(data.get("ok"))
        self.assertEqual(code, 0)

    def test_vendor_domain_tech_blog_rejected(self):
        write_entry_md(
            self.scratch, "tech/v3", entry_id="v3", title="厂商博客条目",
            source_type="vendor_blog",
            url="https://anthropic.com/news/one",
        )
        write_entry_md(
            self.scratch, "tech/v4", entry_id="v4", title="误标技术博客条目",
            source_type="tech_blog",
            url="https://anthropic.com/news/two",
        )
        self.sync_index()
        self.assertIn("source_type_mismatch", self.issue_types())

    def test_vendor_domain_wechat_rejected(self):
        write_entry_md(
            self.scratch, "tech/v5", entry_id="v5", title="厂商文档条目",
            source_type="docs",
            url="https://kimi.com/blog/one",
        )
        write_entry_md(
            self.scratch, "tech/v6", entry_id="v6", title="误标公众号条目",
            source_type="wechat",
            url="https://kimi.com/blog/two",
        )
        self.sync_index()
        self.assertIn("source_type_mismatch", self.issue_types())

    def test_non_vendor_domain_mixed_types_allowed(self):
        write_entry_md(
            self.scratch, "tech/v7", entry_id="v7", title="第三方博客条目",
            source_type="tech_blog",
            url="https://example.org/post-one",
        )
        write_entry_md(
            self.scratch, "tech/v8", entry_id="v8", title="第三方报告条目",
            source_type="report",
            url="https://example.org/post-two",
        )
        self.sync_index()
        types = self.issue_types()
        self.assertNotIn("source_type_mismatch", types)
        self.assertNotIn("source_type_inconsistent", types)


class TestAuditDuplicateCanonicalUrl(AuditScratchBase):
    def test_duplicate_canonical_url_detected(self):
        write_entry_md(
            self.scratch, "tech/u1", entry_id="u1", title="重复链接条目一",
            url="https://example.com/dup?a=1", canonical_url="https://example.com/dup?a=1",
        )
        write_entry_md(
            self.scratch, "tech/u2", entry_id="u2", title="重复链接条目二",
            url="https://example.com/dup?a=1", canonical_url="https://example.com/dup?a=1",
        )
        self.sync_index()
        code, data = self.audit()
        self.assertEqual(code, 1)
        dup = [i for i in data["issues"] if i["type"] == "duplicate_canonical_url"]
        self.assertEqual(len(dup), 2, dup)
        self.assertEqual(sorted(i["path"] for i in dup),
                         ["library/tech/u1/entry.md", "library/tech/u2/entry.md"])

    def test_distinct_urls_not_flagged(self):
        write_entry_md(
            self.scratch, "tech/u3", entry_id="u3", title="独立链接条目一",
            url="https://example.com/one",
        )
        write_entry_md(
            self.scratch, "tech/u4", entry_id="u4", title="独立链接条目二",
            url="https://example.com/two",
        )
        self.sync_index()
        self.assertNotIn("duplicate_canonical_url", self.issue_types())


class TestSearchExactFilters(unittest.TestCase):
    ROWS = [
        {
            "id": "s1", "published": "2026-10-01", "title": "标签精确匹配甲",
            "original_title": "", "source": "来源甲", "source_type": "vendor_blog",
            "category": "tech", "subcategory": "ai", "context": "work",
            "tags": "AI, 大模型", "url": "https://example.com/s1", "summary_chars": 210,
            "images": 0, "status": "archived", "content_note": "", "relevance": "",
            "path": "library/tech/s1/entry.md", "archived": "2026-10-02T03:04:05+08:00",
            "language": "zh", "summary": "摘要甲",
        },
        {
            "id": "s2", "published": "2026-10-02", "title": "标签子串陷阱乙",
            "original_title": "", "source": "来源乙", "source_type": "vendor_blog",
            "category": "tech", "subcategory": "companies", "context": "reference",
            "tags": "OpenAI, Agent", "url": "https://example.com/s2", "summary_chars": 210,
            "images": 0, "status": "partial", "content_note": "截断", "relevance": "",
            "path": "library/tech/s2/entry.md", "archived": "2026-10-02T03:04:05+08:00",
            "language": "en", "summary": "摘要乙",
        },
        {
            "id": "s3", "published": "2026-10-03", "title": "标签子串陷阱丙",
            "original_title": "", "source": "来源丙", "source_type": "wechat",
            "category": "tech", "subcategory": "ai", "context": "work",
            "tags": "AI写作, 文风", "url": "https://example.com/s3", "summary_chars": 210,
            "images": 0, "status": "archived", "content_note": "", "relevance": "",
            "path": "library/tech/s3/entry.md", "archived": "2026-10-02T03:04:05+08:00",
            "language": "zh", "summary": "摘要丙",
        },
    ]

    def setUp(self):
        self.scratch = build_project("ca_test_w1_search_", ("common.py", "search.py"))
        write_index_csv(self.scratch, self.ROWS)

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def search(self, *args: str) -> tuple[int, list[dict]]:
        p = run_script(self.scratch, "search.py", "--json", *args)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.returncode, json.loads(p.stdout)

    def ids(self, *args: str) -> list[str]:
        _, rows = self.search(*args)
        return sorted(r["id"] for r in rows)

    def test_tag_ai_matches_exactly(self):
        self.assertEqual(self.ids("--tag", "AI"), ["s1"])

    def test_tag_ai_is_case_insensitive(self):
        self.assertEqual(self.ids("--tag", "ai"), ["s1"])

    def test_tag_does_not_substring_match(self):
        # "AI" must not hit "OpenAI" (s2) or "AI写作" (s3)
        ids = self.ids("--tag", "AI")
        self.assertNotIn("s2", ids)
        self.assertNotIn("s3", ids)

    def test_multiple_tags_comma_separated(self):
        self.assertEqual(self.ids("--tag", "AI,大模型"), ["s1"])
        self.assertEqual(self.ids("--tag", "AI、大模型"), ["s1"])

    def test_multiple_tags_repeated_flag(self):
        self.assertEqual(self.ids("--tag", "AI", "--tag", "大模型"), ["s1"])

    def test_tag_requires_all_requested(self):
        self.assertEqual(self.ids("--tag", "AI", "--tag", "Agent"), [])

    def test_subcategory_filter(self):
        self.assertEqual(self.ids("--subcategory", "companies"), ["s2"])
        self.assertEqual(self.ids("--subcategory", "ai"), ["s1", "s3"])

    def test_status_filter(self):
        self.assertEqual(self.ids("--status", "partial"), ["s2"])
        self.assertEqual(self.ids("--status", "archived"), ["s1", "s3"])

    def test_combined_filters(self):
        self.assertEqual(self.ids("--subcategory", "ai", "--status", "archived"), ["s1", "s3"])
        self.assertEqual(self.ids("--subcategory", "ai", "--status", "partial"), [])


class TestRebuildIndexAtomic(unittest.TestCase):
    def setUp(self):
        self.scratch = build_project("ca_test_w1_rebuild_", ("common.py", "rebuild_index.py"))
        summary = make_summary()
        write_entry_md(
            self.scratch, "tech/r1", entry_id="r1", title="原子写入条目",
            summary=summary,
        )
        # archived kept as a full ISO string with its +08:00 offset
        fm_path = self.scratch / "library" / "tech" / "r1" / "entry.md"
        text = fm_path.read_text(encoding="utf-8").replace(
            "archived: 2026-10-02T03:04:05+08:00", "archived: '2026-10-02T03:04:05+08:00'"
        )
        fm_path.write_text(text, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_no_temp_files_remain_and_archived_keeps_offset(self):
        p = run_script(self.scratch, "rebuild_index.py")
        self.assertEqual(p.returncode, 0, p.stderr)

        leftovers = sorted(x.name for x in self.scratch.iterdir() if x.name.endswith(".tmp"))
        self.assertEqual(leftovers, [], f"leftover temp files: {leftovers}")
        self.assertTrue((self.scratch / "index.csv").exists())
        self.assertTrue((self.scratch / "index.md").exists())

        with open(self.scratch / "index.csv", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["archived"], "2026-10-02T03:04:05+08:00")
        self.assertEqual(rows[0]["published"], "2026-10-01")

    def test_second_run_is_stable(self):
        self.assertEqual(run_script(self.scratch, "rebuild_index.py").returncode, 0)
        first = (self.scratch / "index.csv").read_bytes()
        self.assertEqual(run_script(self.scratch, "rebuild_index.py").returncode, 0)
        self.assertEqual((self.scratch / "index.csv").read_bytes(), first)
        leftovers = sorted(x.name for x in self.scratch.iterdir() if x.name.endswith(".tmp"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
