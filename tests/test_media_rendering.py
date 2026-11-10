#!/usr/bin/env python3
"""阅读器渲染：figure/figcaption、视频卡片、HTML 关闭与链接安全。"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))

from build_html import build_html  # noqa: E402
from common import write_entry  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class MediaRenderingTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=str(_support.SCRATCH), prefix="media_render_"))
        self.entry = self.root / "library" / "tech" / "entry-one"
        (self.entry / "images").mkdir(parents=True)
        (self.entry / "images" / "001.png").write_bytes(PNG)
        (self.entry / "images" / "002.png").write_bytes(PNG)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def build(self, body: str, entry_id: str = "entry-one") -> str:
        path = self.entry / "entry.md"
        write_entry(path, {
            "id": entry_id,
            "title": "标题",
            "original_title": "Title",
            "summary_chars": 10,
            "status": "archived",
            "category": "tech",
            "context": "work",
            "url": "https://example.com/post",
            "images": 2,
            "image_note": "",
        }, "# 标题\n\n## 摘要\n\n摘要。\n\n## 全文\n\n" + body + "\n")
        rows = [{
            "id": entry_id,
            "path": str(path.relative_to(self.root)),
            "title": "标题",
            "summary": "摘要。",
            "category": "tech",
            "context": "work",
            "status": "archived",
            "url": "https://example.com/post",
            "archived": "2026-11-10T00:00:00+08:00",
        }]
        return build_html(self.root, rows, {"tech": "科技"})

    def body(self, html: str) -> BeautifulSoup:
        page = BeautifulSoup(html, "html.parser")
        script = page.find("script", id="archive-data")
        if script is None:
            self.fail("archive-data script missing")
        import json

        data = json.loads(script.string or "[]")
        self.assertTrue(data and data[0].get("body_html"), "body_html missing")
        return BeautifulSoup(data[0]["body_html"], "html.parser")

    def test_image_with_explicit_caption_renders_figure_and_figcaption(self):
        html = self.build(
            "前文。\n\n![](images/001.png)\n\n> 图注：同一个任务在 IM 群聊与 GitHub PR 之间推进\n\n后文。"
        )
        body = self.body(html)
        figure = body.find("figure", class_="article-figure")
        self.assertIsNotNone(figure)
        self.assertEqual(figure.find("img")["src"], "library/tech/entry-one/images/001.png")
        caption = figure.find("figcaption")
        self.assertIsNotNone(caption)
        self.assertEqual(caption.get_text(strip=True), "同一个任务在 IM 群聊与 GitHub PR 之间推进")
        self.assertEqual(body.find_all("figure"), [figure])
        self.assertNotIn("图注：同一个任务", body.get_text(" ", strip=True))

    def test_long_caption_gets_left_aligned_class(self):
        long_text = "这是一段很长的图注" * 6
        html = self.build(f"![](images/001.png)\n\n> 图注：{long_text}")
        caption = self.body(html).find("figcaption")
        self.assertIsNotNone(caption)
        self.assertIn("long", caption.get("class") or [])
        self.assertEqual(caption.get_text(strip=True), long_text)

    def test_short_caption_has_no_long_class(self):
        html = self.build("![](images/001.png)\n\n> 图注：短片说明")
        caption = self.body(html).find("figcaption")
        self.assertIsNotNone(caption)
        self.assertNotIn("long", caption.get("class") or [])

    def test_video_card_renders_as_aside_and_keeps_safe_links(self):
        html = self.build(
            "> **视频 1（未下载）**\n"
            ">\n"
            "> 封面：![视频封面](images/002.png)\n"
            ">\n"
            "> 观看：[前往原文观看](https://example.com/post)\n"
            ">\n"
            "> 说明：微信视频未下载；归档页不能播放，请前往原文观看。"
        )
        body = self.body(html)
        card = body.find("aside", class_="video-card")
        self.assertIsNotNone(card)
        self.assertIn("视频 1（未下载）", card.get_text(" ", strip=True))
        img = card.find("img")
        self.assertEqual(img["src"], "library/tech/entry-one/images/002.png")
        link = card.find("a", href="https://example.com/post")
        self.assertIsNotNone(link)
        self.assertEqual(link.get("rel"), ["noopener", "noreferrer"])
        self.assertEqual(body.find_all("blockquote"), [])

    def test_ordinary_blockquote_is_not_turned_into_video_card(self):
        html = self.build("> **视频** 这一段不是媒体卡片。")
        body = self.body(html)
        self.assertIsNone(body.find("aside", class_="video-card"))
        self.assertIsNotNone(body.find("blockquote"))

    def test_remote_cover_is_a_safe_external_link_not_an_image_request(self):
        html = self.build(
            "> **视频 1（未下载）**\n>\n> 封面：![视频封面](https://cdn.example.com/cover.jpg)"
        )
        body = self.body(html)
        self.assertIsNone(body.find("img", src=lambda v: v and v.startswith("http")))
        link = body.find("a", class_="ext-img-link")
        self.assertIsNotNone(link)
        self.assertEqual(link["href"], "https://cdn.example.com/cover.jpg")

    def test_caption_html_and_unsafe_link_are_inert(self):
        html = self.build(
            "![](images/001.png)\n\n> 图注：坏<script>alert(1)</script> [点我](javascript:alert(2))"
        )
        body = self.body(html)
        self.assertEqual(body.find("script"), None)
        self.assertIsNone(body.find("a", href=lambda v: v and "javascript" in v.lower()))
        caption = body.find("figcaption")
        self.assertIn("坏", caption.get_text(" ", strip=True))

    def test_raw_html_in_body_stays_escaped(self):
        html = self.build('<img src=x onerror="alert(1)">\n\n普通段落。')
        body = self.body(html)
        self.assertIsNone(body.find("img", attrs={"onerror": True}))
        self.assertIn("&lt;img", str(body))

    def test_caption_link_is_preserved_with_safe_attributes(self):
        html = self.build(
            "![](images/001.png)\n\n> 图注：图源 [作者](https://example.com/source)"
        )
        body = self.body(html)
        caption = body.find("figcaption")
        link = caption.find("a", href="https://example.com/source")
        self.assertIsNotNone(link)
        self.assertEqual(link.get("rel"), ["noopener", "noreferrer"])

    def test_image_group_shares_one_caption_in_one_figure(self):
        html = self.build(
            "![](images/001.png)![](images/002.png)\n\n> 图注：两幅对比"
        )
        body = self.body(html)
        figures = body.find_all("figure", class_="article-figure")
        self.assertEqual(len(figures), 1)
        self.assertEqual(len(figures[0].find_all("img")), 2)
        caption = figures[0].find("figcaption")
        self.assertIsNotNone(caption)
        self.assertEqual(caption.get_text(strip=True), "两幅对比")
        self.assertEqual(body.find_all("blockquote"), [])

    def test_image_group_with_whitespace_and_softbreak_still_figures(self):
        html = self.build(
            "![](images/001.png) \n![](images/002.png)\n\n> 图注：同一组"
        )
        body = self.body(html)
        figure = body.find("figure", class_="article-figure")
        self.assertIsNotNone(figure)
        self.assertEqual(len(figure.find_all("img")), 2)
        self.assertEqual(figure.find("figcaption").get_text(strip=True), "同一组")

    def test_paragraph_with_prose_and_image_is_not_swallowed(self):
        html = self.build(
            "说明文字 ![](images/001.png)\n\n> 图注：普通引用不应变成图注"
        )
        body = self.body(html)
        self.assertIsNone(body.find("figure", class_="article-figure"))
        self.assertIsNotNone(body.find("blockquote"))
        self.assertIn("说明文字", body.get_text(" ", strip=True))

    def test_ordinary_blockquote_after_image_stays_a_quote(self):
        html = self.build("![](images/001.png)\n\n> 普通引用文字")
        body = self.body(html)
        self.assertIsNone(body.find("figure", class_="article-figure"))
        quote = body.find("blockquote")
        self.assertIsNotNone(quote)
        self.assertIn("普通引用文字", quote.get_text(" ", strip=True))

    def test_caption_keeps_literal_stars_and_explicit_emphasis(self):
        html = self.build(
            "![](images/001.png)\n\n"
            "> 图注：原文 \\*stars\\* 与 \\_under\\_ 和 *重点*"
        )
        caption = self.body(html).find("figcaption")
        self.assertIsNotNone(caption)
        text = caption.get_text(" ", strip=True)
        self.assertIn("*stars*", text)
        self.assertIn("_under_", text)
        emphasis = caption.find("em")
        self.assertIsNotNone(emphasis)
        self.assertEqual(emphasis.get_text(strip=True), "重点")

    def test_template_has_media_styles(self):
        css = (ROOT / "scripts" / "archive_page.html").read_text(encoding="utf-8")
        self.assertIn(".article-figure", css)
        self.assertIn("figcaption", css)
        self.assertIn(".video-card", css)


if __name__ == "__main__":
    unittest.main()
