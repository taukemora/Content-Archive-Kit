#!/usr/bin/env python3
"""文章 HTML 强调归一化：跨块强调、内联 CSS 粗细/斜体与 normal 覆盖。"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))

import archive_url  # noqa: E402
import article_formatting  # noqa: E402

PAGE = "https://mp.weixin.qq.com/s/formatting"
GENERIC_PAGE = "https://example.com/post"


def _wechat(inner: str, root_style: str | None = None) -> str:
    style = f' style="{root_style}"' if root_style else ""
    html = f'<html><body><div id="js_content"{style}>{inner}</div></body></html>'
    return archive_url.wechat_extract(html, PAGE)[1]


def _generic(extra: str) -> str:
    filler = "<p>" + ("正文段落，内容足够长，用于通过抽取。" * 12) + "</p>"
    html = (
        "<html><head><title>格式</title></head><body><article>"
        f"{filler}{extra}{filler}</article></body></html>"
    )
    return archive_url.generic_extract(html, GENERIC_PAGE)[1]


def _rendered(content: str):
    from build_html import _create_markdown_renderer

    return BeautifulSoup(_create_markdown_renderer().render(content), "html.parser")


class StyleParsingTest(unittest.TestCase):
    def test_weight_and_style_keywords_and_numbers(self):
        parse = article_formatting.parse_inline_style
        self.assertEqual(parse("font-weight: bold"), (True, None))
        self.assertEqual(parse("font-weight:700"), (True, None))
        self.assertEqual(parse("FONT-WEIGHT: 600 ; font-style: italic"), (True, True))
        self.assertEqual(parse("font-weight: normal"), (False, None))
        self.assertEqual(parse("font-weight:400;font-style:normal"), (False, False))
        self.assertEqual(parse("font-weight: 900 !important"), (True, None))
        self.assertEqual(parse("color:red"), (None, None))
        self.assertEqual(parse(""), (None, None))
        self.assertEqual(parse(None), (None, None))

    def test_later_declaration_wins_and_garbage_is_ignored(self):
        parse = article_formatting.parse_inline_style
        self.assertEqual(parse("font-weight:bold;font-weight:normal"), (False, None))
        self.assertEqual(parse("font-weight:banana"), (None, None))
        self.assertEqual(parse("font-weight:bold;url(javascript:alert(1))"), (True, None))

    def test_important_declarations_beat_later_unimportant_ones(self):
        parse = article_formatting.parse_inline_style
        self.assertEqual(
            parse("font-weight:bold!important;font-weight:normal"), (True, None)
        )
        self.assertEqual(
            parse("font-weight:normal!important;font-weight:bold"), (False, None)
        )
        self.assertEqual(
            parse("font-weight:bold!important;font-weight:normal!important"),
            (False, None),
        )
        self.assertEqual(
            parse("font-weight:bold ! IMPORTANT;font-weight:normal"), (True, None)
        )
        self.assertEqual(
            parse("font-style:italic!important;font-style:normal"), (None, True)
        )

    def test_invalid_duplicate_declarations_do_not_erase_a_valid_one(self):
        parse = article_formatting.parse_inline_style
        self.assertEqual(
            parse("font-weight:bold!important;font-weight:banana"), (True, None)
        )
        self.assertEqual(
            parse("font-weight:bold;font-weight:900px"), (True, None)
        )


class InlineLinkEmphasisTest(unittest.TestCase):
    INNER = (
        '<p><strong>before <a href="https://example.com">bold'
        '<span style="font-weight:normal">plain</span></a> after</strong></p>'
    )

    def test_normal_override_inside_link_stays_unbold(self):
        content = _wechat(self.INNER)
        self.assertEqual(content.count("https://example.com"), 1)
        body = _rendered(content)
        links = body.find_all("a")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].get("href"), "https://example.com")
        self.assertEqual(links[0].get_text(), "boldplain")
        self.assertEqual(
            [tag.get_text() for tag in links[0].find_all("strong")], ["bold"]
        )
        bold_texts = [tag.get_text() for tag in body.find_all("strong")]
        self.assertNotIn("plain", bold_texts)
        self.assertNotIn("boldplain", bold_texts)
        self.assertIn("before", "".join(bold_texts))
        self.assertIn("after", "".join(bold_texts))

    def test_link_text_keeps_its_words_and_url_once(self):
        content = _wechat(self.INNER)
        body = _rendered(content)
        self.assertEqual(re.sub(r"\s+", " ", body.get_text()).strip(), "before boldplain after")
        link = body.find("a")
        assert link is not None
        self.assertIn("bold", link.get_text())
        self.assertNotIn("**", body.get_text())


class RootStyleInheritanceTest(unittest.TestCase):
    def test_root_style_bold_reaches_plain_paragraph(self):
        content = _wechat("<p>alpha</p>", root_style="font-weight:bold")
        self.assertIn("**alpha**", content)
        body = _rendered(content)
        self.assertEqual([tag.get_text() for tag in body.find_all("strong")], ["alpha"])

    def test_descendant_normal_can_override_root_bold_style(self):
        content = _wechat(
            '<p>alpha <span style="font-weight:normal">beta</span></p>',
            root_style="font-weight:bold",
        )
        body = _rendered(content)
        self.assertEqual(
            [tag.get_text() for tag in body.find_all("strong")], ["alpha"]
        )
        self.assertNotIn("beta", "".join(tag.get_text() for tag in body.find_all("strong")))

    def test_block_normal_overrides_root_bold_style(self):
        content = _wechat(
            '<p style="font-weight:normal">alpha</p>', root_style="font-weight:bold"
        )
        body = _rendered(content)
        self.assertEqual(body.find_all("strong"), [])
        self.assertEqual(body.get_text(strip=True), "alpha")


class ImportantCascadeTest(unittest.TestCase):
    def test_important_bold_beats_later_plain_normal(self):
        content = _wechat(
            "<p style='font-weight:bold!important;font-weight:normal'>Alpha</p>"
        )
        self.assertIn("**Alpha**", content)
        body = _rendered(content)
        self.assertEqual([tag.get_text() for tag in body.find_all("strong")], ["Alpha"])

    def test_important_normal_beats_plain_bold(self):
        content = _wechat(
            "<p style='font-weight:normal!important;font-weight:bold'>Alpha</p>"
        )
        body = _rendered(content)
        self.assertEqual(body.find_all("strong"), [])
        self.assertEqual(body.get_text(strip=True), "Alpha")

    def test_last_important_declaration_wins(self):
        content = _wechat(
            "<p style='font-weight:bold!important;font-weight:normal!important'>Alpha</p>"
        )
        body = _rendered(content)
        self.assertEqual(body.find_all("strong"), [])
        self.assertEqual(body.get_text(strip=True), "Alpha")


class WechatCssEmphasisTest(unittest.TestCase):
    def test_inline_css_bold_becomes_strong(self):
        content = _wechat(
            '<p><span style="font-size:16px;font-weight:bold">重点结论</span></p>'
        )
        self.assertIn("**重点结论**", content)
        self.assertNotIn("font-weight", content)
        self.assertNotIn("<span", content)
        body = _rendered(content)
        self.assertEqual([s.get_text() for s in body.find_all("strong")], ["重点结论"])

    def test_numeric_weight_and_css_italic(self):
        content = _wechat(
            '<p><span style="font-weight:700;font-style:italic">斜体重</span></p>'
        )
        self.assertIn("***斜体重***", content)

    def test_css_italic_and_normal_override(self):
        content = _wechat(
            '<p><span style="font-style:italic">斜体字</span> '
            '<em style="font-style:normal">非斜体</em> '
            "<em>真斜体</em></p>"
        )
        self.assertIn("*斜体字*", content)
        self.assertIn("非斜体", content)
        self.assertNotIn("*非斜体*", content)
        self.assertIn("*真斜体*", content)

    def test_normal_override_ends_bold_run(self):
        content = _wechat(
            '<p><strong>粗体<span style="font-weight:normal">正常</span>粗体</strong></p>'
        )
        self.assertIn("**粗体**正常**粗体**", content)
        self.assertNotIn("**粗体正常粗体**", content)

    def test_nested_duplicate_bold_is_not_doubled(self):
        content = _wechat(
            "<p><strong><b>只加粗一次</b></strong></p>"
            '<p><b><span style="font-weight:bold">也只一次</span></b></p>'
        )
        self.assertIn("**只加粗一次**", content)
        self.assertIn("**也只一次**", content)
        self.assertEqual(content.count("**"), 4)

    def test_short_standalone_bold_stays_emphasis_not_heading(self):
        content = _wechat("<p><strong>千万注意</strong></p>")
        self.assertIn("**千万注意**", content)
        self.assertNotIn("##", content)
        self.assertNotIn("#", content)


class CrossBlockEmphasisTest(unittest.TestCase):
    INNER = (
        "<p>前文。</p>"
        "<strong>"
        "<section><p>Writer</p></section>"
        "<section><p>Editor</p></section>"
        "<section><p>Images</p></section>"
        "</strong>"
        "<p>后文。</p>"
    )

    def test_each_block_keeps_its_own_emphasis(self):
        content = _wechat(self.INNER)
        for word in ("Writer", "Editor", "Images"):
            self.assertIn(f"**{word}**", content)
        self.assertNotRegex(content, r"\*\*Writer\s*\n\s*\n")
        self.assertNotIn("**Writer\n", content)
        self.assertEqual(content.count("**"), 6)
        for line in content.splitlines():
            self.assertEqual(line.count("**") % 2, 0, line)
        self.assertNotIn(r"\*\*", content)  # no literal escaped stars

    def test_rendered_blocks_are_independently_bold(self):
        body = _rendered(_wechat(self.INNER))
        self.assertEqual([s.get_text() for s in body.find_all("strong")], ["Writer", "Editor", "Images"])
        self.assertNotIn("**", body.get_text())

    def test_blocks_without_paragraph_wrappers(self):
        content = _wechat("<strong><section>Alpha</section><section>Beta</section></strong>")
        self.assertIn("**Alpha**", content)
        self.assertIn("**Beta**", content)
        self.assertNotIn("**Alpha\n", content)

    def test_strong_span_wrapping_sections_is_flattened(self):
        content = _wechat(
            '<span style="font-weight:bold"><section>甲</section><section>乙</section></span>'
        )
        self.assertIn("**甲**", content)
        self.assertIn("**乙**", content)


class HeadingPreservationTest(unittest.TestCase):
    def test_native_heading_levels_survive(self):
        content = _wechat(
            "<h1>一级标题</h1><h2>二级标题</h2><h4>四级标题</h4>"
            "<p>正文。</p>"
        )
        self.assertIn("# 一级标题", content)
        self.assertIn("## 二级标题", content)
        self.assertIn("#### 四级标题", content)

    def test_bold_inside_heading_does_not_add_stars(self):
        content = _wechat("<h2><strong>二级标题</strong></h2>")
        self.assertIn("## 二级标题", content)
        self.assertNotIn("**", content)

    def test_short_bold_paragraph_is_not_promoted(self):
        content = _wechat("<p><b>短促</b></p><p>另一段。</p>")
        self.assertIn("**短促**", content)
        self.assertNotRegex(content, r"(?m)^#+\s")


class UnicodeFlankingEmphasisTest(unittest.TestCase):
    """Unicode punctuation at an emphasis edge must not emit literal stars.

    CommonMark refuses a closing delimiter preceded by punctuation and followed
    by a letter (and the mirrored opening case).  The boundary punctuation is
    moved outside the emphasis range; visible text and word order are unchanged.
    """

    def _assert_core(self, inner: str, tag: str, core: str, visible: str) -> None:
        body = _rendered(_wechat(inner))
        self.assertEqual(body.get_text().strip(), visible)
        self.assertNotIn("*", body.get_text())
        node = body.find(tag)
        assert node is not None
        self.assertEqual(node.get_text(), core)

    def test_trailing_punctuation_moves_out_of_strong_and_em(self):
        for tag in ("strong", "em"):
            with self.subTest(tag=tag):
                self._assert_core(
                    f"<p><{tag}>文｜</{tag}>合成作者</p>", tag, "文", "文｜合成作者"
                )

    def test_leading_punctuation_moves_out(self):
        for tag in ("strong", "em"):
            with self.subTest(tag=tag):
                self._assert_core(
                    f"<p>前文<{tag}>，强调开始</{tag}>后文</p>",
                    tag,
                    "强调开始",
                    "前文，强调开始后文",
                )

    def test_colon_suffix_and_both_mirrors(self):
        self._assert_core(
            "<p><strong>编辑：</strong>正文继续。</p>", "strong", "编辑", "编辑：正文继续。"
        )
        self._assert_core(
            "<p>前文<em>，重点：</em>后文</p>", "em", "重点", "前文，重点：后文"
        )

    def test_css_only_emphasis_uses_the_same_boundary_repair(self):
        body = _rendered(
            _wechat(
                '<p><span style="font-weight:700">文｜</span>合成作者</p>'
                '<p><span style="font-style:italic">编辑：</span>正文继续。</p>'
            )
        )
        self.assertEqual(
            re.sub(r"\s+", "", body.get_text()), "文｜合成作者编辑：正文继续。"
        )
        self.assertEqual([tag.get_text() for tag in body.find_all("strong")], ["文"])
        self.assertEqual([tag.get_text() for tag in body.find_all("em")], ["编辑"])

    def test_boundary_already_valid_for_commonmark_is_untouched(self):
        body = _rendered(_wechat("<p><strong>文｜</strong>，后续</p>"))
        self.assertEqual(body.get_text().strip(), "文｜，后续")
        strong = body.find("strong")
        assert strong is not None
        self.assertEqual(strong.get_text(), "文｜")

    def test_nested_link_keeps_its_text_and_href(self):
        body = _rendered(
            _wechat(
                '<p>前文<a href="https://example.com/x"><strong>，链接重点</strong></a>后文</p>'
            )
        )
        self.assertEqual(body.get_text().strip(), "前文，链接重点后文")
        link = body.find("a")
        assert link is not None
        self.assertEqual(link.get("href"), "https://example.com/x")
        self.assertEqual(link.get_text(), "，链接重点")
        strong = link.find("strong")
        assert strong is not None
        self.assertEqual(strong.get_text(), "链接重点")

    def test_punctuation_only_emphasis_degrades_to_plain_text(self):
        body = _rendered(_wechat("<p><strong>。！</strong>正文</p>"))
        self.assertEqual(body.get_text().strip(), "。！正文")
        self.assertNotIn("*", body.get_text())
        self.assertIsNone(body.find("strong"))

    def test_code_and_escaped_literals_are_not_relocated(self):
        body = _rendered(
            _wechat(
                "<p><strong>文｜</strong>合成 <code>｜代码｜</code> 与 *星号* 文本。</p>"
            )
        )
        self.assertEqual(body.get_text().strip(), "文｜合成 ｜代码｜ 与 *星号* 文本。")
        code = body.find("code")
        assert code is not None
        self.assertEqual(code.get_text(), "｜代码｜")


class UnicodeFlankingIdempotenceTest(unittest.TestCase):
    def test_normalising_punctuation_boundaries_twice_is_identical(self):
        html = (
            "<p><strong>文｜</strong>合成作者</p>"
            "<p>前文<em>，强调开始</em>后文</p>"
            "<p><strong>。！</strong>正文</p>"
        )
        once = article_formatting.normalize_formatting_html(html)
        twice = article_formatting.normalize_formatting_html(once)
        self.assertEqual(once, twice)

    def test_literal_preformatted_emphasis_is_untouched(self):
        html = "<pre><span><strong>文｜</strong>代码</span></pre>"
        self.assertEqual(article_formatting.normalize_formatting_html(html),
                         '<pre><span><strong>文｜</strong>代码</span></pre>')


class LiteralPunctuationTest(unittest.TestCase):
    def test_literal_markdown_characters_stay_literal(self):
        content = _wechat(
            "<p><strong>a * b</strong> 与 <em>_c_</em> 和 `x` 以及 [方括号]</p>"
        )
        self.assertIn(r"**a \* b**", content)
        self.assertIn(r"*\_c\_*", content)
        self.assertIn("`x`", content)
        self.assertIn("[方括号]", content)
        body = _rendered(content)
        self.assertIn("a * b", body.get_text())
        self.assertIn("_c_", body.get_text())

    def test_no_stray_emphasis_delimiters_are_created(self):
        content = _wechat("<p><strong>两端有空格 </strong>尾随 * 星号</p>")
        for line in content.splitlines():
            if line.strip():
                self.assertFalse(line.strip().startswith("** "), line)
                self.assertFalse(line.strip().endswith(" **"), line)


class HostileInputTest(unittest.TestCase):
    def test_script_style_and_injection_are_inert(self):
        content = _wechat(
            '<p><span style="font-weight:bold;background:url(javascript:alert(1))">外</span>'
            "<script>alert(2)</script><style>p{color:red}</style>"
            '<strong onclick="alert(3)"><img src="x" onerror="alert(4)"></strong></p>'
        )
        self.assertNotIn("<script", content)
        self.assertNotIn("<style", content)
        self.assertNotIn("javascript:", content)
        self.assertNotIn("onerror", content)
        self.assertIn("**外**", content)

    def test_malformed_inline_styles_do_not_crash(self):
        content = _wechat(
            '<p><span style="font-weight:">甲</span>'
            '<span style="font-weight:bold;font-style:">乙</span>'
            '<span style="font-weight:999999">丙</span></p>'
        )
        self.assertIn("甲", content)
        self.assertIn("**乙**", content)
        self.assertIn("丙", content)


class CaptionRegressionTest(unittest.TestCase):
    def test_caption_emphasis_is_left_to_existing_caption_handling(self):
        content = _wechat(
            '<figure><img src="https://example.com/a.png">'
            "<figcaption>图源 <strong>某人</strong> 与 <em>重点</em> *星号*</figcaption>"
            "</figure><p>后文。</p>"
        )
        self.assertIn("> 图注：图源 **某人** 与 *重点*", content)
        self.assertIn(r"\*星号\*", content)
        self.assertNotIn("> **图注：", content)

    def test_caption_prefix_is_not_wrapped_in_emphasis(self):
        content = _wechat(
            '<section><img data-src="https://mmbiz.qpic.cn/x.jpg"></section>'
            '<p style="text-align:center"><span style="font-size:12px;color:#999999">'
            "居中说明文字</span></p>"
        )
        self.assertIn("> 图注：居中说明文字", content)
        self.assertNotIn("> **图注：", content)


class GenericPathTest(unittest.TestCase):
    def test_css_bold_and_native_heading_survive_extraction(self):
        content = _generic(
            '<p><span style="font-weight:bold">通用重点</span> 后续文字。</p>'
            "<h3>通用小标题</h3>"
        )
        self.assertIn("**通用重点**", content)
        self.assertIn("### 通用小标题", content)
        self.assertNotIn("font-weight", content)

    def test_strong_across_blocks_is_split(self):
        content = _generic(
            "<strong><div>块一</div><div>块二</div></strong>"
        )
        self.assertIn("**块一**", content)
        self.assertIn("**块二**", content)
        self.assertEqual(content.count("**"), 4)
        for line in content.splitlines():
            self.assertEqual(line.count("**") % 2, 0, line)


class NormalizeIdempotenceTest(unittest.TestCase):
    def test_normalizing_twice_yields_the_same_markdown(self):
        from markdownify import markdownify as md

        html = (
            '<p><strong>粗体<span style="font-weight:normal">正常</span></strong></p>'
            "<strong><div>块一</div><div>块二</div></strong>"
            '<p><span style="font-style:italic">斜体</span></p>'
        )
        once = article_formatting.normalize_formatting_html(html)
        twice = article_formatting.normalize_formatting_html(once)
        self.assertEqual(
            md(once, heading_style="ATX"), md(twice, heading_style="ATX")
        )


if __name__ == "__main__":
    unittest.main()
