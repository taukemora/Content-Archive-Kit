"""Tests for the encrypted reader's visible-first media scheduler.

Two layers:

* ``NodeSchedulerBehaviourTests`` runs ``tests/reader_media_scheduler.mjs``,
  which loads the shipped ``scripts/encrypted_reader.js`` through its CommonJS
  test seam and drives the real scheduler with a simulated DOM, injected
  geometry, and controlled load promises. This runs in normal CI when ``node``
  is available (the repository already uses Node for WebCrypto interop tests).
* ``SchedulerSourceContractTests`` pins the browser wiring and the placeholder
  CSS that makes pending encrypted images measurable.
* ``BrowserMediaAcceptance`` is opt-in (``CA_RUN_BROWSER_TESTS=1``). It builds
  font-independent synthetic galleries, holds the first response wave at the
  test server, and checks real Chromium fetch timing, per-request geometry,
  decoded ``naturalWidth`` values, scroll-driven hydration, and hide/resume
  behaviour of the same mobile mount.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402
from test_encrypted_site import PASS, EncryptedSiteBase  # noqa: E402

ROOT = _support.ROOT
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


class NodeSchedulerBehaviourTests(unittest.TestCase):
    def test_real_scheduler_behaviour(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available")
        proc = subprocess.run(
            [node, str(ROOT / "tests" / "reader_media_scheduler.mjs")],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(report["ok"], json.dumps(report, ensure_ascii=False, indent=2))
        self.assertGreaterEqual(report["total"], 15)
        self.assertEqual(report["failed"], 0)
        names = {case["name"] for case in report["cases"]}
        for expected in (
            "concurrency is capped and the far image is not requested",
            "a freed slot re-sorts pending jobs by current visibility",
            "stop aborts in-flight loads and swallows late writes",
            "a hidden pane pauses scheduling until it is measurable again",
            "zero-size nodes are never treated as visible",
            "a job with one visible occurrence outranks its distant first node",
            "duplicate occurrences share one request and hydrate every node",
        ):
            self.assertIn(expected, names)


class SchedulerSourceContractTests(unittest.TestCase):
    def setUp(self):
        self.js = (SCRIPTS / "encrypted_reader.js").read_text(encoding="utf-8")
        self.html = (SCRIPTS / "archive_page.html").read_text(encoding="utf-8")

    def test_bounded_visible_first_configuration(self):
        self.assertIn("createMediaScheduler", self.js)
        self.assertIn("concurrency: 3", self.js)
        self.assertIn("prefetchFactor: 2", self.js)
        self.assertIn("ResizeObserver", self.js)
        self.assertIn("addEventListener('scroll'", self.js)
        self.assertNotIn("Promise.all", self.js)
        self.assertIn("loading = 'eager'", self.js)

    def test_security_checks_and_limits_are_preserved(self):
        self.assertIn("IMAGE_MIME_RE", self.js)
        self.assertIn("fetchBytes(job.rel, MAX_IMAGE_BYTES", self.js)
        self.assertIn("oidOf(rel)", self.js)
        self.assertIn("stillValid(myEpoch, renderEpoch)", self.js)
        self.assertIn("URL.createObjectURL", self.js)
        self.assertIn("URL.revokeObjectURL", self.js)
        # No plaintext is ever assigned as an <img> src and no untrusted href
        # survives a failure.
        self.assertIn("node.setAttribute('href', '#')", self.js)

    def test_scheduler_is_stopped_for_every_exit_path(self):
        self.assertGreaterEqual(self.js.count("stopMedia()"), 4)
        for marker in (
            "stopMedia();\n    bodyAbort = new AbortController();",
            "stopMedia();\n    revokeBlobUrls();",
        ):
            self.assertIn(marker, self.js)

    def test_pending_encrypted_images_keep_measurable_geometry(self):
        import re

        match = re.search(r"\.article-body img\[data-ca-src\]:not\(\[data-ca-ready\]\) \{([^}]*)\}", self.html)
        self.assertIsNotNone(match, "pending encrypted image rule is missing")
        rule = match.group(1)
        self.assertNotIn("display: none", rule)
        self.assertIn("display: block", rule)
        # A minimum box is the fallback when aspect-ratio is unsupported.
        self.assertIn("min-height", rule)
        self.assertIn("aspect-ratio", rule)
        # The placeholder must survive assigning the blob src and only end
        # after the decoded image fires load.
        self.assertIn("data-ca-ready", self.js)
        self.assertIn("node.addEventListener('load'", self.js)
        self.assertIn("node.addEventListener('error'", self.js)
        block = self.js.split("function hydrateImage", 1)[1].split("function stopMedia", 1)[0]
        self.assertLess(
            block.index("node.addEventListener('load'"),
            block.index("node.src = url"),
            "load/error listeners must be registered before src is assigned",
        )

    def test_scheduler_measures_every_occurrence(self):
        block = self.js.split("measure: function (job) {", 1)[1].split("viewport: function", 1)[0]
        self.assertIn("job.nodes.length", block)
        self.assertIn("rects.push", block)
        # A hidden pane is reported as unmeasurable instead of falling back to
        # the window, which made every zero-height node look visible.
        viewport_block = self.js.split("viewport: function () {", 1)[1].split("load: function", 1)[0]
        self.assertIn("return null", viewport_block)


def _png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """Minimal valid RGB PNG without an imaging dependency."""
    row = b"\x00" + bytes(rgb) * width
    raw = row * height

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


class BrowserMediaAcceptance(EncryptedSiteBase):
    """Opt-in real-Chromium check of prefetch timing and decoded pixels."""

    @staticmethod
    def _write_gallery(entry, specs, shrink_index=None, duplicate_last=False):
        """Replace an entry body with an image-only gallery.

        Block heights then come from the fixed pending aspect-ratio instead of
        wrapped prose, so the measured layout does not drift with font metrics.
        ``shrink_index`` swaps one image for a wider/shorter one: when it
        decodes, everything below it moves up. That reflow is deliberate -- the
        request-time assertions must judge each request by the layout that
        existed when it was issued, not by the initial placeholder layout.
        """
        from common import read_entry, write_entry

        images = entry / "images"
        images.mkdir(exist_ok=True)
        sizes = list(specs)
        if shrink_index is not None:
            sizes[shrink_index] = (1200, 320)
        fm, _ = read_entry(entry / "entry.md")
        parts: list[str] = []
        for index, (width, height) in enumerate(sizes):
            (images / f"m{index}.png").write_bytes(
                _png(width, height, (20 + index * 20, 90, 140))
            )
            parts.append(f"![合成图{index}](images/m{index}.png)")
        if duplicate_last:
            # The same object appears again as an image and as an attachment
            # link beyond the prefetch window: one fetch must hydrate both.
            last = len(sizes) - 1
            parts.append(f"![重复图{last}](images/m{last}.png)")
            parts.append(f"[重复图{last}附件](images/m{last}.png)")
        write_entry(entry / "entry.md", fm, "\n\n".join(parts))

    @unittest.skipUnless(
        os.environ.get("CA_RUN_BROWSER_TESTS") == "1",
        "set CA_RUN_BROWSER_TESTS=1",
    )
    def test_visible_first_prefetch_and_scroll_hydration(self):
        # Desktop fixture: eight 16:9 images (the pending placeholder box is
        # also 16:9, so decoding changes no height) plus one short image that
        # shrinks and reflows the gallery below it. The repeated object sits at
        # the far end, beyond the two-screen prefetch window.
        self._write_gallery(
            self.root / "library" / "coding" / "2026-01-01-alpha",
            [(800, 450)] * 8,
            shrink_index=2,
            duplicate_last=True,
        )
        # Mobile fixture: four short images, all initially inside the two-screen
        # window at 390x844, so one job stays pending and eligible behind the
        # three concurrent first-wave jobs.
        self._write_gallery(
            self.root / "library" / "coding" / "2026-01-02-beta",
            [(800, 450)] * 4,
        )
        self.build()

        python = os.environ.get("CA_PLAYWRIGHT_PYTHON") or sys.executable
        if not Path(python).is_file():
            self.skipTest("playwright python not available: set CA_PLAYWRIGHT_PYTHON")
        probe = subprocess.run(
            [python, "-c", "import playwright.sync_api"], capture_output=True, text=True
        )
        if probe.returncode != 0:
            self.skipTest(f"playwright is not installed for {python}")

        cmd = [
            python,
            str(ROOT / "tests" / "browser_encrypted_reader.py"),
            "--dir",
            str(self.output),
            "--passphrase",
            PASS,
            "--mode",
            "media",
            "--article-title",
            "加密标题甲",
            "--mobile-article-title",
            "加密标题乙",
            "--repeat-link-text",
            "重复图7附件",
        ]
        browser = os.environ.get("CA_PLAYWRIGHT_BROWSER")
        if browser:
            cmd += ["--browser", browser]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(report["ok"], report)


if __name__ == "__main__":
    unittest.main()
