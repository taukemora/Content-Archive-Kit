#!/usr/bin/env python3
"""Unit and integration tests for Phase 2 implementation."""
from __future__ import annotations

import csv
import json

import yaml
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))

from common import read_entry, write_entry, category_slugs
from cleanup_h1 import cleanup_entry_h1
from search import extract_snippet
from rebuild_index import vendor_name, VENDOR_ORDER


class TestCleanupH1(unittest.TestCase):
    def test_single_h1_unchanged(self):
        body = "# 标题\n\n> 来源：...\n\n## 摘要\n\n摘要内容\n\n## 全文\n\n正文内容\n"
        cleaned, removed = cleanup_entry_h1(body)
        self.assertEqual(len(removed), 0)
        self.assertEqual(cleaned, body)

    def test_multiple_h1_cleaned(self):
        body = (
            "# 中文标题\n\n"
            "> 来源：...\n\n"
            "## 摘要\n\n摘要内容\n\n"
            "## 全文\n\n"
            "# English Subtitle Duplicate H1\n\n"
            "正文第一段。\n\n"
            "# Yet Another H1\n\n"
            "正文第二段。\n"
        )
        cleaned, removed = cleanup_entry_h1(body)
        self.assertEqual(removed, ["# English Subtitle Duplicate H1", "# Yet Another H1"])
        self.assertIn("# 中文标题", cleaned)
        self.assertNotIn("# English Subtitle Duplicate H1", cleaned)
        self.assertNotIn("# Yet Another H1", cleaned)
        self.assertIn("正文第一段。", cleaned)
        self.assertIn("正文第二段。", cleaned)


class TestSearchFullSnippet(unittest.TestCase):
    def test_extract_snippet(self):
        text = (
            "This is a synthetic article about GPU compilers. It describes a compact"
            " Triton-like compiler with its own tile-level IR layer over MLIR."
        )
        snippet = extract_snippet(text, "Triton-like", window=30)
        self.assertIn("Triton-like", snippet)
        self.assertIn("compiler", snippet)

    def test_search_full_cli_vs_default(self):
        """Default search covers title/tags/summary; ``--full`` also scans the body.

        Runs against a throwaway archive so no real article text is involved.
        """
        project = build_scratch_project("ca_test_search_p2_", ("common.py", "search.py", "rebuild_index.py"))
        self.addCleanup(shutil.rmtree, project, True)
        make_entry(
            project,
            "tech/compiler-notes",
            {
                "id": "compiler-notes",
                "title": "编译器分层笔记",
                "source": "Vendor Alpha",
                "source_type": "vendor_blog",
                "category": "tech",
                "context": "work",
                "tags": ["AI", "编译器"],
                "url": "https://alpha.example.com/blog/compiler-layers",
                "summary_chars": 40,
                "status": "archived",
                "published": "2026-01-10T00:00:00+08:00",
                "archived": "2026-01-11T03:04:05+08:00",
            },
            sample_body(
                "编译器分层笔记",
                "这份摘要只谈分层边界与后端调度的取舍。",
                "正文讨论了瓦片化降低（tile-lowering）与寄存器分配的先后顺序。" * 20,
            ),
        )
        make_entry(
            project,
            "tech/benchmark-notes",
            {
                "id": "benchmark-notes",
                "title": "基准测试复现笔记",
                "source": "Vendor Beta",
                "source_type": "vendor_blog",
                "category": "tech",
                "context": "reference",
                "tags": ["基准"],
                "url": "https://beta.example.com/news/benchmark-notes",
                "summary_chars": 40,
                "status": "archived",
                "published": "2026-01-09T00:00:00+08:00",
                "archived": "2026-01-11T03:04:06+08:00",
            },
            sample_body(
                "基准测试复现笔记",
                "基准测试的复现步骤与硬件清单。",
                "复现时需要固定驱动版本。" * 20,
            ),
        )
        rebuild = subprocess.run(
            [_support.PYTHON, "-B", "scripts/rebuild_index.py"],
            capture_output=True, text=True, cwd=str(project),
        )
        self.assertEqual(rebuild.returncode, 0, rebuild.stderr)

        def search(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [_support.PYTHON, "-B", "scripts/search.py", *args],
                capture_output=True, text=True, cwd=str(project),
            )

        # "tile-lowering" only exists in the body of the first entry.
        p_def = search("tile-lowering")
        self.assertEqual(p_def.returncode, 0, p_def.stderr)
        self.assertIn("no matches", p_def.stdout)

        p_full = search("--full", "tile-lowering")
        self.assertEqual(p_full.returncode, 0, p_full.stderr)
        self.assertIn("编译器分层笔记", p_full.stdout)
        self.assertIn("匹配片段", p_full.stdout)

        # A term present in the title matches in both modes; --full adds a snippet.
        p_title_def = search("基准测试复现笔记")
        self.assertEqual(p_title_def.returncode, 0, p_title_def.stderr)
        self.assertIn("基准测试复现笔记", p_title_def.stdout)
        self.assertNotIn("匹配片段", p_title_def.stdout)

        p_title_full = search("--full", "基准测试复现笔记")
        self.assertEqual(p_title_full.returncode, 0, p_title_full.stderr)
        self.assertIn("基准测试复现笔记", p_title_full.stdout)
        self.assertIn("匹配片段", p_title_full.stdout)


def build_scratch_project(prefix: str, scripts: tuple[str, ...]) -> Path:
    """Create an isolated throwaway project root under the temporary directory."""
    scratch = _support.scratch_dir(prefix)
    (scratch / "scripts").mkdir(parents=True, exist_ok=True)
    for name in scripts:
        shutil.copy(ROOT / "scripts" / name, scratch / "scripts" / name)
    if "rebuild_index.py" in scripts:
        for name in ("build_html.py", "media_markup.py", "archive_page.html"):
            shutil.copy(ROOT / "scripts" / name, scratch / "scripts" / name)
    shutil.copy(_support.template("taxonomy.yaml"), scratch / "taxonomy.yaml")
    return scratch


def make_entry(root: Path, rel_dir: str, fm: dict, body: str) -> Path:
    entry_path = root / "library" / rel_dir / "entry.md"
    write_entry(entry_path, fm, body)
    return entry_path


def sample_body(title: str, summary: str, full: str) -> str:
    return f"# {title}\n\n> 来源：测试\n\n## 摘要\n\n{summary}\n\n## 全文\n\n{full}\n"


class TestRebuildIndexStructure(unittest.TestCase):
    """Rebuild behaviour is verified in a scratch project, never on production data.

    The production index.md is only checked for its stable skeleton (section
    headings and the full category list), so archiving new entries into
    previously empty categories can never break the suite.
    """

    def setUp(self):
        self.scratch = build_scratch_project("ca_test_index_p2_", ("common.py", "rebuild_index.py"))
        self.summary_a = "这是一段用于索引重建测试的中文摘要内容，覆盖原子写入与分类结构校验。" * 4
        make_entry(
            self.scratch,
            "tech/entry-a",
            {
                "id": "entry-a",
                "title": "测试甲条目",
                "source": "OpenAI",
                "source_type": "vendor_blog",
                "category": "tech",
                "subcategory": "ai",
                "context": "work",
                "tags": ["AI", "测试"],
                "url": "https://openai.com/index/entry-a",
                "summary_chars": 60,
                "status": "archived",
                "published": "2026-10-01T00:00:00+08:00",
                "archived": "2026-10-02T03:04:05+08:00",
                "relevance": "值得一读",
            },
            sample_body("测试甲条目", self.summary_a, "正文甲。" * 100),
        )
        make_entry(
            self.scratch,
            "business/entry-b",
            {
                "id": "entry-b",
                "title": "测试乙条目",
                "source": "某公司",
                "source_type": "report",
                "category": "business",
                "context": "reference",
                "tags": ["IPO"],
                "url": "https://example.com/entry-b",
                "summary_chars": 60,
                "status": "archived",
                "published": "2026-09-15T00:00:00+08:00",
                "archived": "2026-09-16T03:04:05+08:00",
            },
            sample_body("测试乙条目", self.summary_a, "正文乙。" * 100),
        )
        py = _support.PYTHON
        self.proc = subprocess.run(
            [py, "-B", "scripts/rebuild_index.py"],
            capture_output=True, text=True, cwd=str(self.scratch),
        )

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_rebuild_succeeds_and_reports_rows(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        data = json.loads(self.proc.stdout)
        self.assertEqual(data["entries"], 2)
        self.assertEqual(data["by_category"], {"tech": 1, "business": 1})

    def test_index_md_three_sections_and_counts(self):
        index_md = (self.scratch / "index.md").read_text(encoding="utf-8")
        self.assertIn("共 2 篇。", index_md)
        for heading in ("## 按分类", "## 按厂商", "## 按月份"):
            self.assertIn(heading, index_md)
        self.assertIn("### 科技与 AI（1）", index_md)
        self.assertIn("### 商业与产业（1）", index_md)

    def test_empty_categories_show_zero(self):
        index_md = (self.scratch / "index.md").read_text(encoding="utf-8")
        self.assertIn("### 编程与开发（0）", index_md)
        self.assertIn("### 工作与成长（0）", index_md)
        self.assertIn("### 社会与公共（0）", index_md)
        self.assertIn("### 文化与阅读（0）", index_md)
        self.assertIn("### 生活与健康（0）", index_md)
        self.assertIn("### 其他（0）", index_md)

    def test_index_csv_has_relevance_field(self):
        with open(self.scratch / "index.csv", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            rows = list(reader)
        self.assertIn("relevance", fieldnames)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["relevance"] or rows[1]["relevance"], "值得一读")

    def test_both_index_files_written(self):
        self.assertTrue((self.scratch / "index.csv").exists())
        self.assertTrue((self.scratch / "index.md").exists())
        self.assertTrue((self.scratch / "index.csv").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_rebuilt_index_lists_every_taxonomy_category(self):
        """Every category gets a heading, including empty ones (generated index)."""
        index_md = (self.scratch / "index.md").read_text(encoding="utf-8")
        for heading in ("## 按分类", "## 按厂商", "## 按月份"):
            self.assertIn(heading, index_md)
        categories = yaml.safe_load((self.scratch / "taxonomy.yaml").read_text(encoding="utf-8"))
        for cat in categories.get("categories", []):
            self.assertIn(f"### {cat['label']}", index_md, f"missing category heading for {cat['slug']}")

    def test_rebuilt_index_csv_carries_stable_columns(self):
        """The CSV schema stays machine-readable: relevance plus retrieval fields."""
        with open(self.scratch / "index.csv", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
        for column in ("id", "title", "url", "category", "context", "tags", "path", "relevance"):
            self.assertIn(column, fieldnames)

    def test_vendor_mapping(self):
        self.assertEqual(vendor_name({"source": "OpenAI", "url": "https://openai.com/..."}), "OpenAI")
        self.assertEqual(vendor_name({"source": "Anthropic", "url": "https://anthropic.com/..."}), "Anthropic")
        self.assertEqual(vendor_name({"source": "Kimi / 月之暗面", "url": "https://kimi.com/..."}), "Kimi")
        self.assertEqual(vendor_name({"source": "GLM / 智谱 文档更新", "url": "https://docs.bigmodel.cn/..."}), "GLM")
        self.assertEqual(vendor_name({"source": "腾讯WorkBuddy", "source_type": "wechat", "url": "https://mp.weixin.qq.com/..."}), "微信公众号")
        self.assertEqual(vendor_name({"source": "Random Source", "url": "https://example.com"}), "其他")


INDEX_FIELDS = [
    "id", "published", "title", "original_title", "source", "source_type",
    "category", "subcategory", "context", "tags", "url", "summary_chars",
    "images", "status", "content_note", "relevance", "path", "archived",
    "language", "collection", "summary",
]

# Synthetic display titles for the candidates in tests/fixtures/latest-fetch-synthetic.json,
# in the order the digest should print them (newest ``published`` first).
DIGEST_TITLES = [
    ("https://alpha.example.com/blog/open-inference-engine", "Alpha 发布开源推理引擎"),
    ("https://alpha.example.com/blog/inference-engine-licence", "Alpha 说明推理引擎许可"),
    ("https://beta.example.com/news/model-benchmark", "Beta 发布模型评测基准"),
    ("https://beta.example.com/news/benchmark-reproducibility", "Beta 公布基准复现步骤"),
    ("https://gamma.example.com/blog/code-assistant-preview", "Gamma 预览代码助手"),
    ("https://gamma.example.com/blog/code-assistant-privacy", "Gamma 发布代码助手隐私说明"),
]


class TestDigestGlobalNumberingAndRelevance(unittest.TestCase):
    """Digest numbering/highlights, driven by a synthetic run and a synthetic index."""

    def setUp(self):
        self.scratch = build_scratch_project("ca_test_digest_p2_", ("common.py", "build_digest.py"))
        fetch = json.loads((_support.FIXTURES / "latest-fetch-synthetic.json").read_text(encoding="utf-8"))
        by_url = {c["url"]: c for c in fetch["candidates"]}
        with open(self.scratch / "index.csv", "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=INDEX_FIELDS)
            writer.writeheader()
            for url, title in DIGEST_TITLES:
                cand = by_url[url]
                writer.writerow(
                    {
                        "id": url.rsplit("/", 1)[-1],
                        "published": cand["published"],
                        "title": title,
                        "original_title": cand["title"],
                        "source": cand["source"],
                        "source_type": cand["source_type"],
                        "category": "tech",
                        "context": "work",
                        "tags": "AI",
                        "url": url,
                        "summary_chars": 40,
                        "status": "archived",
                        # After the run's generated_at, so every row counts as new.
                        "archived": "2026-01-15T10:00:00+08:00",
                        "language": "en",
                        "collection": "auto",
                        "summary": "合成摘要，用于验证日报编号与分组。",
                        "path": f"library/tech/{url.rsplit('/', 1)[-1]}/entry.md",
                    }
                )

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_digest_global_numbering_and_relevance(self):
        digest_script = str(self.scratch / "scripts" / "build_digest.py")
        p = subprocess.run(
            [
                _support.PYTHON, "-B", digest_script,
                "--run", str(_support.FIXTURES / "latest-fetch-synthetic.json"),
                "--limit", "12",
            ],
            capture_output=True, text=True, cwd=str(self.scratch),
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        output = p.stdout

        # Global numbering 1..6 across source groups, newest first.
        for idx, (_, title) in enumerate(DIGEST_TITLES, start=1):
            self.assertIn(f"{idx}. {title}", output)

        # More than 5 rows, so the highlight section must appear.
        self.assertIn("## 本日要点", output)

        # No relevance on any row, so the per-entry why-read note must not appear.
        self.assertNotIn("为什么值得看", output)


class TestFinalizeParameters(unittest.TestCase):
    def setUp(self):
        # A self-contained archive root: the sandbox copy of finalize_entry.py
        # derives its project root from its own location, so the test never
        # depends on the checkout looking like an archive.
        self.scratch = _support.archive_project("ca_test_fin_p2_", state=True)
        self.entry_dir = self.scratch / "library" / "tech" / "test-entry"
        self.entry_dir.mkdir(parents=True, exist_ok=True)
        self.entry_path = self.entry_dir / "entry.md"
        fm = {
            "id": "test-entry",
            "title": "测试条目",
            "category": "tech",
            "context": "work",
            "summary_chars": 205,
            "status": "archived",
            "source_type": "vendor_blog",
            "confidence": 1.0,
        }
        body = (
            "# 测试条目\n\n"
            "> 来源：...\n\n"
            "## 摘要\n\n"
            "测试中文摘要内容测试中文摘要内容测试中文摘要内容测试中文摘要内容测试中文摘要内容测试中文摘要内容测试中文摘要内容测试中文摘要内容测试中文摘要内容测试中文摘要内容测试中文摘要内容测试中文摘要内容测试中文摘要内容测试中文摘要内容测试中文摘要内容测试中文摘要内容测试中文摘要内容测试中文摘要内容测试中文摘要内容测试中文摘要内容测试中文摘要内容测试中文摘要内容测试中文摘要内容。\n\n"
            "## 全文\n\n"
            "正文长内容充实详细。" * 40
        )
        write_entry(self.entry_path, fm, body)

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_finalize_source_type_and_relevance(self):
        py = _support.PYTHON
        fin_script = str(self.scratch / "scripts" / "finalize_entry.py")

        res = subprocess.run([
            py, "-B", fin_script,
            "--path", str(self.entry_path),
            "--source-type", "docs",
            "--relevance", "深入理解模型架构演进"
        ], capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(res.returncode, 0)

        fm, _ = read_entry(self.entry_path)
        self.assertEqual(fm.get("source_type"), "docs")
        self.assertEqual(fm.get("relevance"), "深入理解模型架构演进")


class TestAuditChecks(unittest.TestCase):
    def setUp(self):
        self.scratch = _support.scratch_dir("ca_test_audit_p2_")
        # Create a mock project structure in scratch to test audit failure modes
        (self.scratch / "scripts").mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "scripts" / "common.py", self.scratch / "scripts" / "common.py")
        shutil.copy(ROOT / "scripts" / "audit.py", self.scratch / "scripts" / "audit.py")
        shutil.copy(_support.template("taxonomy.yaml"), self.scratch / "taxonomy.yaml")
        shutil.copy(_support.template("sources.yaml"), self.scratch / "sources.yaml")

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def _run_audit(self) -> tuple[int, dict]:
        py = _support.PYTHON
        p = subprocess.run([py, "-B", "scripts/audit.py"], capture_output=True, text=True, cwd=str(self.scratch))
        try:
            data = json.loads(p.stdout)
        except Exception:
            data = {"raw": p.stdout, "err": p.stderr}
        return p.returncode, data

    def test_audit_detects_duplicate_h1(self):
        # Create an entry with duplicate H1 in scratch
        entry_dir = self.scratch / "library" / "tech" / "dup-h1-entry"
        entry_dir.mkdir(parents=True, exist_ok=True)
        fm = {
            "id": "dup-h1-entry",
            "title": "测试中文标题",
            "category": "tech",
            "context": "work",
            "summary_chars": 204,
            "status": "archived",
            "source_type": "vendor_blog",
            "url": "https://example.com/dup",
        }
        body = (
            "# 测试中文标题\n\n"
            "> 来源：...\n\n"
            "## 摘要\n\n"
            "这是一段经过严格计数的中文摘要测试内容，确保字数在两百到两百五十字符范围之内，用于测试审计脚本对重复标题的拦截能力。这是一段经过严格计数的中文摘要测试内容，确保字数在两百到两百五十字符范围之内，用于测试审计脚本对重复标题的拦截能力。这是一段经过严格计数的中文摘要测试内容。\n\n"
            "## 全文\n\n"
            "# 第二个大标题\n\n"
            "正文长内容充实详细。" * 40
        )
        write_entry(entry_dir / "entry.md", fm, body)

        # Create aligned index.csv
        idx = self.scratch / "index.csv"
        idx.write_text(
            "id,path\n"
            "dup-h1-entry,library/tech/dup-h1-entry/entry.md\n",
            encoding="utf-8-sig"
        )

        code, data = self._run_audit()
        self.assertEqual(code, 1)
        issue_types = [i["type"] for i in data.get("issues", [])]
        self.assertIn("duplicate_h1", issue_types)

    def test_audit_detects_bigmodel_source_type_mismatch(self):
        entry_dir = self.scratch / "library" / "tech" / "glm-entry"
        entry_dir.mkdir(parents=True, exist_ok=True)
        fm = {
            "id": "glm-entry",
            "title": "测试智谱文档",
            "category": "tech",
            "context": "work",
            "summary_chars": 204,
            "status": "archived",
            "source_type": "vendor_blog",  # wrong, must be docs
            "url": "https://docs.bigmodel.cn/cn/update/new-releases#test",
        }
        body = (
            "# 测试智谱文档\n\n"
            "> 来源：...\n\n"
            "## 摘要\n\n"
            "这是一段经过严格计数的中文摘要测试内容，确保字数在两百到两百五十字符范围之内，用于测试审计脚本对重复标题的拦截能力。这是一段经过严格计数的中文摘要测试内容，确保字数在两百到两百五十字符范围之内，用于测试审计脚本对重复标题的拦截能力。这是一段经过严格计数的中文摘要测试内容。\n\n"
            "## 全文\n\n"
            "正文长内容充实详细。" * 40
        )
        write_entry(entry_dir / "entry.md", fm, body)
        idx = self.scratch / "index.csv"
        idx.write_text("id,path\nglm-entry,library/tech/glm-entry/entry.md\n", encoding="utf-8-sig")

        code, data = self._run_audit()
        self.assertEqual(code, 1)
        issue_types = [i["type"] for i in data.get("issues", [])]
        self.assertIn("source_type_mismatch", issue_types)

    def test_audit_detects_index_row_count_mismatch(self):
        entry_dir = self.scratch / "library" / "tech" / "ok-entry"
        entry_dir.mkdir(parents=True, exist_ok=True)
        fm = {
            "id": "ok-entry",
            "title": "测试正常条目",
            "category": "tech",
            "context": "work",
            "summary_chars": 204,
            "status": "archived",
            "source_type": "vendor_blog",
            "url": "https://example.com/ok",
        }
        body = (
            "# 测试正常条目\n\n"
            "> 来源：...\n\n"
            "## 摘要\n\n"
            "这是一段经过严格计数的中文摘要测试内容，确保字数在两百到两百五十字符范围之内，用于测试审计脚本对重复标题的拦截能力。这是一段经过严格计数的中文摘要测试内容，确保字数在两百到两百五十字符范围之内，用于测试审计脚本对重复标题的拦截能力。这是一段经过严格计数的中文摘要测试内容。\n\n"
            "## 全文\n\n"
            "正文长内容充实详细。" * 40
        )
        write_entry(entry_dir / "entry.md", fm, body)

        # index.csv has 0 rows, but 1 entry exists
        idx = self.scratch / "index.csv"
        idx.write_text("id,path\n", encoding="utf-8-sig")

        code, data = self._run_audit()
        self.assertEqual(code, 1)
        issue_types = [i["type"] for i in data.get("issues", [])]
        self.assertIn("index_row_count_mismatch", issue_types)


if __name__ == "__main__":
    unittest.main()
