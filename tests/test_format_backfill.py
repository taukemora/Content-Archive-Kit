#!/usr/bin/env python3
"""Format backfill: read-only plan/prepare tests (synthetic archives only).

Every fixture is generated in the platform scratch directory; nothing here
reads a private archive, network, ``.state`` or index.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
SCRIPTS = ROOT / "scripts"
PYTHON = _support.PYTHON
sys.path.insert(0, str(SCRIPTS))

import archive_url  # noqa: E402
import common  # noqa: E402
import format_backfill  # noqa: E402

SCRIPT = SCRIPTS / "format_backfill.py"
PAGE = "https://mp.weixin.qq.com/s/synthetic-backfill"
ENTRY_REL = "library/tech/synthetic-backfill-entry/entry.md"

# Raw DOM: one CSS-only bold run, one image with an explicit caption, one
# semantic bold paragraph.  The image target is what a live archive localises.
RAW_HTML = """<html><body><div id="js_content">
<p>导语段落，用于说明本文主题与上下文。</p>
<p><span style="font-weight:700">关键结论</span>出现在第二段中。</p>
<p><img data-src="https://mmbiz.qpic.cn/synthetic/pic001.png" alt="测试图片"></p>
<p>图注：这是一张合成测试图片。</p>
<p><strong>整体加粗段落。</strong></p>
</div></body></html>"""

LEGACY_FULLTEXT = """导语段落，用于说明本文主题与上下文。

关键结论出现在第二段中。

![测试图片](https://mmbiz.qpic.cn/synthetic/pic001.png)

> 图注：这是一张合成测试图片。

**整体加粗段落。**"""

# The archived body keeps the locally stored image target, never the remote URL.
LEGACY_BODY = LEGACY_FULLTEXT.replace(
    "https://mmbiz.qpic.cn/synthetic/pic001.png", "images/pic001.png"
)

# Raw DOM whose emphasis edges touch Unicode punctuation.  The current
# extractor relocates the punctuation outside the markers; the archived body is
# the legacy conversion where it still sits inside them.
PUNCT_HTML = (
    '<html><body><div id="js_content">'
    "<p><strong>文｜</strong>合成作者</p>"
    "<p>前文<em>，强调开始</em>后文</p>"
    '<p><img data-src="https://mmbiz.qpic.cn/synthetic/pic001.png" alt="测试图片"></p>'
    "</div></body></html>"
)
PUNCT_LEGACY = (
    "**文｜**合成作者\n\n前文*，强调开始*后文\n\n"
    "![测试图片](images/pic001.png)"
)

SUMMARY = "合成摘要段落，用于确认摘要正文的字节在回填前后保持不变。"

MULTI_HTML = (
    '<html><body><div id="js_content">'
    "<div><strong><p>第一段重点。</p><p>第二段重点。</p></strong></div>"
    "</div></body></html>"
)
MULTI_LEGACY = "**第一段重点。\n\n第二段重点。**"
MULTI_CURRENT = "**第一段重点。**\n\n**第二段重点。**"

MIXED_HTML = (
    '<html><body><div id="js_content">'
    '<p>前文 <span style="font-weight:700">粗体</span> 与 '
    '<span style="font-weight:700;font-style:italic">粗斜体</span> 混排。</p>'
    '<p><strong>外层加粗<span style="font-weight:normal">恢复正常</span>仍加粗</strong></p>'
    "</div></body></html>"
)
MIXED_LEGACY = "前文 粗体 与 粗斜体 混排。\n\n**外层加粗恢复正常仍加粗**"

HEADING_HTML = (
    '<html><body><div id="js_content">'
    '<p><span style="font-weight:600">小节标题</span></p>'
    "<p>正文段落。</p>"
    "<p>普通段落文字。</p>"
    "</div></body></html>"
)
HEADING_LEGACY = "小节标题\n\n正文段落。\n\n普通段落文字。"

DUPE_HTML = (
    '<html><body><div id="js_content">'
    "<p><strong>重复强调</strong></p><p>中间正文。</p><p><strong>重复强调</strong></p>"
    "</div></body></html>"
)
DUPE_LEGACY = "**重复强调**\n\n中间正文。\n\n**重复强调**"

INLINE_HTML = (
    '<html><body><div id="js_content">'
    '<p>前文 <img data-src="https://mmbiz.qpic.cn/synthetic/inline.png" alt="内图"> '
    '后文 <span style="font-weight:700">重点</span>。</p>'
    "</div></body></html>"
)
INLINE_LEGACY = "前文 ![内图](images/inline.png) 后文 重点。"

# A caption recognised only by the small-grey CSS heuristic, so raw wording has
# no literal "图注：" prefix.  The archived copy kept the plain paragraph.
CSS_CAPTION_HTML = (
    '<html><body><div id="js_content">'
    "<p>导语段落。</p>"
    '<p><img data-src="https://mmbiz.qpic.cn/synthetic/pic001.png" alt="示意图"></p>'
    '<p style="font-size:12px;color:#888888">小声说明图示内容。</p>'
    "<p>正文继续。</p>"
    "</div></body></html>"
)
CSS_CAPTION_PLAIN = (
    "导语段落。\n\n![示意图](images/pic001.png)\n\n"
    "小声说明图示内容。\n\n正文继续。"
)

# Caption with an attribution link; the link must survive the annotation.
LINK_CAPTION_HTML = (
    '<html><body><div id="js_content">'
    "<p>导语段落。</p>"
    '<p><img data-src="https://mmbiz.qpic.cn/synthetic/pic001.png" alt="测试图片"></p>'
    '<p style="font-size:12px;color:#777777">摄影：'
    '<a href="https://example.com/credit">张三</a></p>'
    "<p>正文继续。</p>"
    "</div></body></html>"
)
LINK_CAPTION_PLAIN = (
    "导语段落。\n\n![测试图片](images/pic001.png)\n\n"
    "摄影：[张三](https://example.com/credit)\n\n正文继续。"
)

# An ordinary prose paragraph after the image: same shape as a caption, but no
# small/grey signal, so it must never be annotated as one.
ORDINARY_HTML = (
    '<html><body><div id="js_content">'
    "<p>导语段落。</p>"
    '<p><img data-src="https://mmbiz.qpic.cn/synthetic/pic001.png" alt="示意图"></p>'
    "<p>这是普通正文，不是图注。</p>"
    "<p>正文继续。</p>"
    "</div></body></html>"
)
ORDINARY_PLAIN = (
    "导语段落。\n\n![示意图](images/pic001.png)\n\n"
    "这是普通正文，不是图注。\n\n正文继续。"
)


def entry_text(fulltext: str, overrides: dict | None = None) -> str:
    fm = {
        "id": "synthetic-backfill-entry",
        "title": "合成回填标题",
        "original_title": "合成回填标题",
        "source": "合成公众号",
        "author": "测试作者",
        "published": "2026-01-01T00:00:00+08:00",
        "archived": "2026-01-02T00:00:00+08:00",
        "url": PAGE,
        "canonical_url": PAGE,
        "category": "tech",
        "collection": "manual",
        "tags": ["synthetic"],
        "summary_chars": common.count_summary_chars(SUMMARY),
        "status": "ok",
        "content_hash": "",
        "raw_file": "raw.html.gz",
        "images": 1,
        "extraction": "wechat",
        "language": "zh",
    }
    fm.update(overrides or {})
    body = (
        "# 合成回填标题\n\n"
        f"> 来源：合成公众号 · 作者：测试作者 · 发布：2026-01-01 · [原文]({PAGE})\n\n"
        "## 摘要\n\n"
        f"{SUMMARY}\n\n"
        "## 全文\n\n"
        f"{fulltext}\n"
    )
    fm["content_hash"] = common.compute_content_hash(body)
    return common.dump_frontmatter(fm) + body


def make_archive(
    fulltext: str,
    raw_html: str = RAW_HTML,
    *,
    raw_name: str = "raw.html.gz",
    gz: bool = True,
    overrides: dict | None = None,
    image: bool = True,
) -> tuple[Path, Path]:
    """Build a throwaway archive root with one WeChat entry; return (root, entry)."""
    root = _support.scratch_dir("backfill-src-")
    entry_dir = root / "library" / "tech" / "synthetic-backfill-entry"
    entry_dir.mkdir(parents=True)
    if image:
        (entry_dir / "images").mkdir()
        (entry_dir / "images" / "pic001.png").write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
    text = entry_text(fulltext, overrides)
    (entry_dir / "entry.md").write_text(text, encoding="utf-8")
    if gz:
        with gzip.open(entry_dir / raw_name, "wb") as fh:
            fh.write(raw_html.encode("utf-8"))
    else:
        (entry_dir / raw_name).write_bytes(raw_html.encode("utf-8"))
    return root, entry_dir / "entry.md"


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, "-B", str(SCRIPT), *args],
        cwd=str(cwd or _support.SCRATCH),
        capture_output=True,
        text=True,
    )


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class SandboxCase(unittest.TestCase):
    def setUp(self):
        self._dirs: list[Path] = []

    def tearDown(self):
        for directory in self._dirs:
            shutil.rmtree(directory, ignore_errors=True)

    def track(self, path: Path) -> Path:
        self._dirs.append(path)
        return path

    def archive(self, *args, **kwargs) -> tuple[Path, Path]:
        root, entry = make_archive(*args, **kwargs)
        self.track(root)
        return root, entry

    def plan(self, root: Path, selections: dict | None = None):
        out = self.track(_support.scratch_dir("backfill-plan-")) / "plan.json"
        args = ["plan", "--source", str(root), "--out", str(out)]
        if selections is not None:
            sel = out.parent / "selections.json"
            sel.write_text(json.dumps(selections, ensure_ascii=False), encoding="utf-8")
            args += ["--selections", str(sel)]
        proc = run_cli(*args)
        return proc, out, (read_json(out) if out.exists() else {})

    def prepare(self, root: Path, plan_path: Path, dest: Path | None = None):
        if dest is None:
            dest = self.track(_support.scratch_dir("backfill-dest-parent-")) / "candidate"
        proc = run_cli("prepare", "--source", str(root), "--plan", str(plan_path), "--dest", str(dest))
        receipt = read_json(dest / "receipt.json") if (dest / "receipt.json").exists() else {}
        return proc, dest, receipt

    def prepared_entry(self, root: Path, selections: dict | None = None) -> str:
        proc, out, plan = self.plan(root, selections)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(plan["rows"], "plan produced no rows")
        proc, dest, receipt = self.prepare(root, out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(receipt["written"]), 1, receipt)
        return (dest / ENTRY_REL).read_text(encoding="utf-8")

    def body_fulltext(self, text: str) -> str:
        return format_backfill.fulltext_region(common.split_frontmatter(text)[1])[1]


class CaptionCycleTest(SandboxCase):
    """The first required cycle: plan then prepare one captioned entry."""

    def test_plan_reports_one_changed_row_with_marker_only_diff(self):
        root, entry = self.archive(LEGACY_BODY)
        proc, _out, plan = self.plan(root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(plan["rows"]), 1)
        row = plan["rows"][0]
        self.assertEqual(row["status"], "changed")
        self.assertEqual(row["path"], ENTRY_REL)
        self.assertEqual(row["source_entry_sha256"], format_backfill.sha256_file(entry))
        self.assertEqual(row["raw_sha256"], format_backfill.sha256_file(entry.parent / "raw.html.gz"))
        self.assertNotEqual(row["candidate_sha256"], row["source_entry_sha256"])
        self.assertTrue(row["diff"].startswith("--- "), row["diff"])
        self.assertIn("+**关键结论**出现在第二段中。", row["diff"])
        self.assertEqual(plan["counts"], {"changed": 1, "unchanged": 0, "needs_review": 0, "unsupported": 0})
        self.assertEqual(sum(plan["counts"].values()), len(plan["rows"]))
        self.assertTrue(plan["plan_digest"].startswith("sha256:"))
        self.assertEqual(plan["plan_digest"], format_backfill.plan_digest(plan))

    def test_prepare_writes_candidate_tree_and_preserves_bytes(self):
        root, entry = self.archive(LEGACY_BODY)
        source_before = entry.read_bytes()
        raw_before = (entry.parent / "raw.html.gz").read_bytes()
        image_before = (entry.parent / "images" / "pic001.png").read_bytes()

        proc, out, plan = self.plan(root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc, dest, receipt = self.prepare(root, out)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        self.assertTrue(receipt["requires_index_rebuild"])
        self.assertFalse(receipt["source_modified"])
        self.assertFalse(receipt["network_used"])
        self.assertEqual(receipt["plan_digest"], plan["plan_digest"])
        self.assertEqual([r["path"] for r in receipt["written"]], [ENTRY_REL])
        self.assertEqual(receipt["skipped"], [])

        new_entry = dest / ENTRY_REL
        text = new_entry.read_text(encoding="utf-8")
        self.assertIn("**关键结论**出现在第二段中。", text)
        self.assertIn("![测试图片](images/pic001.png)", text)
        self.assertIn("> 图注：这是一张合成测试图片。", text)
        self.assertEqual(text.count("> 图注："), 1)
        self.assertNotIn("https://mmbiz.qpic.cn", text)
        self.assertIn(SUMMARY, text)
        self.assertEqual(self.body_fulltext(text).count("**关键结论**"), 1)

        fm, body = common.split_frontmatter(text)
        self.assertEqual(fm["content_hash"], common.compute_content_hash(body))
        self.assertEqual(fm["content_hash"], receipt["written"][0]["content_hash_after"])
        self.assertNotEqual(fm["content_hash"], plan["rows"][0]["content_hash_before"])

        self.assertEqual((new_entry.parent / "raw.html.gz").read_bytes(), raw_before)
        self.assertEqual((new_entry.parent / "images" / "pic001.png").read_bytes(), image_before)
        self.assertEqual(entry.read_bytes(), source_before, "source entry.md was modified")
        self.assertFalse((dest / "index.csv").exists())
        self.assertFalse((dest / ".state").exists())

    def test_second_plan_on_candidate_is_idempotent(self):
        root, _entry = self.archive(LEGACY_BODY)
        proc, out, _plan = self.plan(root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc, dest, _receipt = self.prepare(root, out)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        proc, _out2, plan2 = self.plan(dest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(plan2["counts"]["changed"], 0)
        self.assertEqual(plan2["rows"][0]["status"], "unchanged")
        self.assertIn("already current", " ".join(plan2["rows"][0]["reasons"]))
        self.assertIn("> 图注：", (dest / ENTRY_REL).read_text(encoding="utf-8"))


class CaptionRecoveryTest(SandboxCase):
    """Unmarked archived captions get only the generated caption syntax."""

    def test_unmarked_explicit_caption_is_annotated(self):
        root, _entry = self.archive(LEGACY_BODY.replace("> 图注：", "图注："))
        proc, _out, plan = self.plan(root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(plan["rows"][0]["status"], "changed", plan["rows"][0]["reasons"])
        text = self.prepared_entry(root)
        fulltext = self.body_fulltext(text)
        self.assertEqual(fulltext.count("> 图注：这是一张合成测试图片。"), 1)
        self.assertNotIn("\n图注：这是一张合成测试图片。", fulltext)
        self.assertIn("![测试图片](images/pic001.png)", fulltext)
        self.assertNotIn("mmbiz.qpic.cn", text)
        self.assertIn("**关键结论**出现在第二段中。", text)
        fm, body = common.split_frontmatter(text)
        self.assertEqual(fm["content_hash"], common.compute_content_hash(body))

    def test_unmarked_css_caption_without_prefix_is_annotated(self):
        root, _entry = self.archive(CSS_CAPTION_PLAIN, CSS_CAPTION_HTML)
        text = self.prepared_entry(root)
        fulltext = self.body_fulltext(text)
        self.assertIn("> 图注：小声说明图示内容。", fulltext)
        self.assertNotIn("\n小声说明图示内容。", fulltext)
        self.assertIn("![示意图](images/pic001.png)", fulltext)
        self.assertIn("正文继续。", fulltext)

    def test_caption_attribution_link_is_preserved(self):
        root, _entry = self.archive(LINK_CAPTION_PLAIN, LINK_CAPTION_HTML)
        text = self.prepared_entry(root)
        fulltext = self.body_fulltext(text)
        self.assertIn("> 图注：摄影：[张三](https://example.com/credit)", fulltext)
        self.assertEqual(fulltext.count("https://example.com/credit"), 1)

    def test_ordinary_paragraph_after_image_is_not_annotated(self):
        root, entry = self.archive(ORDINARY_PLAIN, ORDINARY_HTML)
        row, candidate = format_backfill.analyse_entry(root, entry)
        self.assertEqual(row["status"], "unchanged", row["reasons"])
        self.assertEqual(candidate, entry.read_text(encoding="utf-8"))
        self.assertNotIn("> 图注：", candidate)

    def test_edited_caption_text_is_refused(self):
        edited = LEGACY_BODY.replace("> 图注：这是一张合成测试图片。", "图注：这是一张被改写的图注。")
        root, _entry = self.archive(edited)
        proc, _out, plan = self.plan(root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = plan["rows"][0]
        self.assertEqual(row["status"], "needs_review")
        self.assertIn("does not match the raw snapshot", " ".join(row["reasons"]))


class UnicodeBoundaryBackfillTest(SandboxCase):
    """Legacy entries with punctuation inside emphasis markers are repairable."""

    def test_marker_edits_accept_only_the_relocated_stars(self):
        for old, new in (
            ("**文｜**合成作者", "**文**｜合成作者"),
            ("前文**，强调开始**后文", "前文，**强调开始**后文"),
            ("**编辑：**正文继续。", "**编辑**：正文继续。"),
        ):
            with self.subTest(old=old):
                edits = format_backfill.marker_edits(old, new)
                self.assertIsNotNone(edits)
                self.assertEqual(format_backfill.apply_edits(old, edits), new)
                for start, end, added in edits:
                    self.assertLessEqual(set(old[start:end]) | set(added), {"*"})
        # Narrative and punctuation rewrites remain refused.
        self.assertIsNone(format_backfill.marker_edits("**原文**", "**改写**"))
        self.assertIsNone(format_backfill.marker_edits("**文｜**合成", "**文，**合成"))
        # Literal stars inside code/escapes are content, never markers.
        self.assertIsNone(
            format_backfill.marker_edits("`**x**`**文｜**合成作者", "`x`**文**｜合成作者")
        )

    def test_legacy_punctuation_entry_is_backfilled_and_stable(self):
        root, entry = self.archive(PUNCT_LEGACY, PUNCT_HTML)
        source_before = entry.read_bytes()
        raw_before = (entry.parent / "raw.html.gz").read_bytes()
        image_before = (entry.parent / "images" / "pic001.png").read_bytes()

        proc, out, plan = self.plan(root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = plan["rows"][0]
        self.assertEqual(row["status"], "changed", row["reasons"])
        self.assertIn("+**文**｜合成作者", row["diff"])
        self.assertIn("+前文，*强调开始*后文", row["diff"])

        proc, dest, receipt = self.prepare(root, out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(receipt["written"]), 1, receipt)
        text = (dest / ENTRY_REL).read_text(encoding="utf-8")
        fulltext = self.body_fulltext(text)
        expected = archive_url.wechat_extract(PUNCT_HTML, PAGE)[1].replace(
            "https://mmbiz.qpic.cn/synthetic/pic001.png", "images/pic001.png"
        )
        self.assertEqual(fulltext, expected)
        self.assertIn("**文**｜合成作者", fulltext)
        self.assertIn("前文，*强调开始*后文", fulltext)
        self.assertNotIn("**文｜**", fulltext)
        self.assertNotIn("*，强调开始*", fulltext)
        self.assertEqual((dest / ENTRY_REL).parent.joinpath("raw.html.gz").read_bytes(), raw_before)
        self.assertEqual(
            (dest / ENTRY_REL).parent.joinpath("images", "pic001.png").read_bytes(),
            image_before,
        )
        self.assertEqual(entry.read_bytes(), source_before, "source entry.md was modified")
        fm, body = common.split_frontmatter(text)
        self.assertEqual(fm["content_hash"], common.compute_content_hash(body))

        # A second plan on the candidate is byte-identical and unchanged.
        proc, _out2, plan2 = self.plan(dest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(plan2["rows"][0]["status"], "unchanged", plan2["rows"][0]["reasons"])


class FormattingBoundaryTest(SandboxCase):
    def test_emphasis_spanning_blocks_is_split(self):
        root, _entry = self.archive(MULTI_LEGACY, MULTI_HTML)
        text = self.prepared_entry(root)
        self.assertIn(MULTI_CURRENT, self.body_fulltext(text))
        self.assertNotIn("**第一段重点。\n\n第二段重点。**", text)

    def test_css_weight_italic_and_normal_override(self):
        root, _entry = self.archive(MIXED_LEGACY, MIXED_HTML)
        text = self.prepared_entry(root)
        # The candidate must equal the current extractor, so this keeps tracking
        # the formatting owner's fixes instead of pinning one intermediate output.
        import archive_url

        current = archive_url.wechat_extract(MIXED_HTML, PAGE)[1]
        self.assertEqual(
            format_backfill.image_key(self.body_fulltext(text)),
            format_backfill.image_key(current),
        )

    def test_literal_markdown_punctuation_and_code_survive(self):
        from markdownify import markdownify as md

        inner = "<p>代码 <code>**literal**</code> 与转义 \\*星号\\* 文本。</p>"
        body_text = md(inner).strip()
        html = f'<html><body><div id="js_content">{inner}</div></body></html>'
        root, entry = self.archive(body_text, html)
        before = entry.read_bytes()
        proc, _out, plan = self.plan(root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = plan["rows"][0]
        self.assertEqual(row["status"], "unchanged", row["reasons"])
        self.assertEqual(row["candidate_sha256"], format_backfill.sha256_file(entry))
        self.assertEqual(entry.read_bytes(), before)
        self.assertIn("`**literal**`", body_text)

    def test_appended_notes_are_preserved(self):
        root, _entry = self.archive(LEGACY_BODY + "\n\n---\n\n## 备注\n\n这是我追加的笔记。")
        proc, _out, plan = self.plan(root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = plan["rows"][0]
        self.assertEqual(row["status"], "changed", row["reasons"])
        self.assertIn("appended", " ".join(row["reasons"]))
        text = self.prepared_entry(root)
        self.assertIn("## 备注", text)
        self.assertIn("这是我追加的笔记。", text)
        self.assertIn("**关键结论**出现在第二段中。", text)

    def test_inline_image_target_survives_marker_shift(self):
        root, _entry = self.archive(INLINE_LEGACY, INLINE_HTML)
        text = self.prepared_entry(root)
        fulltext = self.body_fulltext(text)
        self.assertEqual(fulltext, "前文 ![内图](images/inline.png) 后文 **重点**。")
        self.assertNotIn("mmbiz.qpic.cn", text)


class HeadingSelectionTest(SandboxCase):
    def test_reviewed_bold_paragraph_is_promoted(self):
        root, _entry = self.archive(HEADING_LEGACY, HEADING_HTML)
        proc, _out, plan = self.plan(root, {ENTRY_REL: ["小节标题"]})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = plan["rows"][0]
        self.assertEqual(row["status"], "changed", row["reasons"])
        self.assertEqual(row["selections"], ["小节标题"])
        text = self.prepared_entry(root, {ENTRY_REL: ["小节标题"]})
        self.assertIn("## 小节标题", text)
        self.assertNotIn("**小节标题**", text)

    def test_ordinary_prose_selection_is_refused(self):
        root, _entry = self.archive(HEADING_LEGACY, HEADING_HTML)
        proc, _out, plan = self.plan(root, {ENTRY_REL: ["普通段落文字。"]})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = plan["rows"][0]
        self.assertEqual(row["status"], "needs_review")
        self.assertIsNone(row["candidate_sha256"])
        self.assertIn("ordinary prose", " ".join(row["reasons"]))

    def test_missing_selection_is_refused(self):
        root, _entry = self.archive(HEADING_LEGACY, HEADING_HTML)
        proc, _out, plan = self.plan(root, {ENTRY_REL: ["并不存在的标题"]})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = plan["rows"][0]
        self.assertEqual(row["status"], "needs_review")
        self.assertIn("not found", " ".join(row["reasons"]))

    def test_repeated_raw_block_selection_is_refused(self):
        root, _entry = self.archive(DUPE_LEGACY, DUPE_HTML)
        proc, _out, plan = self.plan(root, {ENTRY_REL: ["重复强调"]})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = plan["rows"][0]
        self.assertEqual(row["status"], "needs_review")
        self.assertIn("repeated", " ".join(row["reasons"]))

    def test_unknown_selection_path_fails_the_plan(self):
        root, _entry = self.archive(HEADING_LEGACY, HEADING_HTML)
        proc, out, _plan = self.plan(root, {"library/tech/nope/entry.md": ["小节标题"]})
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(out.exists())
        self.assertIn("unknown entries", proc.stdout)

    def test_heading_only_change_is_reported_changed(self):
        html = '<div id="js_content"><p><strong>小节标题</strong></p><p>正文段落。</p></div>'
        root, entry = self.archive("**小节标题**\n\n正文段落。", html)
        row, candidate = format_backfill.analyse_entry(
            root, entry, {ENTRY_REL: ["小节标题"]}
        )
        self.assertEqual(row["status"], "changed", row)
        self.assertIn("## 小节标题", candidate)
        self.assertEqual(row["chunks_changed"], 1)
        self.assertTrue(row["diff"])

    def test_reviewed_heading_rerun_is_byte_identical_unchanged(self):
        selections = {ENTRY_REL: ["小节标题"]}
        root, entry = self.archive(HEADING_LEGACY, HEADING_HTML)
        row, candidate = format_backfill.analyse_entry(root, entry, selections)
        self.assertEqual(row["status"], "changed", row["reasons"])
        entry.write_text(candidate, encoding="utf-8")  # synthetic fixture only
        before = entry.read_bytes()

        row2, candidate2 = format_backfill.analyse_entry(root, entry, selections)
        self.assertEqual(row2["status"], "unchanged", row2["reasons"])
        self.assertEqual(candidate2, candidate)
        self.assertEqual(entry.read_bytes(), before)
        self.assertIn("## 小节标题", candidate2)

    def test_unselected_heading_drift_is_refused(self):
        selections = {ENTRY_REL: ["小节标题"]}
        root, entry = self.archive(HEADING_LEGACY, HEADING_HTML)
        _row, candidate = format_backfill.analyse_entry(root, entry, selections)
        drifted = candidate.replace("## 小节标题", "## 另一个标题")
        entry.write_text(drifted, encoding="utf-8")

        row2, candidate2 = format_backfill.analyse_entry(root, entry, selections)
        self.assertEqual(row2["status"], "needs_review", row2["reasons"])
        self.assertIsNone(candidate2)
        row3, _candidate3 = format_backfill.analyse_entry(root, entry, None)
        self.assertEqual(row3["status"], "needs_review", row3["reasons"])


class ContentDriftTest(SandboxCase):
    def test_edited_narrative_is_refused(self):
        edited = LEGACY_BODY.replace("关键结论出现在第二段中。", "关键结论已被人工改写。")
        root, _entry = self.archive(edited)
        proc, out, plan = self.plan(root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = plan["rows"][0]
        self.assertEqual(row["status"], "needs_review")
        self.assertIn("does not match the raw snapshot", " ".join(row["reasons"]))
        proc, dest, receipt = self.prepare(root, out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(receipt["written"], [])
        self.assertFalse(receipt["requires_index_rebuild"])
        self.assertEqual(receipt["skipped"][0]["status"], "needs_review")
        self.assertFalse((dest / ENTRY_REL).exists())

    def test_removed_paragraph_is_refused(self):
        shortened = LEGACY_BODY.replace("\n\n**整体加粗段落。**", "")
        root, _entry = self.archive(shortened)
        proc, _out, plan = self.plan(root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(plan["rows"][0]["status"], "needs_review")
        self.assertIn("shorter", " ".join(plan["rows"][0]["reasons"]))

    def test_duplicated_body_paragraph_is_refused(self):
        duplicated = LEGACY_BODY.replace(
            "关键结论出现在第二段中。", "关键结论出现在第二段中。\n\n关键结论出现在第二段中。"
        )
        root, _entry = self.archive(duplicated)
        proc, _out, plan = self.plan(root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(plan["rows"][0]["status"], "needs_review")


class SourceSafetyTest(SandboxCase):
    def _single_row(self, root: Path) -> dict:
        proc, _out, plan = self.plan(root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return plan["rows"][0]

    def test_raw_file_traversal_is_refused(self):
        root, _entry = self.archive(LEGACY_BODY, overrides={"raw_file": "../../escape.html.gz"})
        row = self._single_row(root)
        self.assertEqual(row["status"], "unsupported")
        self.assertIn("plain entry-local", " ".join(row["reasons"]))

    def test_symlinked_raw_snapshot_is_refused(self):
        root, entry = self.archive(
            LEGACY_BODY, raw_name="raw-link.html.gz", gz=False,
            overrides={"raw_file": "raw-link.html.gz"},
        )
        target = self.track(_support.scratch_dir("backfill-outside-")) / "outside.html"
        target.write_text(RAW_HTML, encoding="utf-8")
        raw_link = entry.parent / "raw-link.html.gz"
        raw_link.unlink()
        os.symlink(target, raw_link)
        row = self._single_row(root)
        self.assertEqual(row["status"], "unsupported")
        self.assertIn("symlink", " ".join(row["reasons"]))

    def test_oversized_gzip_snapshot_is_refused(self):
        root, entry = self.archive(LEGACY_BODY)
        with gzip.open(entry.parent / "raw.html.gz", "wb") as fh:
            fh.write(b"\0" * (format_backfill.MAX_RAW_BYTES + 64))
        row = self._single_row(root)
        self.assertEqual(row["status"], "unsupported")
        self.assertIn("exceeds", " ".join(row["reasons"]))

    def test_unreadable_entry_is_unsupported_and_other_entries_continue(self):
        root, entry = self.archive(LEGACY_BODY)
        second = root / "library" / "tech" / "second-valid-entry"
        shutil.copytree(entry.parent, second)

        original = Path.read_bytes

        def read_bytes(path):
            if path == entry:
                raise PermissionError("synthetic unreadable entry")
            return original(path)

        with patch.object(Path, "read_bytes", read_bytes):
            plan = format_backfill.build_plan(root)

        self.assertEqual(len(plan["rows"]), 2)
        self.assertEqual(sum(plan["counts"].values()), 2)
        self.assertEqual(plan["counts"]["unsupported"], 1)
        self.assertEqual(plan["counts"]["changed"], 1)
        failed = next(row for row in plan["rows"] if row["status"] == "unsupported")
        self.assertEqual(failed["path"], ENTRY_REL)
        self.assertIn("PermissionError", " ".join(failed["reasons"]))
        self.assertIsNone(failed["source_entry_sha256"])
        self.assertIsNone(failed["candidate_sha256"])
        valid = next(row for row in plan["rows"] if row["status"] == "changed")
        self.assertTrue(valid["source_entry_sha256"].startswith("sha256:"))

    def test_missing_raw_snapshot_is_reported(self):
        root, _entry = self.archive(LEGACY_BODY, overrides={"raw_file": "missing.html.gz"})
        row = self._single_row(root)
        self.assertEqual(row["status"], "unsupported")
        self.assertIn("missing", " ".join(row["reasons"]))

    def test_corrupt_gzip_snapshot_is_reported_per_entry(self):
        root, entry = self.archive(LEGACY_BODY)
        (entry.parent / "raw.html.gz").write_bytes(b"this is not gzip")
        row = self._single_row(root)
        self.assertEqual(row["status"], "unsupported")
        self.assertIn("unreadable", " ".join(row["reasons"]))

    def test_truncated_gzip_is_refused_per_entry_without_crashing(self):
        root, entry = self.archive(LEGACY_BODY)
        raw = entry.parent / "raw.html.gz"
        raw.write_bytes(raw.read_bytes()[:-8])

        second = root / "library" / "tech" / "second-entry"
        second.mkdir()
        (second / "entry.md").write_text(
            entry_text(LEGACY_BODY, {"id": "second-entry", "url": PAGE + "-second"}),
            encoding="utf-8",
        )
        (second / "raw.html.gz").write_bytes(self._valid_raw())

        plan = format_backfill.build_plan(root)
        self.assertEqual(len(plan["rows"]), 2)
        by_path = {row["path"]: row for row in plan["rows"]}
        self.assertEqual(by_path[ENTRY_REL]["status"], "unsupported")
        self.assertIn("unreadable", " ".join(by_path[ENTRY_REL]["reasons"]))
        self.assertEqual(
            by_path["library/tech/second-entry/entry.md"]["status"], "changed"
        )
        self.assertEqual(plan["counts"], {
            "changed": 1, "unchanged": 0, "needs_review": 0, "unsupported": 1,
        })

    def test_crlf_entry_is_refused_and_left_untouched(self):
        root, entry = self.archive(LEGACY_BODY)
        original = entry.read_bytes().replace(b"\n", b"\r\n")
        entry.write_bytes(original)
        row, candidate = format_backfill.analyse_entry(root, entry)
        self.assertEqual(row["status"], "unsupported", row)
        self.assertIn("CRLF", " ".join(row["reasons"]))
        self.assertIsNone(candidate)
        self.assertEqual(entry.read_bytes(), original)
        self.assertEqual(row["source_entry_sha256"], format_backfill.sha256_file(entry))

    def _valid_raw(self) -> bytes:
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb") as handle:
            handle.write(RAW_HTML.encode("utf-8"))
        return buffer.getvalue()

    def test_tool_has_no_network_client(self):
        self.assertFalse(hasattr(format_backfill, "requests"))
        self.assertFalse(hasattr(format_backfill, "urllib"))

    def test_non_wechat_and_partial_entries_are_unsupported(self):
        root, _entry = self.archive(
            LEGACY_BODY,
            overrides={"extraction": "generic", "source_type": "tech_blog", "url": "https://example.com/post"},
        )
        row = self._single_row(root)
        self.assertEqual(row["status"], "unsupported")
        self.assertIn("not a WeChat", " ".join(row["reasons"]))

        root2, _entry2 = self.archive(LEGACY_BODY, overrides={"status": "partial"})
        row2 = self._single_row(root2)
        self.assertEqual(row2["status"], "unsupported")
        self.assertIn("partial", " ".join(row2["reasons"]))

    def test_every_scanned_entry_is_counted(self):
        root, _entry = self.archive(LEGACY_BODY)
        other = root / "library" / "tech" / "second-entry"
        other.mkdir()
        other_text = entry_text(
            "另一篇正文。",
            {"id": "second-entry", "url": "https://example.com/x", "extraction": "generic",
             "source_type": "tech_blog"},
        )
        (other / "entry.md").write_text(other_text, encoding="utf-8")
        proc, _out, plan = self.plan(root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(plan["rows"]), 2)
        self.assertEqual(sum(plan["counts"].values()), 2)
        self.assertEqual(plan["counts"]["changed"], 1)
        self.assertEqual(plan["counts"]["unsupported"], 1)


class PlanIntegrityTest(SandboxCase):
    def _plan(self, root: Path) -> Path:
        proc, out, _plan = self.plan(root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return out

    def test_changed_source_after_plan_is_rejected_as_stale(self):
        root, entry = self.archive(LEGACY_BODY)
        out = self._plan(root)
        entry.write_text(entry.read_text(encoding="utf-8") + "\n<!-- edited -->\n", encoding="utf-8")
        proc, dest, _receipt = self.prepare(root, out)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("stale", proc.stdout)
        self.assertFalse(dest.exists())

    def test_tampered_plan_digest_is_rejected(self):
        root, _entry = self.archive(LEGACY_BODY)
        out = self._plan(root)
        plan = read_json(out)
        plan["rows"][0]["chunks_changed"] = 99
        out.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        proc, dest, _receipt = self.prepare(root, out)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("digest mismatch", proc.stdout)
        self.assertFalse(dest.exists())

    def test_altered_candidate_hash_with_valid_digest_is_rejected(self):
        root, _entry = self.archive(LEGACY_BODY)
        out = self._plan(root)
        plan = read_json(out)
        plan["rows"][0]["candidate_sha256"] = "sha256:" + "0" * 64
        plan["plan_digest"] = format_backfill.plan_digest(plan)
        out.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        proc, dest, _receipt = self.prepare(root, out)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("stale", proc.stdout)
        self.assertFalse(dest.exists())

    def test_plan_from_another_source_root_is_rejected(self):
        root, _entry = self.archive(LEGACY_BODY)
        other, _other_entry = self.archive(LEGACY_BODY)
        out = self._plan(root)
        plan = read_json(out)
        plan["source_root"] = str(other.resolve())
        plan["plan_digest"] = format_backfill.plan_digest(plan)
        out.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        proc, dest, _receipt = self.prepare(root, out)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("different source root", proc.stdout)
        self.assertFalse(dest.exists())

    def test_plan_output_inside_source_is_refused(self):
        root, _entry = self.archive(LEGACY_BODY)
        out = root / "plan.json"
        proc = run_cli("plan", "--source", str(root), "--out", str(out))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("inside the source", proc.stdout)
        self.assertFalse(out.exists())

    def test_plan_output_inside_public_checkout_is_refused(self):
        root, _entry = self.archive(LEGACY_BODY)
        out = ROOT / "backfill-plan-should-not-exist.json"
        self.assertFalse(out.exists())
        proc = run_cli("plan", "--source", str(root), "--out", str(out))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("public checkout", proc.stdout)
        self.assertFalse(out.exists())


class DestinationSafetyTest(SandboxCase):
    def test_existing_destination_is_refused(self):
        root, _entry = self.archive(LEGACY_BODY)
        out = self._plan(root)
        dest = self.track(_support.scratch_dir("backfill-existing-")) / "candidate"
        dest.mkdir()
        (dest / "keep.txt").write_text("x", encoding="utf-8")
        proc, _dest, _receipt = self.prepare(root, out, dest)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("already exists", proc.stdout)
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ["keep.txt"])

    def test_destination_inside_source_is_refused(self):
        root, _entry = self.archive(LEGACY_BODY)
        out = self._plan(root)
        dest = root / "candidate"
        proc, _dest, _receipt = self.prepare(root, out, dest)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("overlaps", proc.stdout)
        self.assertFalse(dest.exists())

    def test_destination_through_symlink_is_refused(self):
        root, _entry = self.archive(LEGACY_BODY)
        out = self._plan(root)
        target = self.track(_support.scratch_dir("backfill-link-target-"))
        link = self.track(_support.scratch_dir("backfill-link-parent-")) / "link"
        os.symlink(target, link)
        proc, _dest, _receipt = self.prepare(root, out, link / "candidate")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("symlink", proc.stdout)
        self.assertFalse((target / "candidate").exists())

    def test_destination_inside_public_checkout_is_refused(self):
        root, _entry = self.archive(LEGACY_BODY)
        out = self._plan(root)
        dest = ROOT / "backfill-candidate-should-not-exist"
        self.assertFalse(dest.exists())
        proc, _dest, _receipt = self.prepare(root, out, dest)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("public checkout", proc.stdout)
        self.assertFalse(dest.exists())

    def test_prepare_without_changes_writes_only_a_receipt(self):
        root, _entry = self.archive(LEGACY_BODY.replace("关键结论出现在第二段中。", "人工改写后的句子。"))
        out = self._plan(root)
        proc, dest, receipt = self.prepare(root, out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(receipt["written"], [])
        self.assertFalse(receipt["requires_index_rebuild"])
        self.assertFalse((dest / "library").exists())

    def _plan(self, root: Path) -> Path:
        proc, out, _plan = self.plan(root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return out


if __name__ == "__main__":
    unittest.main()
