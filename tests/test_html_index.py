#!/usr/bin/env python3
"""Tests for standalone offline HTML index generator (scripts/build_html.py).

Verifies Oink reader philosophy, offline self-containment, MarkdownIt rendering,
strict image/link sandboxing, XSS/script escapes, collection filtering,
archived time descending order, warnings/badges, and mobile responsiveness.

All test artifacts are created inside the platform temporary directory.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
SCRIPTS = ROOT / "scripts"
SCRATCH = _support.SCRATCH
SCRATCH.mkdir(parents=True, exist_ok=True)

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_html import build_html


class TestHtmlIndex(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(dir=str(SCRATCH), prefix="test_html_"))
        self.labels = {
            "tech": "科技与 AI",
            "coding": "编程与开发",
            "business": "商业与产业",
            "other": "其他",
        }

    def tearDown(self):
        if self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_mock_entry(
        self,
        rel_path: str,
        title: str,
        body_content: str,
        collection: str = "manual",
        published: str = "2026-10-20",
        archived: str = "2026-10-21T10:00:00+08:00",
        status: str = "archived",
        category: str = "coding",
        tags: list[str] | None = None,
        source: str = "测试发布方",
        original_title: str = "",
        content_note: str = "",
        image_note: str = "",
        published_unknown: bool = False,
    ) -> dict:
        entry_file = self.tmp_dir / rel_path
        entry_file.parent.mkdir(parents=True, exist_ok=True)

        fm_lines = [
            "---",
            f"id: {entry_file.parent.name}",
            f"title: {title}",
            f"original_title: {original_title or title}",
            f"published: {published}",
            f"archived: {archived}",
            f"category: {category}",
            f"source: {source}",
            f"status: {status}",
            f"collection: {collection}",
            f"content_note: '{content_note}'",
            f"image_note: '{image_note}'",
            f"published_unknown: {'true' if published_unknown else 'false'}",
        ]
        if tags:
            fm_lines.append("tags:")
            for t in tags:
                fm_lines.append(f"  - {t}")
        fm_lines.append("---\n")

        full_text = "\n".join(fm_lines) + "\n" + body_content
        entry_file.write_text(full_text, encoding="utf-8")

        return {
            "id": entry_file.parent.name,
            "title": title,
            "original_title": original_title or title,
            "published": published,
            "archived": archived,
            "category": category,
            "source": source,
            "status": status,
            "collection": collection,
            "content_note": content_note,
            "image_note": image_note,
            "published_unknown": published_unknown,
            "tags": tags or [],
            "url": "https://example.com/article",
            "path": rel_path,
            "summary": "这是文章的中文摘要测试内容。",
        }

    def _extract_embedded_articles(self, html_doc: str) -> list[dict]:
        m = re.search(r'<script id="archive-data" type="application/json">\s*(.*?)\s*</script>', html_doc, re.S)
        self.assertIsNotNone(m, "archive-data JSON block must be present")
        return json.loads(m.group(1))

    def test_build_html_signature_and_types(self):
        """build_html must accept (root: Path, rows: list[dict], labels: dict[str, str]) -> str."""
        row = self._create_mock_entry(
            rel_path="library/coding/mock-1/entry.md",
            title="测试文章 1",
            body_content="# 测试文章 1\n\n正文内容测试。",
        )
        output = build_html(self.tmp_dir, [row], self.labels)
        self.assertIsInstance(output, str)
        self.assertTrue(output.strip().startswith("<!DOCTYPE html>"))
        self.assertIn("<html lang=\"zh-CN\">", output)
        self.assertIn("</html>", output)

    def test_offline_strict_no_external_resources(self):
        """Page must be 100% offline: no external CDN scripts, stylesheets, fonts, or fetch."""
        row = self._create_mock_entry(
            rel_path="library/coding/mock-1/entry.md",
            title="离线测试文章",
            body_content="离线阅读正文测试。",
        )
        output = build_html(self.tmp_dir, [row], self.labels)

        # Prohibit external scripts and stylesheets
        self.assertNotRegex(output, r'<script[^>]+src=["\']http')
        self.assertNotRegex(output, r'<link[^>]+href=["\']http')
        self.assertNotRegex(output, r'@import\s+url\(["\']?http')

        # Prohibit CDNs
        for cdn in ["cdnjs.cloudflare.com", "unpkg.com", "jsdelivr.net", "fonts.googleapis.com"]:
            self.assertNotIn(cdn, output)

        # Prohibit network fetch / XMLHttpRequest
        self.assertNotIn("fetch(", output)
        self.assertNotIn("XMLHttpRequest", output)

    def test_markdown_rendering_table_and_strikethrough(self):
        """MarkdownIt must support tables, strikethrough, blockquotes, and code blocks."""
        body = (
            "| 表头A | 表头B |\n"
            "|---|---|\n"
            "| 内容1 | ~~删除内容~~ |\n\n"
            "> 这是引用文本\n\n"
            "```python\nprint('hello world')\n```\n"
        )
        row = self._create_mock_entry(
            rel_path="library/coding/mock-table/entry.md",
            title="表格与删除线测试",
            body_content=body,
        )
        output = build_html(self.tmp_dir, [row], self.labels)
        articles = self._extract_embedded_articles(output)
        body_html = articles[0]["body_html"]

        self.assertIn("<table>", body_html)
        self.assertIn("<th>表头A</th>", body_html)
        self.assertIn("<td>内容1</td>", body_html)
        self.assertTrue("<s>删除内容</s>" in body_html or "<del>删除内容</del>" in body_html)
        self.assertIn("<blockquote>", body_html)
        self.assertIn("<pre><code", body_html)

    def test_markdown_raw_html_escaped(self):
        """Raw HTML in markdown source must be escaped by MarkdownIt (html=False)."""
        body = (
            "<script>alert('xss1')</script>\n"
            "<iframe src='http://evil.com'></iframe>\n"
            "<style>body { display: none; }</style>\n"
            "<div onclick='alert(2)'>Click me</div>\n"
        )
        row = self._create_mock_entry(
            rel_path="library/tech/mock-xss/entry.md",
            title="XSS 过滤测试",
            body_content=body,
        )
        output = build_html(self.tmp_dir, [row], self.labels)
        articles = self._extract_embedded_articles(output)
        body_html = articles[0]["body_html"]

        self.assertNotIn("<script>", body_html)
        self.assertIn("&lt;script&gt;", body_html)
        self.assertNotIn("<iframe", body_html)
        self.assertIn("&lt;iframe", body_html)
        self.assertNotIn("<style>", body_html)
        self.assertIn("&lt;style&gt;", body_html)
        self.assertNotIn("<div onclick", body_html)

    def test_image_local_valid(self):
        """Local images within article dir must render with root-relative URL-encoded paths."""
        rel_entry = "library/coding/2026-08-22-测试 文章/entry.md"
        entry_dir = self.tmp_dir / "library/coding/2026-08-22-测试 文章"
        img_dir = entry_dir / "images"
        img_dir.mkdir(parents=True, exist_ok=True)
        img_file = img_dir / "架构 图.png"
        img_file.write_bytes(b"\x89PNG\r\n\x1a\nfakeimage")

        body = "![系统 架构图](images/%E6%9E%B6%E6%9E%84%20%E5%9B%BE.png)"
        row = self._create_mock_entry(
            rel_path=rel_entry,
            title="本地图片测试",
            body_content=body,
        )
        output = build_html(self.tmp_dir, [row], self.labels)
        articles = self._extract_embedded_articles(output)
        body_html = articles[0]["body_html"]

        self.assertIn("<img", body_html)
        self.assertIn("loading=\"lazy\"", body_html)
        self.assertIn("alt=\"系统 架构图\"", body_html)
        # Verify POSIX forward slashes and URL encoding
        self.assertIn("library/coding/2026-08-22-%E6%B5%8B%E8%AF%95%20%E6%96%87%E7%AB%A0/images/%E6%9E%B6%E6%9E%84%20%E5%9B%BE.png", body_html)
        self.assertNotIn("\\", body_html)

    def test_image_remote_transformed_to_external_link(self):
        """Remote images (http/https) must be converted into external text links with rel=noopener."""
        body = (
            "![远程配图](https://example.com/assets/banner.jpg)\n\n"
            "![HTTP配图](http://example.com/pic.png \"标题提示\")"
        )
        row = self._create_mock_entry(
            rel_path="library/tech/mock-remote/entry.md",
            title="远程图片转外链测试",
            body_content=body,
        )
        output = build_html(self.tmp_dir, [row], self.labels)
        articles = self._extract_embedded_articles(output)
        body_html = articles[0]["body_html"]

        # No <img> tag for remote URLs
        self.assertNotIn("<img src=\"https://example.com", body_html)
        self.assertNotIn("<img src=\"http://example.com", body_html)

        # Transformed to <a> links with target=_blank and rel="noopener noreferrer"
        self.assertIn("<a href=\"https://example.com/assets/banner.jpg\"", body_html)
        self.assertIn("target=\"_blank\"", body_html)
        self.assertIn("rel=\"noopener noreferrer\"", body_html)
        self.assertIn("[图片: 远程配图]", body_html)
        self.assertIn("title=\"标题提示\"", body_html)

    def test_image_path_traversal_blocked(self):
        """Images attempting path traversal outside the article directory must be blocked."""
        # Create a secret file in parent dir
        secret_file = self.tmp_dir / "library/secret.png"
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_bytes(b"secret")

        body = (
            "![越界图](../secret.png)\n\n"
            "![编码越界](images/%2e%2e/%2e%2e/secret.png)"
        )
        row = self._create_mock_entry(
            rel_path="library/coding/mock-traversal/entry.md",
            title="越界图片测试",
            body_content=body,
        )
        output = build_html(self.tmp_dir, [row], self.labels)
        articles = self._extract_embedded_articles(output)
        body_html = articles[0]["body_html"]

        self.assertNotIn("<img", body_html)
        self.assertIn("[禁止的越界图片路径]", body_html)

    def test_image_absolute_path_blocked(self):
        """Images with absolute paths must be blocked."""
        body = (
            "![Linux绝对路径](/etc/passwd.png)\n\n"
            "![Windows绝对路径](C:\\images\\pic.png)"
        )
        row = self._create_mock_entry(
            rel_path="library/coding/mock-abs/entry.md",
            title="绝对路径图片测试",
            body_content=body,
        )
        output = build_html(self.tmp_dir, [row], self.labels)
        articles = self._extract_embedded_articles(output)
        body_html = articles[0]["body_html"]

        self.assertNotIn("<img", body_html)
        self.assertIn("[禁止的绝对路径图片]", body_html)

    def test_image_dangerous_protocol_blocked(self):
        """Images with data:, file:, javascript: protocols must be blocked."""
        body = (
            "![Data图片](data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=)\n\n"
            "![File协议](file:///etc/passwd)\n\n"
            "![JS协议](javascript:alert(1))"
        )
        row = self._create_mock_entry(
            rel_path="library/coding/mock-proto/entry.md",
            title="危险协议图片测试",
            body_content=body,
        )
        output = build_html(self.tmp_dir, [row], self.labels)
        articles = self._extract_embedded_articles(output)
        body_html = articles[0]["body_html"]

        self.assertNotIn("<img", body_html)

    def test_image_missing_handled_safely(self):
        """Nonexistent local images must render a safe placeholder instead of broken <img>."""
        body = "![不存在的图](images/not_found.png)"
        row = self._create_mock_entry(
            rel_path="library/coding/mock-missing/entry.md",
            title="缺失图片测试",
            body_content=body,
        )
        output = build_html(self.tmp_dir, [row], self.labels)
        articles = self._extract_embedded_articles(output)
        body_html = articles[0]["body_html"]

        self.assertNotIn("<img", body_html)
        self.assertIn("[图片缺失: 不存在的图]", body_html)

    def test_image_svg_safety(self):
        """Safe SVGs must be allowed; SVGs containing script/events must be blocked."""
        entry_dir = self.tmp_dir / "library/coding/mock-svg"
        img_dir = entry_dir / "images"
        img_dir.mkdir(parents=True, exist_ok=True)

        safe_svg = img_dir / "safe.svg"
        safe_svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><circle cx="5" cy="5" r="5"/></svg>', encoding="utf-8")

        evil_svg = img_dir / "evil.svg"
        evil_svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><circle cx="5" cy="5" r="5"/></svg>', encoding="utf-8")

        body = (
            "![安全矢量图](images/safe.svg)\n\n"
            "![恶意矢量图](images/evil.svg)"
        )
        row = self._create_mock_entry(
            rel_path="library/coding/mock-svg/entry.md",
            title="SVG 安全测试",
            body_content=body,
        )
        output = build_html(self.tmp_dir, [row], self.labels)
        articles = self._extract_embedded_articles(output)
        body_html = articles[0]["body_html"]

        self.assertIn("images/safe.svg", body_html)
        self.assertIn("[不安全的SVG图片]", body_html)
        self.assertNotIn("images/evil.svg", body_html)

    def test_link_relative_escape_root_blocked(self):
        """Non-image relative links that escape repo root must be neutralized."""
        body = (
            "[跳出项目链接](../../../../etc/passwd)\n\n"
            "[正常外部链接](https://example.com/docs)\n\n"
            "[页内锚点](#section-1)"
        )
        row = self._create_mock_entry(
            rel_path="library/coding/mock-links/entry.md",
            title="链接越界测试",
            body_content=body,
        )
        output = build_html(self.tmp_dir, [row], self.labels)
        articles = self._extract_embedded_articles(output)
        body_html = articles[0]["body_html"]

        # Traversal link neutralized to span
        self.assertIn("<span class=\"disabled-link\" title=\"链接不可用\">跳出项目链接</span>", body_html)
        # External link allowed with target=_blank
        self.assertIn("<a href=\"https://example.com/docs\" target=\"_blank\" rel=\"noopener noreferrer\">正常外部链接</a>", body_html)
        # Anchor allowed
        self.assertIn("<a href=\"#section-1\">页内锚点</a>", body_html)

    def test_json_script_escape(self):
        """Embedded JSON must escape '<' as '\\u003c' to prevent </script> tag breakout."""
        evil_title = "测试文章 </script><script>alert('pwned')</script>"
        row = self._create_mock_entry(
            rel_path="library/coding/mock-json/entry.md",
            title=evil_title,
            body_content="正常正文",
        )
        output = build_html(self.tmp_dir, [row], self.labels)

        # Check script content in raw HTML
        script_match = re.search(r'<script id="archive-data" type="application/json">(.*?)</script>', output, re.S)
        self.assertIsNotNone(script_match)
        script_body = script_match.group(1)

        self.assertNotIn("</script>", script_body)
        self.assertIn(r"\u003c/script\u003e", script_body)

        # But when parsed by JSON parser, string is preserved faithfully
        articles = json.loads(script_body)
        self.assertEqual(articles[0]["title"], evil_title)

    def test_collection_filter_manual_auto_unlabeled(self):
        """Collection must handle manual, auto, and unlabeled; unlabeled must NOT be faked as manual."""
        r1 = self._create_mock_entry("library/coding/e1/entry.md", "手动文章", "正文1", collection="manual")
        r2 = self._create_mock_entry("library/tech/e2/entry.md", "自动文章", "正文2", collection="auto")
        r3 = self._create_mock_entry("library/business/e3/entry.md", "未标记文章", "正文3", collection="")

        # Old fixture: remove collection key from row dict to verify fallback
        del r3["collection"]

        output = build_html(self.tmp_dir, [r1, r2, r3], self.labels)
        articles = self._extract_embedded_articles(output)

        col_map = {a["title"]: a["collection"] for a in articles}
        self.assertEqual(col_map["手动文章"], "manual")
        self.assertEqual(col_map["自动文章"], "auto")
        self.assertEqual(col_map["未标记文章"], "")

        # Verify collection tabs and badges in HTML
        self.assertIn("id=\"tab-all\"", output)
        self.assertIn("id=\"tab-manual\"", output)
        self.assertIn("id=\"tab-auto\"", output)

    def test_sorting_archived_descending(self):
        """Articles must be sorted by archived time descending."""
        r1 = self._create_mock_entry("library/coding/a1/entry.md", "较早文章", "正文", archived="2026-10-01T12:00:00+08:00")
        r2 = self._create_mock_entry("library/coding/a2/entry.md", "最新文章", "正文", archived="2026-10-30T12:00:00+08:00")
        r3 = self._create_mock_entry("library/coding/a3/entry.md", "中间文章", "正文", archived="2026-10-15T12:00:00+08:00")

        # Pass in unordered
        output = build_html(self.tmp_dir, [r1, r2, r3], self.labels)
        articles = self._extract_embedded_articles(output)

        titles = [a["title"] for a in articles]
        self.assertEqual(titles, ["最新文章", "中间文章", "较早文章"])

    def test_reader_warnings_and_badges(self):
        """Reader pane must support partial, published_unknown, image_note, content_note, and external link."""
        r = self._create_mock_entry(
            rel_path="library/tech/warn-entry/entry.md",
            title="警告测试文章",
            body_content="# 警告测试文章\n\n正文",
            status="partial",
            content_note="正文仅包含前半部分",
            image_note="手动补齐 3 张图片",
            published_unknown=True,
            original_title="Warning Test Article",
        )
        output = build_html(self.tmp_dir, [r], self.labels)
        articles = self._extract_embedded_articles(output)
        a = articles[0]

        self.assertEqual(a["status"], "partial")
        self.assertEqual(a["content_note"], "正文仅包含前半部分")
        self.assertEqual(a["image_note"], "手动补齐 3 张图片")
        self.assertTrue(a["published_unknown"])
        self.assertEqual(a["original_title"], "Warning Test Article")

        # Verify UI handles alert styling
        self.assertIn("alert-warning", output)
        self.assertIn("alert-info", output)
        self.assertIn("btn-external", output)

    def test_search_text_coverage(self):
        """search_text must cover Chinese title, original title, summary, tags, and body text."""
        r = self._create_mock_entry(
            rel_path="library/coding/search-entry/entry.md",
            title="独特的中文标题关键字",
            body_content="这是一段包含非常冷门词汇量子涨落的正文全文。",
            original_title="UniqueOriginalTitleEnglish",
            tags=["特别标签ABC"],
        )
        r["summary"] = "独特的摘要说明语段。"

        output = build_html(self.tmp_dir, [r], self.labels)
        articles = self._extract_embedded_articles(output)
        st = articles[0]["search_text"]

        self.assertIn("独特的中文标题关键字".lower(), st)
        self.assertIn("uniqueoriginaltitleenglish".lower(), st)
        self.assertIn("特别标签abc".lower(), st)
        self.assertIn("独特的摘要说明语段", st)
        self.assertIn("量子涨落", st)

    def test_ui_touch_target_and_accessibility(self):
        """Buttons, tabs, inputs, and selects must have >= 44px min-height and clear focus states."""
        r = self._create_mock_entry("library/coding/ui/entry.md", "UI测试", "正文")
        output = build_html(self.tmp_dir, [r], self.labels)

        # Check CSS rules
        self.assertIn("min-height: 44px;", output)
        self.assertIn(":focus-visible", output)
        self.assertIn("outline: 2px solid", output)

    def test_mobile_single_column_mode(self):
        """Responsive single column layout and back button must be defined."""
        r = self._create_mock_entry("library/coding/mobile/entry.md", "移动端测试", "正文")
        output = build_html(self.tmp_dir, [r], self.labels)

        self.assertIn("@media (max-width:", output)
        self.assertIn("mobile-back-btn", output)
        self.assertIn("mobile-reading", output)


if __name__ == "__main__":
    unittest.main()
