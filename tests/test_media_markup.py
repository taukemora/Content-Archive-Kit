#!/usr/bin/env python3
"""Markdown 媒体语义：微信/通用图注、原位置视频卡片、注入边界。"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))

import archive_url  # noqa: E402
import media_preservation  # noqa: E402
import media_markup  # noqa: E402

PAGE = "https://mp.weixin.qq.com/s/example"

WECHAT_HTML = """
<html><body>
<h1 id="activity-name">标题</h1>
<span id="js_name">账号</span>
<div id="js_content">
  <p>第一段文字。</p>
  <section style="text-align:center"><img data-src="https://mmbiz.qpic.cn/a.jpg" data-ratio="0.5"></section>
  <p style="text-align:center;line-height:1em"><span leaf=""><span style="font-size:12px;color:rgb(133,133,133)">同一个任务在 IM 群聊与 GitHub PR 之间推进</span></span></p>
  <p style="text-align:center"><span style="font-size:16px;color:rgb(0,0,0)">这只是一句普通居中短句</span></p>
  <section><img data-src="https://mmbiz.qpic.cn/b.jpg"></section>
  <p style="text-align:center;margin-bottom:8px"><span style="font-size:12px;color:#858585">第二张图的图注 <a href="https://example.com/source">图源：作者</a></span></p>
  <section><iframe class="video_iframe rich_pages" data-mpvid="wxv_4775954563285483520"
      data-src="https://mp.weixin.qq.com/mp/readtemplate?t=pages/video_player_tmpl&amp;action=mpvideo&amp;vid=wxv_4775954563285483520"
      data-cover="https%3A%2F%2Fmmbiz.qpic.cn%2Fcover.jpg"></iframe></section>
  <p>视频之后的文字。</p>
</div>
</body></html>
"""

GENERIC_HTML = """
<html><head><title>Generic</title></head><body><article>
<h2>Section</h2>
<p>""" + ("正文段落，内容足够长以通过抽取。" * 12) + """</p>
<figure><img src="https://example.com/a.jpg" alt="A"><figcaption>明确图注 <a href="https://example.com/credit">摄影：某人</a></figcaption></figure>
<p>""" + ("第二段正文，继续描述。" * 12) + """</p>
<video controls poster="https://example.com/poster.jpg"><source src="https://example.com/clip.mp4"></video>
<p>""" + ("第三段正文，收尾。" * 12) + """</p>
</article></body></html>
"""


class WechatMarkupTest(unittest.TestCase):
    def setUp(self):
        self.meta, self.content, _ = archive_url.wechat_extract(WECHAT_HTML, PAGE)

    def test_images_videos_and_captions_keep_source_order(self):
        c = self.content
        self.assertEqual(c.count("!["), 3)  # 两张正文图 + 一张视频封面
        self.assertNotIn("<iframe", c)
        self.assertNotIn("<section", c)
        self.assertIn("**视频 1（未下载）**", c)
        self.assertIn("wxv_4775954563285483520", c)
        self.assertIn(PAGE, c)
        self.assertIn("微信视频", c)
        self.assertNotIn("readtemplate", c)
        self.assertLess(c.index("images") if "images" in c else c.index("a.jpg"), c.index("图注：同一个任务"))
        self.assertLess(c.index("图注：同一个任务"), c.index("普通居中短句"))
        self.assertLess(c.index("普通居中短句"), c.index("b.jpg"))
        self.assertLess(c.index("图注：第二张图"), c.index("视频 1（未下载）"))
        self.assertLess(c.index("视频 1（未下载）"), c.index("视频之后的文字"))

    def test_real_caption_becomes_explicit_blockquote_next_to_image(self):
        c = self.content
        self.assertRegex(
            c, r"!\[[^\]]*\]\([^)]*a\.jpg\)\n\n> 图注：同一个任务在 IM 群聊与 GitHub PR 之间推进"
        )
        self.assertRegex(
            c, r"!\[[^\]]*\]\([^)]*b\.jpg\)\n\n> 图注：第二张图的图注 \[图源：作者\]\(https://example\.com/source\)"
        )

    def test_single_centered_sentence_is_not_a_caption(self):
        c = self.content
        self.assertIn("这只是一句普通居中短句", c)
        self.assertNotIn("> 图注：这只是一句普通居中短句", c)

    def test_cover_and_article_link_are_preserved_without_playback_promise(self):
        c = self.content
        self.assertIn("![视频封面](https://mmbiz.qpic.cn/cover.jpg)", c)
        self.assertIn(f"[前往原文观看]({PAGE})", c)
        self.assertNotIn("直接播放", c)
        self.assertNotIn("自动播放", c)

    def test_video_card_contains_no_raw_media_markup(self):
        c = self.content
        for bad in ("<video", "<iframe", "<mpvideo", "data-mpvid"):
            self.assertNotIn(bad, c)

    def test_arbitrary_iframe_is_removed_not_treated_as_video(self):
        html = WECHAT_HTML.replace(
            'class="video_iframe rich_pages" data-mpvid="wxv_4775954563285483520"',
            'class="map_iframe" data-src="https://example.com/map"',
        ).replace(' data-src="https://mp.weixin.qq.com/mp/readtemplate?t=pages/video_player_tmpl&amp;action=mpvideo&amp;vid=wxv_4775954563285483520"', "")
        _, content, _ = archive_url.wechat_extract(html, PAGE)
        self.assertNotIn("**视频", content)
        self.assertNotIn("<iframe", content)


class CaptionHeuristicTest(unittest.TestCase):
    def extract(self, caption_html: str) -> str:
        html = f"""
        <html><body><div id="js_content">
        <p>前文。</p>
        <section><img data-src="https://mmbiz.qpic.cn/x.jpg"></section>
        {caption_html}
        <p>后文。</p>
        </div></body></html>"""
        _, content, _ = archive_url.wechat_extract(html, PAGE)
        return content

    def test_small_grey_caption_without_center_is_a_caption(self):
        content = self.extract(
            '<p><span style="font-size:12px;color:#999999">小字灰色说明</span></p>'
        )
        self.assertIn("> 图注：小字灰色说明", content)

    def test_small_centered_caption_is_a_caption(self):
        content = self.extract(
            '<p style="text-align:center"><span style="font-size:12px;color:#000000">居中说明文字</span></p>'
        )
        self.assertIn("> 图注：居中说明文字", content)

    def test_large_grey_centered_text_is_not_a_caption(self):
        content = self.extract(
            '<p style="text-align:center"><span style="font-size:17px;color:#999999">较大的灰色居中文字</span></p>'
        )
        self.assertNotIn("> 图注：", content)

    def test_source_marker_with_center_is_a_caption(self):
        content = self.extract(
            '<p style="text-align:center"><span style="font-size:16px;color:#333333">图源：某机构</span></p>'
        )
        self.assertIn("> 图注：图源：某机构", content)

    def test_small_text_not_adjacent_to_image_is_not_a_caption(self):
        html = """
        <html><body><div id="js_content">
        <p>前文。</p>
        <section><img data-src="https://mmbiz.qpic.cn/x.jpg"></section>
        <p>中间插入的普通段落。</p>
        <p style="text-align:center"><span style="font-size:12px;color:#999999">隔了一段的小字</span></p>
        </div></body></html>"""
        _, content, _ = archive_url.wechat_extract(html, PAGE)
        self.assertNotIn("> 图注：", content)

    def test_alt_text_is_not_promoted_to_caption(self):
        html = """
        <html><body><div id="js_content">
        <section><img data-src="https://mmbiz.qpic.cn/x.jpg" alt="这是 alt 文本"></section>
        <p>后文。</p>
        </div></body></html>"""
        _, content, _ = archive_url.wechat_extract(html, PAGE)
        self.assertNotIn("> 图注：", content)


class BadgeCaptionTest(unittest.TestCase):
    """Small white-on-grey badges next to an image (conservative branch)."""

    IMAGE = '<section><img data-src="https://mmbiz.qpic.cn/badge.jpg"></section>'

    def extract(self, caption_html: str, image: str | None = None) -> str:
        html = (
            '<html><body><div id="js_content"><p>前文。</p>'
            + (self.IMAGE if image is None else image)
            + caption_html
            + "<p>后文。</p></div></body></html>"
        )
        _, content, _ = archive_url.wechat_extract(html, PAGE)
        return content

    def badge(self, style: str, text: str = "白字灰底图注") -> str:
        return f"<p style=\"{style}\">{text}</p>"

    def test_13px_white_on_grey_right_aligned_badge_is_a_caption(self):
        content = self.extract(self.badge(
            "text-align:right;font-size:13px;color:#ffffff;background-color:#8a8a8a"
        ))
        self.assertIn("> 图注：白字灰底图注", content)
        self.assertRegex(
            content,
            r"!\[[^\]]*\]\(https://mmbiz\.qpic\.cn/badge\.jpg\)\n\n> 图注：白字灰底图注",
        )
        self.assertNotIn("\n白字灰底图注", content)

    def test_14px_badge_boundary_is_accepted(self):
        content = self.extract(self.badge(
            "font-size:14px;color:#ffffff;background-color:#8a8a8a"
        ))
        self.assertIn("> 图注：白字灰底图注", content)

    def test_supported_colour_spellings_are_accepted(self):
        pairs = [
            ("white", "#8a8a8a"),
            ("#fff", "rgb(136,136,136)"),
            ("#ffffff", "#888"),
            ("rgb(255,255,255)", "rgb(136,136,136)"),
            ("rgba(255,255,255,1)", "rgba(136,136,136,1)"),
        ]
        for colour, background in pairs:
            with self.subTest(colour=colour, background=background):
                content = self.extract(self.badge(
                    f"font-size:13px;color:{colour};background-color:{background}"
                ))
                self.assertIn("> 图注：白字灰底图注", content)

    def test_inline_inheritance_within_the_same_run_is_accepted(self):
        content = self.extract(
            '<p style="text-align:right;background-color:#8a8a8a;font-size:13px">'
            '<span style="color:#ffffff">白字灰底图注</span></p>'
        )
        self.assertIn("> 图注：白字灰底图注", content)

    def test_repeat_extraction_is_stable(self):
        html = (
            '<html><body><div id="js_content"><p>前文。</p>' + self.IMAGE
            + self.badge("font-size:13px;color:#ffffff;background-color:#8a8a8a")
            + "<p>后文。</p></div></body></html>"
        )
        first = archive_url.wechat_extract(html, PAGE)[1]
        second = archive_url.wechat_extract(html, PAGE)[1]
        self.assertEqual(first, second)
        self.assertEqual(first.count("> 图注："), 1)

    def test_right_aligned_small_plain_text_is_not_a_caption(self):
        content = self.extract(self.badge("text-align:right;font-size:13px", "只是右对齐小字"))
        self.assertNotIn("> 图注：", content)
        self.assertIn("只是右对齐小字", content)

    def test_grey_background_without_white_foreground_is_not_a_caption(self):
        content = self.extract(self.badge(
            "font-size:13px;color:#333333;background-color:#8a8a8a", "灰底深灰字"
        ))
        self.assertNotIn("> 图注：", content)

    def test_white_foreground_and_grey_background_in_separate_runs_is_not_a_caption(self):
        content = self.extract(
            '<p style="text-align:right;font-size:13px">'
            '<span style="color:#ffffff">白字</span>'
            '<span style="background-color:#8a8a8a">灰底</span></p>'
        )
        self.assertNotIn("> 图注：", content)

    def test_normal_sized_white_on_grey_text_is_not_a_caption(self):
        content = self.extract(self.badge(
            "text-align:right;font-size:16px;color:#ffffff;background-color:#8a8a8a"
        ))
        self.assertNotIn("> 图注：", content)

    def test_mixed_text_sizes_are_not_a_caption(self):
        content = self.extract(
            '<p style="text-align:right;font-size:13px;color:#ffffff;background-color:#8a8a8a">'
            '白字灰底<span style="font-size:18px">大字</span></p>'
        )
        self.assertNotIn("> 图注：", content)

    def test_mixed_colours_are_not_a_caption(self):
        content = self.extract(
            '<p style="text-align:right;font-size:13px;color:#ffffff;background-color:#8a8a8a">'
            '白字灰底<span style="color:#ff0000">红字</span></p>'
        )
        self.assertNotIn("> 图注：", content)

    def test_translucent_or_transparent_styles_fail_closed(self):
        styles = [
            "font-size:13px;color:rgba(255,255,255,0.5);background-color:#8a8a8a",
            "font-size:13px;color:transparent;background-color:#8a8a8a",
            "font-size:13px;color:#ffffff;background-color:transparent",
            "font-size:13px;color:#ffffff;background-color:rgba(136,136,136,0.4)",
            "font-size:13px;color:#ffffff;background-color:#8888",
        ]
        for style in styles:
            with self.subTest(style=style):
                self.assertNotIn("> 图注：", self.extract(self.badge(style)))

    def test_unknown_colour_syntax_fails_closed(self):
        content = self.extract(self.badge(
            "font-size:13px;color:var(--badge-fg);background-color:#8a8a8a"
        ))
        self.assertNotIn("> 图注：", content)

    def test_descendant_colour_override_fails_closed(self):
        content = self.extract(
            '<p style="text-align:right;font-size:13px;color:#ffffff;background-color:#8a8a8a">'
            '白字灰底<span style="color:#333333">改写色</span></p>'
        )
        self.assertNotIn("> 图注：", content)

    def test_later_declaration_wins_in_one_style_attribute(self):
        content = self.extract(self.badge(
            "font-size:13px;color:#333333;color:#ffffff;background-color:#8a8a8a"
        ))
        self.assertIn("> 图注：白字灰底图注", content)

    def test_important_declaration_wins_over_normal_in_one_style_attribute(self):
        content = self.extract(self.badge(
            "font-size:13px;color:#333333 !important;color:#ffffff;background-color:#8a8a8a"
        ))
        self.assertNotIn("> 图注：", content)

    def test_nested_spans_with_explicit_13px_style_are_supported(self):
        # Synthetic equivalent of the real badge: p[right] > span > span[13px].
        content = self.extract(
            '<p style="text-align:right">'
            '<span><span style="font-size:13px;'
            'background-color:rgb(136,136,136);color:rgb(255,255,255)">'
            "白字灰底图注</span></span></p>"
        )
        self.assertIn("> 图注：白字灰底图注", content)

    def test_non_finite_colour_channels_fail_closed(self):
        styles = [
            "font-size:13px;color:rgb(nan,255,255);background-color:#888",
            "font-size:13px;color:rgb(255,inf,255);background-color:#888",
            "font-size:13px;color:white;background-color:rgb(136,nan,136)",
            "font-size:13px;color:white;background-color:rgb(136,136,inf)",
        ]
        for style in styles:
            with self.subTest(style=style):
                content = self.extract(self.badge(style))
                self.assertNotIn("> 图注：", content)
                self.assertIn("白字灰底图注", content)

    def test_background_shorthand_override_fails_closed(self):
        styles = [
            "font-size:13px;color:white;background-color:#888;background:red",
            "font-size:13px;color:white;background-color:#888;background:transparent",
            "font-size:13px;color:white;background:#888",
        ]
        for style in styles:
            with self.subTest(style=style):
                self.assertNotIn("> 图注：", self.extract(self.badge(style)))
        # Control: the supported longhand spelling still qualifies.
        self.assertIn(
            "> 图注：",
            self.extract(self.badge("font-size:13px;color:white;background-color:#888")),
        )

    def test_font_shorthand_override_fails_closed(self):
        styles = [
            "font-size:13px;font:20px serif;color:white;background-color:#888",
            "font:13px serif;color:white;background-color:#888",
        ]
        for style in styles:
            with self.subTest(style=style):
                self.assertNotIn("> 图注：", self.extract(self.badge(style)))

    def test_all_reset_fails_closed(self):
        styles = [
            "font-size:13px;color:white;background-color:#888;all:initial",
            "font-size:13px;color:white;background-color:#888;all:unset",
        ]
        for style in styles:
            with self.subTest(style=style):
                self.assertNotIn("> 图注：", self.extract(self.badge(style)))

    def test_shorthand_on_inner_run_cannot_borrow_outer_style(self):
        cases = [
            '<span style="background:transparent">重叠</span>',
            '<span style="all:initial">重叠</span>',
            '<span style="font:inherit">重叠</span>',
        ]
        for inner in cases:
            with self.subTest(inner=inner):
                content = self.extract(
                    '<p style="text-align:right;font-size:13px;color:#ffffff;'
                    'background-color:#8a8a8a">白字灰底' + inner + "</p>"
                )
                self.assertNotIn("> 图注：", content)

    def test_point_sizes_convert_by_96_over_72(self):
        # 13pt renders at 17.33 CSS px, which is not small text.
        self.assertNotIn(
            "> 图注：",
            self.extract(self.badge("font-size:13pt;color:white;background-color:#888")),
        )
        self.assertNotIn(
            "> 图注：",
            self.extract(self.badge("font-size:11pt;color:white;background-color:#888")),
        )
        # 9pt = 12 CSS px and 10.5pt = 14 CSS px remain inside the boundary.
        self.assertIn(
            "> 图注：",
            self.extract(self.badge("font-size:9pt;color:white;background-color:#888")),
        )
        self.assertIn(
            "> 图注：",
            self.extract(self.badge("font-size:10.5pt;color:white;background-color:#888")),
        )

    def test_zero_and_non_positive_sizes_fail_closed(self):
        styles = [
            "font-size:0px;color:white;background-color:#888",
            "font-size:0.0px;color:white;background-color:#888",
            "font-size:0pt;color:white;background-color:#888",
            "font-size:-13px;color:white;background-color:#888",
        ]
        for style in styles:
            with self.subTest(style=style):
                self.assertNotIn("> 图注：", self.extract(self.badge(style)))

    def test_em_and_rem_sizes_are_unsupported_in_the_badge_branch(self):
        # Rendered parent/root size is unknown here, so 16px cannot be assumed.
        styles = [
            "font-size:0.8em;color:white;background-color:#888",
            "font-size:0.8rem;color:white;background-color:#888",
            "font-size:1em;color:white;background-color:#888",
        ]
        for style in styles:
            with self.subTest(style=style):
                self.assertNotIn("> 图注：", self.extract(self.badge(style)))
        # The legacy grey/centred heuristic keeps its historical em handling.
        legacy = self.extract(
            '<p style="text-align:center"><span style="font-size:0.8em;'
            'color:rgb(133,133,133)">灰色说明</span></p>'
        )
        self.assertIn("> 图注：", legacy)

    def test_heading_is_not_a_white_on_grey_caption(self):
        content = self.extract(
            '<h3 style="font-size:13px;color:#ffffff;background-color:#8a8a8a">标题文字</h3>'
        )
        self.assertNotIn("> 图注：", content)
        self.assertIn("### 标题文字", content)

    def test_blockquote_or_list_is_not_a_white_on_grey_caption(self):
        cases = [
            '<blockquote style="font-size:13px;color:#ffffff;background-color:#8a8a8a">引用文字</blockquote>',
            '<ul style="font-size:13px;color:#ffffff;background-color:#8a8a8a"><li>列表项</li></ul>',
        ]
        for html in cases:
            with self.subTest(html=html):
                self.assertNotIn("> 图注：", self.extract(html))

    def test_badge_not_adjacent_to_image_is_not_a_caption(self):
        content = self.extract(
            "<p>中间插入的普通段落。</p>"
            + self.badge("font-size:13px;color:#ffffff;background-color:#8a8a8a")
        )
        self.assertNotIn("> 图注：", content)

    def test_badge_content_stays_inert(self):
        content = self.extract(
            '<p style="font-size:13px;color:#ffffff;background-color:#8a8a8a">'
            '坏&lt;script&gt;alert(1)&lt;/script&gt; '
            '<a href="javascript:alert(2)">点我</a></p>'
        )
        self.assertNotIn("<script", content)
        self.assertNotIn("javascript:", content)
        self.assertIn("> 图注：", content)


class GenericMarkupTest(unittest.TestCase):
    def test_explicit_figure_figcaption_is_preserved(self):
        meta, content = archive_url.generic_extract(GENERIC_HTML, "https://example.com/post")
        self.assertIn("![A](https://example.com/a.jpg)", content)
        self.assertRegex(
            content,
            r"!\[A\]\(https://example\.com/a\.jpg\)\n\n> 图注：明确图注 \[摄影：某人\]\(https://example\.com/credit\)",
        )
        self.assertNotIn("<figcaption", content)
        self.assertNotIn("图注：图注：", content)

    def test_standard_video_card_keeps_position_and_safe_links(self):
        meta, content = archive_url.generic_extract(GENERIC_HTML, "https://example.com/post")
        self.assertIn("**视频 1（未下载）**", content)
        self.assertIn("![视频封面](https://example.com/poster.jpg)", content)
        self.assertIn("[前往原文观看](https://example.com/post)", content)
        self.assertIn("[打开视频地址](https://example.com/clip.mp4)", content)
        self.assertIn("视频本体未下载", content)
        self.assertNotIn("<video", content)
        first_body = content.index("正文段落")
        video_at = content.index("视频 1（未下载）")
        after_second = content.index("第三段正文")
        self.assertLess(first_body, video_at)
        self.assertLess(video_at, after_second)

    def test_generic_without_figure_does_not_guess_captions(self):
        html = """<html><head><title>T</title></head><body><article>
        <p>""" + ("普通正文段落。" * 30) + """</p>
        <p><img src="https://example.com/plain.jpg"></p>
        <p style="text-align:center"><span style="font-size:12px;color:#999999">图片后面的小字</span></p>
        <p>""" + ("结尾正文。" * 30) + """</p></article></body></html>"""
        _, content = archive_url.generic_extract(html, "https://example.com/p")
        self.assertNotIn("> 图注：", content)

    def test_youtube_component_is_inline_and_appendix_does_not_duplicate_it(self):
        html = """
        <html><head><title>G</title></head><body><article>
        <p>""" + ("Google 正文段落。" * 30) + """</p>
        <uni-youtube-player-article video-id="oM5QbIJzeH0" page-title="Beam video"></uni-youtube-player-article>
        <p>""" + ("后续正文。" * 30) + """</p>
        </article></body></html>"""
        _, content = archive_url.generic_extract(html, "https://blog.google/x")
        self.assertIn("**视频 1（未下载）**", content)
        self.assertIn("https://www.youtube.com/watch?v=oM5QbIJzeH0", content)
        once, report = media_preservation.preserve_media(content, html, "https://blog.google/x")
        self.assertEqual(once, content)
        self.assertTrue(report["video_already_present"])
        self.assertFalse(report["appended"])


class InjectionTest(unittest.TestCase):
    def test_caption_html_is_inert_markdown(self):
        html = """
        <html><body><div id="js_content">
        <section><img data-src="https://mmbiz.qpic.cn/x.jpg"></section>
        <p style="text-align:center"><span style="font-size:12px;color:#999999">坏&lt;script&gt;alert(1)&lt;/script&gt; <a href="javascript:alert(2)">点我</a></span></p>
        </div></body></html>"""
        _, content, _ = archive_url.wechat_extract(html, PAGE)
        self.assertNotIn("<script", content)
        self.assertNotIn("javascript:", content)
        self.assertIn("> 图注：", content)

    def test_unsafe_cover_url_is_not_emitted_as_image(self):
        html = WECHAT_HTML.replace(
            "https%3A%2F%2Fmmbiz.qpic.cn%2Fcover.jpg",
            "javascript%3Aalert(1)",
        )
        _, content, _ = archive_url.wechat_extract(html, PAGE)
        self.assertNotIn("javascript:", content)
        self.assertNotIn("![视频封面]", content)


class CaptionLinkResolutionTest(unittest.TestCase):
    """Relative attribution links resolve against the article URL; unsafe ones die."""

    FIGURE = (
        '<figure><img src="https://example.com/a.png">'
        '<figcaption>图源 <a href="/credits">摄影师</a></figcaption></figure>'
    )

    def test_wechat_explicit_figcaption_resolves_relative_link(self):
        _, content, _ = archive_url.wechat_extract(
            '<div id="js_content">' + self.FIGURE + "</div>", PAGE
        )
        self.assertIn("> 图注：图源 [摄影师](https://mp.weixin.qq.com/credits)", content)

    def test_wechat_heuristic_caption_resolves_relative_link(self):
        html = f"""
        <html><body><div id="js_content">
        <section><img data-src="https://mmbiz.qpic.cn/x.jpg"></section>
        <p style="text-align:center"><span style="font-size:12px;color:#999999">图源 <a href="../credits">摄影师</a></span></p>
        </div></body></html>"""
        _, content, _ = archive_url.wechat_extract(html, PAGE)
        self.assertIn("(https://mp.weixin.qq.com/credits)", content)
        self.assertIn("[摄影师]", content)

    def test_generic_figcaption_resolves_relative_link(self):
        _, content = archive_url.generic_extract(
            "<html><body><article><p>" + ("正文。" * 80) + "</p>"
            + self.FIGURE + "</article></body></html>",
            "https://example.com/post/story",
        )
        self.assertIn("(https://example.com/credits)", content)

    def test_unsafe_caption_links_are_still_stripped(self):
        figure = (
            '<figure><img src="https://example.com/a.png"><figcaption>'
            '<a href="javascript:alert(1)">js</a> '
            '<a href="data:text/html,x">data</a> '
            '<a href="/credits">保留</a></figcaption></figure>'
        )
        _, content, _ = archive_url.wechat_extract(
            '<div id="js_content">' + figure + "</div>", PAGE
        )
        self.assertNotIn("javascript:", content)
        self.assertNotIn("data:text", content)
        self.assertIn("[保留](https://mp.weixin.qq.com/credits)", content)


class CaptionFidelityRegressionTest(unittest.TestCase):
    def test_literal_stars_and_em_emphasis_survive(self):
        html = (
            '<div id="js_content"><figure>'
            '<img src="https://example.com/a.png">'
            "<figcaption>原文有 *stars* 与 _under_ 和 <em>重点</em></figcaption>"
            "</figure></div>"
        )
        _, content, _ = archive_url.wechat_extract(html, PAGE)
        self.assertIn(r"\*stars\*", content)
        self.assertIn(r"\_under\_", content)
        self.assertIn("*重点*", content)


class _FakeImageResponse:
    headers: dict = {}

    def iter_content(self, size=1, decode_unicode=False):
        yield b"\x89PNG\r\n\x1a\n" + b"0" * 16

    def close(self):
        pass


class VideoIdentifierInertTest(unittest.TestCase):
    def test_markdown_payload_in_video_id_cannot_produce_media(self):
        value = (
            'wxv_1 ![injected](https://example.com/unintended.png) '
            "[fake](https://example.com/fake) <b>h</b> *a* _b_ &amp; (p) "
            "&amp;#33;[ent](https://example.com/entity.png)"
        )
        html = (
            '<div id="js_content"><p>前段</p>'
            f'<iframe class="video_iframe" data-mpvid="{value}"></iframe>'
            "<p>后段</p></div>"
        )
        _, content, _ = archive_url.wechat_extract(html, PAGE)
        self.assertNotIn("![injected](", content)
        self.assertNotIn("[fake](", content)

        from build_html import _create_markdown_renderer

        body = BeautifulSoup(_create_markdown_renderer().render(content), "html.parser")
        card = body.find("aside", class_="video-card")
        self.assertIsNotNone(card)
        self.assertEqual(len(card.select("a")), 1)  # 只有原文观看链接
        self.assertEqual(len(card.select("img")), 0)
        self.assertIsNone(card.find("a", href="https://example.com/fake"))
        text = card.get_text(" ", strip=True)
        for fragment in (
            "![injected](https://example.com/unintended.png)",
            "[fake](https://example.com/fake)",
            "<b>h</b>",
            "*a*",
            "_b_",
            "&",
            "(p)",
            "&#33;[ent](https://example.com/entity.png)",
        ):
            self.assertIn(fragment, text)

    def test_card_urls_keep_parens_and_backslashes_inside_link_targets(self):
        import media_markup

        card = media_markup._render_video_card({
            "index": 1,
            "cover_url": "https://example.com/cover(1).jpg",
            "watch_url": "https://example.com/a(b)\\c",
            "media_url": "",
            "watch_label": "前往原文观看",
            "note": "说明",
        })
        self.assertIn("](https://example.com/cover%281%29.jpg)", card)
        self.assertIn("](https://example.com/a%28b%29%5Cc)", card)

    def test_localize_images_never_fetches_attack_url(self):
        value = "wxv_123![injected](https://example.com/unintended.png)"
        html = (
            '<div id="js_content">'
            '<p><img data-src="https://example.com/ok.png"></p>'
            f'<iframe class="video_iframe" data-mpvid="{value}"></iframe>'
            "</div>"
        )
        _, content, _ = archive_url.wechat_extract(html, PAGE)
        called: list[str] = []

        def fake_fetch(url, **kwargs):
            called.append(url)
            return _FakeImageResponse()

        with tempfile.TemporaryDirectory(dir=str(_support.SCRATCH)) as tmp:
            with patch.object(archive_url, "safe_fetch", side_effect=fake_fetch):
                out, ok, failed, notes = archive_url.localize_images(
                    content, PAGE, Path(tmp) / "images"
                )
        self.assertNotIn("https://example.com/unintended.png", called)
        self.assertEqual(called, ["https://example.com/ok.png"])
        self.assertEqual((ok, failed), (1, 0), notes)
        self.assertIn("images/001.png", out)

    def test_legitimate_wechat_id_stays_readable(self):
        _, content, _ = archive_url.wechat_extract(WECHAT_HTML, PAGE)
        self.assertIn("视频标识：wxv_4775954563285483520", content)


if __name__ == "__main__":
    unittest.main()
