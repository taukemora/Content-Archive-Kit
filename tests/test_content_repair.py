#!/usr/bin/env python3
"""Batch 1 repair tests: explicit-limit Exa acquisition and media preservation.

All writes stay under the platform temporary directory.  The instance
``.state/seen.json`` hash is asserted unchanged at teardown.
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
SCRIPTS = ROOT / "scripts"
SCRATCH = _support.SCRATCH
sys.path.insert(0, str(SCRIPTS))

import archive_url  # noqa: E402
import common  # noqa: E402
import extract_remote  # noqa: E402
import media_preservation  # noqa: E402

PROD_SEEN = ROOT / ".state" / "seen.json"


def prod_seen_sha() -> str | None:
    """Digest of instance state, ``None`` when the archive has none."""
    return _support.state_digest(PROD_SEEN)


def sse(payload: dict) -> bytes:
    return ("event: message\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def ok_payload(text: str, meta: dict | None = None) -> dict:
    block = {"type": "text", "text": text}
    if meta:
        block["_meta"] = meta
    return {"jsonrpc": "2.0", "id": 1, "result": {"content": [block]}}


class SandboxCase(unittest.TestCase):
    def setUp(self):
        self._dirs: list[Path] = []
        self._prod_sha = prod_seen_sha()

    def tearDown(self):
        for d in self._dirs:
            shutil.rmtree(d, ignore_errors=True)
        self.assertEqual(prod_seen_sha(), self._prod_sha, "production seen.json was modified")

    def temp_dir(self, prefix: str) -> Path:
        d = Path(tempfile.mkdtemp(dir=str(SCRATCH), prefix=prefix))
        self._dirs.append(d)
        return d


class TestExaPayloadAndParsing(unittest.TestCase):
    def test_payload_sets_explicit_max_characters(self):
        payload = extract_remote.build_payload(["https://example.com/a"], 50000)
        self.assertEqual(payload["method"], "tools/call")
        self.assertEqual(payload["params"]["name"], "web_fetch_exa")
        args = payload["params"]["arguments"]
        self.assertEqual(args["urls"], ["https://example.com/a"])
        self.assertEqual(args["maxCharacters"], 50000)

    def test_payload_rejects_missing_or_bad_limit(self):
        with self.assertRaises(ValueError):
            extract_remote.build_payload(["https://example.com/a"], 0)
        with self.assertRaises(ValueError):
            extract_remote.build_payload(["https://example.com/a"], "50000")  # type: ignore[arg-type]

    def test_parse_sse_text(self):
        raw = sse(ok_payload("hello world", {"searchTime": 1.5}))
        parsed = extract_remote.parse_mcp_response(raw)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["content"], "hello world")
        self.assertEqual(parsed["meta"], {"searchTime": 1.5})

    def test_parse_plain_json(self):
        raw = json.dumps(ok_payload("plain body")).encode("utf-8")
        parsed = extract_remote.parse_mcp_response(raw)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["content"], "plain body")

    def test_is_error_flag(self):
        payload = ok_payload("blocked by upstream")
        payload["result"]["isError"] = True
        parsed = extract_remote.parse_mcp_response(sse(payload))
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["error"]["code"], "upstream_is_error")
        self.assertIn("blocked", parsed["error"]["message"])

    def test_jsonrpc_error(self):
        payload = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad params"}}
        parsed = extract_remote.parse_mcp_response(sse(payload))
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["error"]["code"], "jsonrpc_error")
        self.assertIn("bad params", parsed["error"]["message"])

    def test_empty_content(self):
        parsed = extract_remote.parse_mcp_response(sse(ok_payload("   ")))
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["error"]["code"], "empty_content")

    def test_invalid_payload(self):
        parsed = extract_remote.parse_mcp_response(b"not json at all")
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["error"]["code"], "invalid_response")


class TestExaExtraction(SandboxCase):
    def test_metadata_files_and_unverified_completeness(self):
        out = self.temp_dir("repair_exa_")
        text = "A" * 120
        with mock.patch.object(extract_remote, "_post_mcp", return_value=(200, sse(ok_payload(text)))):
            result = extract_remote.extract_url(
                "https://openai.com/index/legora",
                max_characters=50000,
                out_dir=out,
            )
        self.assertEqual(result["fetch_status"], "ok")
        self.assertEqual(result["completeness"], "unverified")
        self.assertEqual(result["archive_status"], "not_archived")
        self.assertEqual(result["requested_url"], "https://openai.com/index/legora")
        self.assertEqual(result["provider"], "exa_mcp")
        self.assertEqual(result["max_characters"], 50000)
        self.assertEqual(result["content_chars"], 120)
        self.assertFalse(result["limit_reached"])
        self.assertTrue((out / "content.md").read_text(encoding="utf-8") == text)
        self.assertTrue((out / "raw.sse.txt").exists())
        meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
        for key in (
            "requested_url",
            "fetched_at",
            "provider",
            "max_characters",
            "content_chars",
            "completeness",
            "archive_status",
        ):
            self.assertIn(key, meta)
        self.assertEqual(meta["completeness"], "unverified")
        self.assertNotEqual(meta.get("status"), "complete")

    def test_limit_hit_is_recognisable_but_still_unverified(self):
        out = self.temp_dir("repair_exa_limit_")
        text = "B" * 100
        with mock.patch.object(extract_remote, "_post_mcp", return_value=(200, sse(ok_payload(text)))):
            result = extract_remote.extract_url("https://example.com/a", max_characters=100, out_dir=out)
        self.assertEqual(result["content_chars"], 100)
        self.assertTrue(result["limit_reached"])
        self.assertEqual(result["completeness"], "unverified")
        meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
        self.assertTrue(meta["limit_reached"])

    def test_ssrf_blocked_before_post(self):
        out = self.temp_dir("repair_exa_ssrf_")
        with mock.patch.object(extract_remote, "_post_mcp", side_effect=AssertionError("must not post")):
            result = extract_remote.extract_url("http://127.0.0.1/private", out_dir=out)
        self.assertEqual(result["fetch_status"], "error")
        self.assertEqual(result["error_code"], "blocked_url")
        self.assertEqual(result["completeness"], "unverified")

    def test_response_too_large(self):
        out = self.temp_dir("repair_exa_big_")
        with mock.patch.object(extract_remote, "_post_mcp", return_value=(200, b"x" * 2048)):
            result = extract_remote.extract_url(
                "https://example.com/a", out_dir=out, max_response_bytes=1024
            )
        self.assertEqual(result["fetch_status"], "error")
        self.assertEqual(result["error_code"], "response_too_large")

    def test_upstream_error_writes_metadata(self):
        out = self.temp_dir("repair_exa_err_")
        payload = ok_payload("rate limited")
        payload["result"]["isError"] = True
        with mock.patch.object(extract_remote, "_post_mcp", return_value=(200, sse(payload))):
            result = extract_remote.extract_url("https://example.com/a", out_dir=out)
        self.assertEqual(result["fetch_status"], "error")
        self.assertEqual(result["error_code"], "upstream_is_error")
        meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["error_code"], "upstream_is_error")
        self.assertEqual(meta["completeness"], "unverified")

    def test_timeout(self):
        import requests

        out = self.temp_dir("repair_exa_timeout_")
        with mock.patch.object(extract_remote, "_post_mcp", side_effect=requests.Timeout("slow")):
            result = extract_remote.extract_url("https://example.com/a", out_dir=out)
        self.assertEqual(result["fetch_status"], "error")
        self.assertEqual(result["error_code"], "timeout")

    def test_output_dir_must_be_scratch_or_state(self):
        with self.assertRaises(ValueError):
            extract_remote.validate_output_dir("/etc")
        with self.assertRaises(ValueError):
            extract_remote.validate_output_dir(ROOT)
        inside = self.temp_dir("repair_exa_allow_")
        self.assertEqual(extract_remote.validate_output_dir(inside), inside.resolve())

    def test_refuses_to_overwrite_without_force(self):
        out = self.temp_dir("repair_exa_overwrite_")
        (out / "content.md").write_text("old", encoding="utf-8")
        with mock.patch.object(extract_remote, "_post_mcp", return_value=(200, sse(ok_payload("new")))):
            result = extract_remote.extract_url("https://example.com/a", out_dir=out)
        self.assertEqual(result["fetch_status"], "error")
        self.assertEqual(result["error_code"], "output_exists")
        self.assertEqual((out / "content.md").read_text(encoding="utf-8"), "old")


class TestMediaPreservation(unittest.TestCase):
    GOOGLE_HTML = """
    <html><body><article>
      <p>Intro paragraph.</p>
      <uni-youtube-player-article video-id="oM5QbIJzeH0"
        page-title="Beam video" thumbnail-alt="Beam video"></uni-youtube-player-article>
      <h2>Chart section</h2>
      <svg role="img" viewBox="0 0 100 40"
           aria-label="Tokens per answer for each setup">
        <text x="1" y="10">SQLite MCP</text><text x="40" y="10">172k</text>
        <text x="1" y="20">Markdown</text><text x="40" y="20">7.5M</text>
      </svg>
      <script>alert('do not keep')</script>
    </article></body></html>
    """

    def test_extracts_google_youtube_component(self):
        videos = media_preservation.extract_youtube_videos(self.GOOGLE_HTML)
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["video_id"], "oM5QbIJzeH0")
        self.assertEqual(videos[0]["url"], "https://www.youtube.com/watch?v=oM5QbIJzeH0")

    def test_appends_video_link_and_static_svg_text(self):
        content = "Body only.\n"
        new, report = media_preservation.preserve_media(content, self.GOOGLE_HTML, "https://blog.google/x")
        self.assertTrue(report["appended"])
        self.assertIn("## 媒体保留", new)
        self.assertIn("https://www.youtube.com/watch?v=oM5QbIJzeH0", new)
        self.assertIn("SQLite MCP", new)
        self.assertIn("172k", new)
        self.assertNotIn("<script", new)
        self.assertNotIn("alert(", new)
        self.assertNotIn("<iframe", new)

    def test_no_duplicate_video_when_body_already_links_it(self):
        content = "Watch https://www.youtube.com/watch?v=oM5QbIJzeH0 here.\n"
        new, report = media_preservation.preserve_media(content, self.GOOGLE_HTML, "https://blog.google/x")
        self.assertEqual(new.count("oM5QbIJzeH0"), 1)
        self.assertTrue(report["video_already_present"])

    def test_idempotent(self):
        once, _ = media_preservation.preserve_media("Body.\n", self.GOOGLE_HTML, "https://blog.google/x")
        twice, report = media_preservation.preserve_media(once, self.GOOGLE_HTML, "https://blog.google/x")
        self.assertEqual(once, twice)
        self.assertFalse(report["appended"])

    def test_no_media_keeps_content_unchanged(self):
        html = "<html><body><article><p>Text.</p></article></body></html>"
        new, report = media_preservation.preserve_media("Text.\n", html, "https://example.com/a")
        self.assertEqual(new, "Text.\n")
        self.assertFalse(report["appended"])

    def test_logo_role_img_without_label_is_ignored(self):
        html = '<svg role="img"><use href="/icons.svg#logo"></use></svg>'
        self.assertEqual(media_preservation.extract_inline_svg_charts(html), [])

    def test_sanitize_svg_removes_executable_and_remote_bits(self):
        dirty = """
        <svg role="img" aria-label="Chart" onload="alert(1)">
          <script>alert(2)</script>
          <foreignObject><div>html</div></foreignObject>
          <image href="https://tracker.example/pixel.png"></image>
          <use href="https://evil.example/icons.svg#x"></use>
          <use href="#local"></use>
          <a href="javascript:alert(3)"><text>bad</text></a>
          <text onclick="alert(4)" fill="url(https://evil.example/paint)" style="fill:url(https://evil.example/x)">Safe label</text>
        </svg>
        """
        clean = media_preservation.sanitize_svg(dirty)
        self.assertIn("Safe label", clean)
        self.assertIn('href="#local"', clean)
        for bad in ("<script", "foreignObject", "onload", "onclick", "javascript:", "https://"):
            self.assertNotIn(bad, clean)

    def test_svg_text_is_escaped_not_live_html(self):
        html = """
        <svg role="img" aria-label="Perf chart">
          <text x="1" y="10">Setup A</text>
          <text x="40" y="10">&lt;img src=x onerror=alert(1)&gt;</text>
          <text x="1" y="20">Setup B</text><text x="40" y="20">7.5M</text>
        </svg>
        """
        section, report = media_preservation.build_media_appendix(html, "Body.\n")
        self.assertTrue(report["appended"])
        # No live HTML element may reach the Markdown body.
        self.assertNotIn("<img", section)
        self.assertNotIn("<script", section)
        # Readable text is preserved, escaped so a renderer shows it literally.
        self.assertIn("&lt;img", section)
        self.assertIn("Setup A", section)
        self.assertIn("7.5M", section)

    def test_markdown_image_and_link_injection_is_defused(self):
        html = """
        <h2>Chart &lt;script&gt;alert('ctx')&lt;/script&gt;</h2>
        <svg role="img" aria-label="![pixel](https://tracker.example/p.png)">
          <text x="1" y="10">[click](https://evil.example/landing)</text>
          <text x="40" y="10">&lt;https://evil.example/auto&gt;</text>
        </svg>
        """
        section, _ = media_preservation.build_media_appendix(html, "Body.\n")
        # No active Markdown image/link/autolink syntax survives.
        self.assertNotIn("![", section)
        self.assertNotIn("](", section)
        self.assertNotIn("](https", section)
        self.assertNotIn("<https://", section)
        self.assertNotIn("<img", section)
        self.assertNotIn("<script>", section)
        self.assertNotIn("https://", section)  # no bare-URL autolinking either
        # Text stays present (escaped) so the appendix is still readable.
        self.assertIn("pixel", section)
        self.assertIn("click", section)
        self.assertIn("Chart", section)

    def test_svg_text_rows_are_ordered(self):
        html = """
        <svg role="img" aria-label="Chart">
          <text x="50" y="10">Right</text><text x="1" y="10">Left</text>
          <text x="1" y="30">Bottom</text>
        </svg>
        """
        charts = media_preservation.extract_inline_svg_charts(html)
        self.assertEqual(len(charts), 1)
        self.assertEqual(charts[0]["rows"], ["Left | Right", "Bottom"])


class FakeResponse:
    status_code = 200
    headers = {"Content-Type": "text/html; charset=utf-8"}
    is_redirect = False

    def __init__(self, text: str):
        self.text = text
        self.content = text.encode("utf-8")

    def raise_for_status(self):
        return None

    def close(self):
        return None


class TestArchiveMediaIntegration(SandboxCase):
    HTML = """
    <html><head><title>Google media fixture</title></head><body>
      <article>
        <h1>Google media fixture</h1>
        <p>First paragraph with enough text to be treated as an article body by the extractor.</p>
        <uni-youtube-player-hero video-id="abcdefghijk" page-title="Hero video"></uni-youtube-player-hero>
        <h2>Results</h2>
        <svg role="img" viewBox="0 0 100 40" aria-label="Results chart">
          <text x="1" y="10">Setup A</text><text x="40" y="10">42</text>
          <text x="1" y="20">Setup B</text><text x="40" y="20">100</text>
        </svg>
        <p>Second paragraph after the chart, also long enough for extraction.</p>
      </article>
    </body></html>
    """

    def make_project(self) -> Path:
        base = self.temp_dir("repair_media_project_")
        shutil.copytree(SCRIPTS, base / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("taxonomy.yaml", "sources.yaml"):
            shutil.copy2(_support.template(name), base / name)
        (base / "library").mkdir()
        (base / ".state").mkdir()
        (base / ".state" / "seen.json").write_text('{"entries": {}}', encoding="utf-8")
        return base

    def test_archive_add_preserves_media_without_scripts(self):
        project = self.make_project()
        argv = [
            "archive_url.py",
            "https://blog.google/technology/ai/media-fixture",
            "--title-zh",
            "媒体保留测试",
            "--category",
            "tech",
            "--context",
            "reference",
        ]
        with mock.patch.object(archive_url, "ROOT", project), mock.patch.object(
            common, "ROOT", project
        ), mock.patch.object(archive_url, "assert_public_url", lambda url: None), mock.patch.object(
            archive_url, "fetch", lambda *a, **k: FakeResponse(self.HTML)
        ), mock.patch.object(sys, "argv", argv):
            code = archive_url.main()
        self.assertEqual(code, 0)
        entries = list((project / "library").rglob("entry.md"))
        self.assertEqual(len(entries), 1)
        body = entries[0].read_text(encoding="utf-8")
        self.assertIn("https://www.youtube.com/watch?v=abcdefghijk", body)
        self.assertIn("Setup A", body)
        self.assertIn("42", body)
        self.assertNotIn("<script", body)
        self.assertNotIn("onload", body)
        self.assertTrue((entries[0].parent / "raw.html.gz").exists())
        seen = json.loads((project / ".state" / "seen.json").read_text(encoding="utf-8"))
        self.assertEqual(len(seen["entries"]), 1)

    def test_content_file_plus_media_html_strips_exa_header(self):
        project = self.make_project()
        files = self.temp_dir("repair_media_files_")
        content_file = files / "exa.md"
        content_file.write_text(
            "# Google media fixture\nURL: https://blog.google/x\nPublished: 2026-10-29\n\nBody paragraph.\n",
            encoding="utf-8",
        )
        html_file = files / "raw.html"
        html_file.write_text(self.HTML, encoding="utf-8")
        argv = [
            "archive_url.py",
            "https://blog.google/technology/ai/media-fixture-2",
            "--title-zh",
            "Exa 媒体测试",
            "--category",
            "tech",
            "--content-file",
            str(content_file),
            "--media-html-file",
            str(html_file),
            "--extraction",
            "exa",
        ]
        with mock.patch.object(archive_url, "ROOT", project), mock.patch.object(
            common, "ROOT", project
        ), mock.patch.object(archive_url, "assert_public_url", lambda url: None), mock.patch.object(
            sys, "argv", argv
        ):
            code = archive_url.main()
        self.assertEqual(code, 0)
        entry = next((project / "library").rglob("entry.md"))
        body = entry.read_text(encoding="utf-8")
        fulltext = body.split("## 全文", 1)[1]
        self.assertNotIn("URL: https://blog.google/x", fulltext)
        self.assertIn("https://www.youtube.com/watch?v=abcdefghijk", fulltext)
        self.assertIn("Setup B", fulltext)
        self.assertNotIn("<script", fulltext)


class TestExaOutputProtection(SandboxCase):
    """Any failing run must never damage an existing successful extraction."""

    def _success(self, out: Path, text: str = "OLD-CONTENT") -> dict:
        with mock.patch.object(extract_remote, "_post_mcp", return_value=(200, sse(ok_payload(text)))):
            result = extract_remote.extract_url("https://example.com/old", out_dir=out)
        self.assertEqual(result["fetch_status"], "ok")
        return result

    def _snapshot(self, out: Path) -> tuple[str, str, bytes]:
        return (
            (out / "content.md").read_text(encoding="utf-8"),
            (out / "metadata.json").read_text(encoding="utf-8"),
            (out / "raw.sse.txt").read_bytes(),
        )

    def test_success_metadata_is_commit_marker(self):
        out = self.temp_dir("repair_exa_commit_marker_")
        self._success(out, "committed")
        meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["fetch_status"], "ok")
        self.assertNotIn("error_code", meta)
        self.assertEqual(
            [p.name for p in out.iterdir() if p.name.startswith(".staging-")], []
        )

    def test_invalid_url_without_force_keeps_existing_success(self):
        out = self.temp_dir("repair_exa_guard_url_")
        self._success(out)
        before = self._snapshot(out)
        with mock.patch.object(extract_remote, "_post_mcp", side_effect=AssertionError("must not post")):
            result = extract_remote.extract_url("", out_dir=out)
        self.assertEqual(result["fetch_status"], "error")
        self.assertIn(result["error_code"], {"output_exists", "invalid_url"})
        self.assertEqual(self._snapshot(out), before)

    def test_bad_max_argument_without_force_keeps_existing_success(self):
        out = self.temp_dir("repair_exa_guard_max_")
        self._success(out)
        before = self._snapshot(out)
        with mock.patch.object(extract_remote, "_post_mcp", side_effect=AssertionError("must not post")):
            result = extract_remote.extract_url(
                "https://example.com/new", max_characters=0, out_dir=out
            )
        self.assertEqual(result["fetch_status"], "error")
        self.assertIn(result["error_code"], {"output_exists", "invalid_argument"})
        self.assertEqual(self._snapshot(out), before)

    def test_force_failure_keeps_previous_success_output(self):
        out = self.temp_dir("repair_exa_force_fail_")
        self._success(out, "OLD-CONTENT")
        before = self._snapshot(out)
        payload = ok_payload("rate limited")
        payload["result"]["isError"] = True
        with mock.patch.object(extract_remote, "_post_mcp", return_value=(200, sse(payload))):
            result = extract_remote.extract_url(
                "https://example.com/new", out_dir=out, force=True
            )
        self.assertEqual(result["fetch_status"], "error")
        self.assertEqual(result["error_code"], "upstream_is_error")
        self.assertTrue(result.get("preserved_previous"))
        # The old success is intact: content and metadata are never mixed with
        # the failed run's raw response or error metadata.
        self.assertEqual(self._snapshot(out), before)
        meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["fetch_status"], "ok")
        self.assertNotIn("error_code", meta)

    def test_force_failure_before_fetch_keeps_previous_success_output(self):
        out = self.temp_dir("repair_exa_force_invalid_")
        self._success(out)
        before = self._snapshot(out)
        result = extract_remote.extract_url(
            "https://example.com/new", max_characters=0, out_dir=out, force=True
        )
        self.assertEqual(result["fetch_status"], "error")
        self.assertEqual(result["error_code"], "invalid_argument")
        self.assertTrue(result.get("preserved_previous"))
        self.assertEqual(self._snapshot(out), before)

    def test_commit_failure_restores_previous_version(self):
        import os

        out = self.temp_dir("repair_exa_commit_fail_")
        self._success(out, "OLD-CONTENT")
        before = self._snapshot(out)
        real_replace = os.replace

        def flaky_replace(src, dst, *args, **kwargs):
            if Path(dst) == out / "metadata.json":
                raise OSError("simulated commit failure")
            return real_replace(src, dst, *args, **kwargs)

        with mock.patch.object(
            extract_remote, "_post_mcp", return_value=(200, sse(ok_payload("NEW-CONTENT")))
        ), mock.patch.object(extract_remote.os, "replace", side_effect=flaky_replace):
            result = extract_remote.extract_url(
                "https://example.com/new", out_dir=out, force=True
            )
        self.assertEqual(result["fetch_status"], "error")
        self.assertEqual(result["error_code"], "write_error")
        self.assertTrue(result.get("preserved_previous"))
        self.assertEqual(self._snapshot(out), before)
        self.assertEqual(
            [p.name for p in out.iterdir() if p.name.startswith(".staging-")], []
        )

    def test_existing_partial_output_is_still_refused_without_force(self):
        out = self.temp_dir("repair_exa_partial_")
        (out / "raw.sse.txt").write_bytes(b"partial")
        with mock.patch.object(extract_remote, "_post_mcp", side_effect=AssertionError("must not post")):
            result = extract_remote.extract_url("", out_dir=out)
        self.assertEqual(result["error_code"], "output_exists")
        self.assertEqual((out / "raw.sse.txt").read_bytes(), b"partial")
        self.assertFalse((out / "metadata.json").exists())


class TestExaOutputPathRule(SandboxCase):
    def test_project_internal_paths_are_limited_to_state(self):
        project = self.temp_dir("repair_exa_project_")
        (project / ".state").mkdir()
        (project / "library" / "tech").mkdir(parents=True)
        with mock.patch.object(extract_remote, "ROOT", project):
            allowed = extract_remote.validate_output_dir(project / ".state" / "probe")
            self.assertEqual(allowed, (project / ".state" / "probe").resolve())
            with self.assertRaises(ValueError):
                extract_remote.validate_output_dir(project / "library" / "tech")
            with self.assertRaises(ValueError):
                extract_remote.validate_output_dir(project)

    def test_plain_scratch_dir_is_allowed(self):
        outside = self.temp_dir("repair_exa_outside_")
        project = self.temp_dir("repair_exa_project2_")
        with mock.patch.object(extract_remote, "ROOT", project):
            self.assertEqual(
                extract_remote.validate_output_dir(outside), outside.resolve()
            )


class TestArchiveDuplicateRace(SandboxCase):
    """Two runs racing on the same URL must never delete the registered winner."""

    URL = "https://blog.google/technology/ai/race-fixture"
    HTML = """
    <html><head><title>race entry</title></head><body><article>
      <h1>race entry</h1>
      <p>Winner body paragraph with plenty of text for the extractor to keep it
      alive as the article body.</p>
      <p><img src="https://blog.example/pic.png" alt="pic"></p>
    </article></body></html>
    """

    def make_project(self) -> Path:
        base = self.temp_dir("repair_race_project_")
        shutil.copytree(SCRIPTS, base / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("taxonomy.yaml", "sources.yaml"):
            shutil.copy2(_support.template(name), base / name)
        (base / "library" / "tech").mkdir(parents=True)
        (base / ".state").mkdir()
        (base / ".state" / "seen.json").write_text('{"entries": {}}', encoding="utf-8")
        return base

    def winner_hook(self, project: Path, winner_dir: Path, entry_id: str, calls: list):
        def hook(content, base_url, images_dir, referer=None, **kwargs):
            calls.append(images_dir.parent)
            winner_dir.mkdir(parents=True, exist_ok=True)
            archive_url.write_entry(
                winner_dir / "entry.md",
                {"id": entry_id, "title": "race entry", "status": "archived"},
                "# race entry\n\nWINNER-ENTRY-BODY\n",
            )
            (winner_dir / "raw.html.gz").write_bytes(b"winner-raw-bytes")
            seen = common.load_seen()
            seen.setdefault("entries", {})[archive_url.dedupe_key(self.URL)] = {
                "id": entry_id,
                "path": str((winner_dir / "entry.md").relative_to(project)),
                "first_seen": common.iso(),
            }
            common.save_seen(seen)
            return content, 0, 0, []
        return hook

    def run_archive(self, project: Path, hook) -> tuple[int, str]:
        argv = [
            "archive_url.py",
            self.URL,
            "--title-zh", "race entry",
            "--category", "tech",
            "--context", "reference",
        ]
        out = io.StringIO()
        with mock.patch.object(archive_url, "ROOT", project), mock.patch.object(
            common, "ROOT", project
        ), mock.patch.object(archive_url, "assert_public_url", lambda url: None), mock.patch.object(
            archive_url, "fetch", lambda *a, **k: FakeResponse(self.HTML)
        ), mock.patch.object(archive_url, "localize_images", hook), mock.patch.object(
            sys, "argv", argv
        ), redirect_stdout(out):
            code = archive_url.main()
        return code, out.getvalue()

    def _winner_state(self, project: Path, entry_id: str) -> Path:
        winner = project / "library" / "tech" / entry_id
        self.assertTrue((winner / "entry.md").exists(), "winner entry.md was destroyed")
        self.assertEqual((winner / "raw.html.gz").read_bytes(), b"winner-raw-bytes")
        self.assertIn("WINNER-ENTRY-BODY", (winner / "entry.md").read_text(encoding="utf-8"))
        seen = json.loads((project / ".state" / "seen.json").read_text(encoding="utf-8"))
        record = seen["entries"][archive_url.dedupe_key(self.URL)]
        self.assertEqual(record["path"], f"library/tech/{entry_id}/entry.md")
        self.assertTrue((project / record["path"]).exists())
        return winner

    def test_loser_does_not_delete_winner_sharing_the_same_directory(self):
        project = self.make_project()
        entry_id = f"{archive_url.now().strftime('%Y-%m-%d')}-race-entry"
        calls: list[Path] = []
        hook = self.winner_hook(project, project / "library" / "tech" / entry_id, entry_id, calls)
        code, output = self.run_archive(project, hook)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["status"], "duplicate")
        self.assertTrue(calls, "localize_images hook did not run")
        self._winner_state(project, entry_id)
        self.assertEqual(
            [p.name for p in (project / "library" / "tech").iterdir()], [entry_id]
        )

    def test_loser_moves_only_its_own_suffix_directory_to_trash(self):
        project = self.make_project()
        entry_id = f"{archive_url.now().strftime('%Y-%m-%d')}-race-entry"
        winner = project / "library" / "tech" / entry_id
        winner.mkdir()  # winner claimed the base name first, registers during image phase
        calls: list[Path] = []
        hook = self.winner_hook(project, winner, entry_id, calls)
        code, output = self.run_archive(project, hook)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["status"], "duplicate")
        self.assertTrue(calls, "localize_images hook did not run")
        self._winner_state(project, entry_id)
        self.assertFalse((project / "library" / "tech" / f"{entry_id}-2").exists())
        trashed = [p for p in (project / "trash").rglob(f"*{entry_id}-2") if p.is_dir()]
        self.assertTrue(trashed, "loser's own directory was not soft-moved to trash")
        self.assertNotIn(
            "WINNER-ENTRY-BODY",
            "".join(p.read_text(encoding="utf-8") for p in trashed[0].rglob("*.md")),
        )


if __name__ == "__main__":
    unittest.main()
