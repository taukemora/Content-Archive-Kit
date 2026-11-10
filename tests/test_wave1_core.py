#!/usr/bin/env python3
"""Wave 1A regression tests: core safety, sandbox isolation, dedupe and headings.

Everything destructive runs inside throwaway directories under the platform
temporary directory; any instance ``.state/seen.json`` stays byte-identical, and
that is asserted at teardown so cross-directory pollution cannot slip through.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
SCRIPTS = ROOT / "scripts"
SCRATCH = _support.SCRATCH
sys.path.insert(0, str(SCRIPTS))

import common
from archive_url import check_web_content_type, strip_truncation_note, strip_web_extract_header, wechat_extract
from common import (
    assert_public_url,
    atx_h1_lines,
    compute_content_hash,
    dedupe_key,
    entry_char_count,
    is_allowed_file_path,
    is_prohibited_ip,
    normalize_body_headings,
    read_entry,
    state_lock,
    write_entry,
    write_text_atomic,
)

PROD_SEEN = ROOT / ".state" / "seen.json"


def prod_seen_sha() -> str | None:
    """Digest of instance state, ``None`` when the archive has none."""
    return _support.state_digest(PROD_SEEN)


ARTICLE_BODY = (
    "# Wave One Fixture\n"
    "\n"
    "Intro paragraph that is long enough to look like real article text.\n"
    "\n"
    "URL: https://example.com/inside-body\n"
    "Published: not a header line here\n"
    "\n"
    "# Section Two\n"
    "\n"
    "Second section body text.\n"
    "\n"
    "```bash\n"
    "# this is a shell comment, not a heading\n"
    "echo hi\n"
    "```\n"
    "\n"
    "# Section Three\n"
    "\n"
    "Third section body text.\n"
)


class SandboxCase(unittest.TestCase):
    """Base case: temp dirs under scratch + production seen.json guard."""

    def setUp(self):
        self._dirs: list[Path] = []
        self._prod_sha = prod_seen_sha()

    def tearDown(self):
        for d in self._dirs:
            shutil.rmtree(d, ignore_errors=True)
        self.assertEqual(
            prod_seen_sha(),
            self._prod_sha,
            "production .state/seen.json was modified by a test",
        )

    def temp_dir(self, prefix: str) -> Path:
        d = Path(tempfile.mkdtemp(dir=str(SCRATCH), prefix=prefix))
        self._dirs.append(d)
        return d

    def make_project(self, prefix: str, with_state: bool = True) -> Path:
        """Copy the project scripts/config into an isolated sandbox root."""
        base = self.temp_dir(prefix)
        shutil.copytree(SCRIPTS, base / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("taxonomy.yaml", "sources.yaml"):
            shutil.copy2(_support.template(name), base / name)
        (base / "library").mkdir()
        if with_state:
            (base / ".state").mkdir()
            (base / ".state" / "seen.json").write_text('{"entries": {}}', encoding="utf-8")
        return base

    def scratch_file(self, name: str, text: str) -> Path:
        d = self.temp_dir("wave1_files_")
        p = d / name
        p.write_text(text, encoding="utf-8")
        return p

    def run_archive(self, project: Path, *args: str) -> tuple[int, dict]:
        """Run archive_url.py inside the sandbox project with SSRF relaxed."""
        code = (
            "import runpy, sys\n"
            f"sys.path.insert(0, {str(project / 'scripts')!r})\n"
            "import common\n"
            "common.assert_public_url = lambda url: None\n"
            f"sys.argv = ['archive_url.py'] + {list(args)!r}\n"
            f"runpy.run_path({str(project / 'scripts' / 'archive_url.py')!r}, run_name='__main__')\n"
        )
        res = subprocess.run(
            [sys.executable, "-B", "-c", code],
            capture_output=True,
            text=True,
            cwd=str(project),
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        try:
            data = json.loads(res.stdout)
        except Exception:
            data = {"raw": res.stdout, "stderr": res.stderr}
        return res.returncode, data


class TestProhibitedIps(unittest.TestCase):
    def test_ipv6_site_local_blocked(self):
        ip = common.ipaddress.ip_address("fec0::1")
        self.assertTrue(ip.is_site_local)
        self.assertTrue(is_prohibited_ip(ip))
        with self.assertRaises(ValueError):
            assert_public_url("http://[fec0::1]/site-local")

    def test_sixtofour_embedding_private_v4_blocked(self):
        ip = common.ipaddress.ip_address("2002:0a00:0001::")
        self.assertEqual(str(ip.sixtofour), "10.0.0.1")
        self.assertTrue(is_prohibited_ip(ip))

    def test_teredo_embedding_private_v4_blocked(self):
        ip = common.ipaddress.ip_address("2001:0000:0a00:0001:0000:0000:f5ff:fffe")
        teredo = ip.teredo
        self.assertIsNotNone(teredo)
        self.assertEqual(str(teredo[0]), "10.0.0.1")
        self.assertTrue(is_prohibited_ip(ip))

    def test_public_addresses_still_allowed(self):
        for addr in ("8.8.8.8", "2001:4860:4860::8888"):
            self.assertFalse(is_prohibited_ip(common.ipaddress.ip_address(addr)), addr)


class TestSandboxFilePathIsolation(SandboxCase):
    def _load_sandbox_common(self, project: Path):
        module_path = project / "scripts" / "common.py"
        spec = importlib.util.spec_from_file_location("sandbox_common_wave1", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_project_copy_rejects_project_internal_files(self):
        project = self.make_project("wave1_proj_")
        (project / ".state" / "seen.json").write_text('{"entries": {}}', encoding="utf-8")

        sandbox_common = self._load_sandbox_common(project)
        self.assertEqual(sandbox_common.ROOT.resolve(), project.resolve())

        # sources.yaml inside the copied project must never be usable as input...
        self.assertFalse(sandbox_common.is_allowed_file_path(project / "sources.yaml"))
        self.assertFalse(sandbox_common.is_allowed_file_path(project / "sources.yaml", project))
        self.assertFalse(is_allowed_file_path(project / "sources.yaml", project))
        self.assertFalse(is_allowed_file_path(project / "scripts" / "common.py", project))
        # ...while .state/ stays usable for state files.
        self.assertTrue(is_allowed_file_path(project / ".state" / "seen.json", project))
        # Files outside the project in the scratch allowlist are fine.
        outside = self.scratch_file("content.md", "hello")
        self.assertTrue(is_allowed_file_path(outside, project))

    def test_production_project_still_rejects_sources_yaml(self):
        self.assertFalse(is_allowed_file_path(ROOT / "sources.yaml"))
        self.assertFalse(is_allowed_file_path(ROOT / "sources.yaml", ROOT))
        self.assertTrue(is_allowed_file_path(ROOT / ".state" / "pending.json"))


class TestFinalizeWithoutState(SandboxCase):
    def test_finalize_does_not_touch_production_seen(self):
        project = self.make_project("wave1_nostate_", with_state=False)
        entry_dir = project / "library" / "tech" / "2026-10-27-nostate"
        entry_dir.mkdir(parents=True)
        entry_path = entry_dir / "entry.md"
        write_entry(entry_path, {
            "id": "2026-10-27-nostate",
            "title": "无状态沙箱条目",
            "category": "tech",
            "status": "partial",
            "content_note": "正文截断",
            "confidence": 1.0,
            "url": "https://example.com/nostate",
            "canonical_url": "https://example.com/nostate",
        }, "# 无状态沙箱条目\n\n## 全文\n\n正文内容。\n")

        res = subprocess.run(
            [sys.executable, "-B", str(project / "scripts" / "finalize_entry.py"),
             "--path", str(entry_path), "--status", "archived", "--title-zh", "沙箱标题"],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

        # No .state / lock created in the sandbox, and the entry itself was written.
        self.assertFalse((project / ".state").exists())
        fm, body = read_entry(entry_path)
        self.assertEqual(fm.get("status"), "archived")
        self.assertEqual(fm.get("title"), "沙箱标题")
        self.assertTrue(body.lstrip().startswith("# 沙箱标题"))
        self.assertTrue((fm.get("content_hash") or "").startswith("sha256:"))


class TestFinalizeHeadingUpdate(SandboxCase):
    def test_title_update_skips_fenced_code_comments(self):
        project = self.make_project("wave1_finh1_", with_state=False)
        entry_dir = project / "library" / "tech" / "2026-10-27-fenced"
        entry_dir.mkdir(parents=True)
        entry_path = entry_dir / "entry.md"
        body = (
            "```bash\n"
            "# not a heading, just a comment\n"
            "echo hi\n"
            "```\n"
            "\n"
            "# 旧的一级标题\n"
            "\n"
            "## 全文\n"
            "\n"
            "正文内容。\n"
        )
        write_entry(entry_path, {
            "id": "2026-10-27-fenced",
            "title": "旧的一级标题",
            "category": "tech",
            "status": "archived",
            "confidence": 1.0,
            "url": "https://example.com/fenced",
            "canonical_url": "https://example.com/fenced",
        }, body)

        res = subprocess.run(
            [sys.executable, "-B", str(project / "scripts" / "finalize_entry.py"),
             "--path", str(entry_path), "--title-zh", "新的一级标题"],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

        fm, saved = read_entry(entry_path)
        self.assertEqual(fm["title"], "新的一级标题")
        self.assertIn("# not a heading, just a comment", saved)
        self.assertIn("# 新的一级标题", saved)
        self.assertNotIn("# 旧的一级标题", saved)
        self.assertEqual(len(atx_h1_lines(saved)), 1, atx_h1_lines(saved))

    def test_title_update_prepends_h1_when_body_has_none(self):
        project = self.make_project("wave1_finh1b_", with_state=False)
        entry_dir = project / "library" / "tech" / "2026-10-27-noh1"
        entry_dir.mkdir(parents=True)
        entry_path = entry_dir / "entry.md"
        write_entry(entry_path, {
            "id": "2026-10-27-noh1",
            "title": "无标题正文",
            "category": "tech",
            "status": "archived",
            "confidence": 1.0,
            "url": "https://example.com/noh1",
            "canonical_url": "https://example.com/noh1",
        }, "## 全文\n\n正文内容。\n")

        res = subprocess.run(
            [sys.executable, "-B", str(project / "scripts" / "finalize_entry.py"),
             "--path", str(entry_path), "--title-zh", "补上的标题"],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        _, saved = read_entry(entry_path)
        self.assertTrue(saved.lstrip().startswith("# 补上的标题"), saved[:80])
        self.assertEqual(len(atx_h1_lines(saved)), 1)


class TestHeadingNormalization(unittest.TestCase):
    def test_strips_duplicate_title_and_demotes_h1(self):
        out = normalize_body_headings(ARTICLE_BODY, ["Wave One Fixture"])
        headings = atx_h1_lines(out)
        self.assertEqual(headings, [])
        self.assertIn("## Section Two", out)
        self.assertIn("## Section Three", out)
        self.assertNotIn("# Wave One Fixture", out)
        # fence content and body metadata lines are untouched
        self.assertIn("# this is a shell comment, not a heading", out)
        self.assertIn("URL: https://example.com/inside-body", out)
        self.assertIn("Published: not a header line here", out)

    def test_demotes_leading_h1_when_title_differs(self):
        out = normalize_body_headings("# Different Source Title\n\nBody.\n", ["Wave One Fixture"])
        self.assertTrue(out.startswith("## Different Source Title"))
        self.assertEqual(atx_h1_lines(out), [])

    def test_tilde_fence_comments_preserved(self):
        text = "~~~python\n# not a heading\n~~~\n\n# Real Heading\n\nBody\n"
        out = normalize_body_headings(text, [])
        self.assertIn("# not a heading", out)
        self.assertIn("## Real Heading", out)

    def test_web_extract_header_keeps_body_url_lines(self):
        text = (
            "# Wave One Fixture\n"
            "URL: https://example.com/post\n"
            "Published: 2026-10-27\n"
            "\n"
            "Intro paragraph.\n"
            "\n"
            "URL: https://example.com/body-link\n"
            "Published: 2026-01-01\n"
        )
        out = strip_web_extract_header(text, "Wave One Fixture")
        self.assertNotIn("https://example.com/post", out)
        self.assertIn("URL: https://example.com/body-link", out)
        self.assertIn("Published: 2026-01-01", out)

    def test_truncation_note_is_stripped_and_reported(self):
        text = "Article body text.\n\n---\n[Page truncated: showing first 15000 chars; full text saved to /tmp/x.txt]"
        cleaned, matched = strip_truncation_note(text)
        self.assertTrue(matched)
        self.assertEqual(cleaned, "Article body text.")
        untouched, matched2 = strip_truncation_note("Article body text.")
        self.assertFalse(matched2)
        self.assertEqual(untouched, "Article body text.")


class TestContentHashing(unittest.TestCase):
    def test_partial_banner_not_counted_in_short_threshold(self):
        body_text = "字" * 780
        entry_body = (
            "## 全文\n\n"
            "> ⚠️ **部分抓取**：本文正文在约 780 字符处截断，以下内容不完整。\n\n"
            f"{body_text}\n"
        )
        self.assertEqual(entry_char_count({}, entry_body), 780)
        self.assertLess(entry_char_count({}, entry_body), common.SHORT_ARTICLE_CHARS)

    def test_hash_ignores_banner_and_surrounding_sections(self):
        full = "## 全文\n\n正文内容。\n"
        banner = "## 全文\n\n> ⚠️ **部分抓取**：本文正文在约 5 字符处截断，以下内容不完整。\n\n正文内容。\n"
        self.assertEqual(compute_content_hash(full), compute_content_hash(banner))
        self.assertTrue(compute_content_hash(full).startswith("sha256:"))
        self.assertNotEqual(compute_content_hash(full), compute_content_hash("## 全文\n\n别的内容。"))

    def test_hash_invariant_across_image_localization(self):
        remote = "## 全文\n\n第一段。\n\n![架构图](https://mmbiz.qpic.cn/sz_mmbiz_png/abc?wx_fmt=png)\n\n第二段。\n"
        localized = "## 全文\n\n第一段。\n\n![架构图](images/001.png)\n\n第二段。\n"
        self.assertEqual(compute_content_hash(remote), compute_content_hash(localized))

    def test_docs_fragment_dedupe(self):
        self.assertEqual(
            dedupe_key("https://docs.bigmodel.cn/cn/update/new-releases#v4"),
            "https://docs.bigmodel.cn/cn/update/new-releases#v4",
        )
        self.assertEqual(dedupe_key("https://example.com/a#frag"), "https://example.com/a")
        self.assertTrue(dedupe_key("https://example.com/a#frag", keep_fragment=True).endswith("#frag"))
        self.assertFalse(dedupe_key("https://docs.bigmodel.cn/x#v1", keep_fragment=False).endswith("#v1"))


class TestAtomicHelpers(SandboxCase):
    def test_write_text_atomic_and_state_lock(self):
        d = self.temp_dir("wave1_lock_")
        target = d / "nested" / "file.txt"
        write_text_atomic(target, "hello")
        self.assertEqual(target.read_text(encoding="utf-8"), "hello")
        write_text_atomic(target, "world")
        self.assertEqual(target.read_text(encoding="utf-8"), "world")
        self.assertEqual([p.name for p in target.parent.iterdir()], ["file.txt"])

        with state_lock(d) as lock_path:
            self.assertTrue(lock_path.exists())
        self.assertTrue((d / ".state" / ".lock").exists())


class TestWebContentTypeGate(unittest.TestCase):
    class _Resp:
        def __init__(self, content_type: str):
            self.headers = {"Content-Type": content_type}

    def test_non_article_media_rejected(self):
        for ct in (
            "application/pdf",
            "application/pdf; charset=binary",
            "application/zip",
            "application/octet-stream",
            "image/png",
            "video/mp4",
            "audio/mpeg",
        ):
            with self.subTest(content_type=ct):
                with self.assertRaises(ValueError) as ctx:
                    check_web_content_type(self._Resp(ct))
                self.assertIn("不支持非网页文章媒体类型", str(ctx.exception))

    def test_html_allowed(self):
        for ct in ("text/html; charset=utf-8", "application/xhtml+xml", "text/plain", ""):
            check_web_content_type(self._Resp(ct))


class TestWechatExtract(unittest.TestCase):
    HTML = """
    <html lang="zh"><body>
      <h1 id="activity-name">微信测试文章</h1>
      <a id="js_name">测试公众号</a>
      <span id="js_author_name">作者甲</span>
      <div id="js_content"><p>正文第一段。</p><p>正文第二段。</p></div>
      <script>window.ct = '1700000000';</script>
    </body></html>
    """

    HTML_META_ONLY = """
    <html lang="zh"><head>
      <meta property="article:published_time" content="2026-10-26T09:30:00+08:00" />
    </head><body>
      <h1 id="activity-name">meta 时间文章</h1>
      <div id="js_content"><p>正文。</p></div>
    </body></html>
    """

    def test_single_quoted_window_ct(self):
        meta, content, _ = wechat_extract(self.HTML, "https://mp.weixin.qq.com/s/abc")
        self.assertTrue(meta["published"].startswith("2023-11-15"), meta["published"])
        self.assertIn("正文第一段", content)

    def test_meta_published_fallback(self):
        meta, _, _ = wechat_extract(self.HTML_META_ONLY, "https://mp.weixin.qq.com/s/def")
        self.assertTrue(meta["published"].startswith("2026-10-26"), meta["published"])


class TestArchiveUrlDedupeAndForce(SandboxCase):
    def test_content_hash_blocks_second_url_with_same_body(self):
        project = self.make_project("wave1_dedupe_")
        content = self.scratch_file("article.md", ARTICLE_BODY)

        code1, res1 = self.run_archive(
            project,
            "https://example.com/post-a",
            "--content-file", str(content),
            "--original-title", "Wave One Fixture",
            "--category", "tech",
            "--language", "en",
        )
        self.assertEqual(code1, 0, res1)
        self.assertEqual(res1["status"], "ok")

        code2, res2 = self.run_archive(
            project,
            "https://example.com/post-b",
            "--content-file", str(content),
            "--original-title", "Wave One Fixture",
            "--category", "tech",
            "--language", "en",
        )
        self.assertEqual(code2, 0, res2)
        self.assertEqual(res2["status"], "duplicate")
        self.assertEqual(res2["matched_by"], "content_hash")

        entry_dirs = [p.parent for p in (project / "library").rglob("entry.md")]
        self.assertEqual(len(entry_dirs), 1, entry_dirs)

        seen = json.loads((project / ".state" / "seen.json").read_text(encoding="utf-8"))
        self.assertEqual(len(seen["entries"]), 1)

        # The stored body has exactly one H1 and the demoted section headings.
        fm, body = read_entry(entry_dirs[0] / "entry.md")
        self.assertEqual([t for _, t in atx_h1_lines(body)], [fm["title"]])
        self.assertIn("## Section Two", body)
        self.assertIn("# this is a shell comment, not a heading", body)
        self.assertTrue(fm["content_hash"].startswith("sha256:"))

    def test_force_moves_old_entry_to_trash_without_duplicating(self):
        project = self.make_project("wave1_force_")
        content = self.scratch_file("article.md", ARTICLE_BODY)
        url = "https://example.com/force-me"

        code1, res1 = self.run_archive(
            project,
            url,
            "--content-file", str(content),
            "--original-title", "Wave One Fixture",
            "--category", "tech",
            "--context", "work",
            "--tags", "alpha,beta",
            "--language", "en",
        )
        self.assertEqual(code1, 0, res1)
        self.assertEqual(res1["status"], "ok")
        first_dir = Path(res1["path"]).parent

        code2, res2 = self.run_archive(
            project,
            url,
            "--content-file", str(content),
            "--original-title", "Wave One Fixture",
            "--force",
            "--language", "en",
        )
        self.assertEqual(code2, 0, res2)
        self.assertEqual(res2["status"], "ok")
        second_dir = Path(res2["path"]).parent

        # No duplicate: exactly one live entry directory, same id, no "-2" suffix.
        entry_dirs = [p.parent for p in (project / "library").rglob("entry.md")]
        self.assertEqual(len(entry_dirs), 1, entry_dirs)
        self.assertEqual(second_dir, first_dir)
        self.assertRegex(second_dir.name, r"\d{4}-\d{2}-\d{2}-wave-one-fixture$")

        # The previous copy is soft-moved to trash/, never deleted.
        trash_dirs = [p for p in (project / "trash").iterdir() if p.is_dir()]
        self.assertEqual(len(trash_dirs), 1, trash_dirs)
        self.assertTrue((trash_dirs[0] / "entry.md").exists())

        # Metadata inherited from the replaced entry when not passed again.
        fm2, _ = read_entry(second_dir / "entry.md")
        self.assertEqual(fm2["category"], "tech")
        self.assertEqual(fm2["context"], "work")
        self.assertEqual(fm2["tags"], ["alpha", "beta"])

        seen = json.loads((project / ".state" / "seen.json").read_text(encoding="utf-8"))
        self.assertEqual(len(seen["entries"]), 1)
        self.assertEqual(Path(seen["entries"][dedupe_key(url)]["path"]).parent.name, second_dir.name)

    def test_truncation_note_forces_partial_status(self):
        project = self.make_project("wave1_partial_")
        content = self.scratch_file(
            "article.md",
            ARTICLE_BODY + "\n---\n[Page truncated: showing first 15000 chars. Full text saved to /tmp/full.md]\n",
        )
        code, res = self.run_archive(
            project,
            "https://example.com/truncated",
            "--content-file", str(content),
            "--original-title", "Wave One Fixture",
            "--category", "tech",
            "--language", "en",
        )
        self.assertEqual(code, 0, res)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["entry_status"], "partial")
        self.assertTrue(res["content_note"])

        fm, body = read_entry(Path(res["path"]))
        self.assertEqual(fm["status"], "partial")
        self.assertIn("部分抓取", body)
        self.assertNotIn("full text saved to", body)

    def test_docs_host_inferred_source_type(self):
        project = self.make_project("wave1_docs_")
        content = self.scratch_file("docs.md", "# Release Notes\n\nVersion 4 details.\n")
        code, res = self.run_archive(
            project,
            "https://docs.bigmodel.cn/cn/update/new-releases#v4",
            "--content-file", str(content),
            "--extraction", "web_extract",
            "--original-title", "Release Notes",
            "--category", "tech",
            "--language", "en",
        )
        self.assertEqual(code, 0, res)
        fm, _ = read_entry(Path(res["path"]))
        self.assertEqual(fm["source_type"], "docs")
        self.assertTrue(fm["url"].endswith("#v4"))


if __name__ == "__main__":
    unittest.main()
