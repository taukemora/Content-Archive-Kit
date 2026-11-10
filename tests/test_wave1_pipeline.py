#!/usr/bin/env python3
"""Wave 1B regression tests: water mark / window, poisoned dates, raw snapshots,
SSRF guards, daily-gate candidate closure and digest scoping.

Every test builds a throwaway project under the temporary directory and
serves fixtures from a loopback HTTP server, so the production library/.state is
never touched. ``audit.py`` / ``rebuild_index.py`` are stubbed inside the throwaway
copy (they are covered by their own test modules) so that ``daily finish`` can be
exercised end-to-end without depending on the audit implementation.
"""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_sources as fetch_mod  # noqa: E402
from common import dedupe_key, iso, now  # noqa: E402

PY = _support.PYTHON
SCRATCH = _support.scratch_dir("wave1b-")
COPY = ("common.py", "fetch_sources.py", "daily_gate.py", "build_digest.py")
STUB = "#!/usr/bin/env python3\nprint('stub: ok')\nraise SystemExit(0)\n"
INDEX_FIELDS = [
    "id", "title", "source", "published", "archived", "status",
    "content_note", "url", "summary", "relevance",
]

RSS_TMPL = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>fixture</title>
<item><title>RSS One</title><link>https://openai.com/index/one</link>
<pubDate>{pub}</pubDate><description>one</description></item>
</channel></rss>"""


def _en_date(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _glm_section(sec_id: str, date_text: str, title: str, body: str) -> str:
    return f"""<div class="update" id="{sec_id}">
  <div><button data-component-part="update-label">{date_text}</button>
    <div data-component-part="update-description">{title}</div></div>
  <div data-component-part="update-content"><p>{body}</p></div>
</div>"""


def make_routes(port: int) -> dict[str, str]:
    today = now().date()
    late = today - timedelta(days=2)  # 48h old: outside the 36h window
    stale = today - timedelta(days=45)  # beyond the 30-day lookback

    news = f"""<html><body><ul>
<li><a class="title-link" href="/news/recent">Recent item</a><time>{_en_date(today)}</time></li>
<li><a class="title-link" href="/news/delayed">Delayed item</a><time>{_en_date(late)}</time></li>
</ul></body></html>"""

    news_with_old = f"""<html><body><ul>
<li><a class="title-link" href="/news/recent">Recent item</a><time>{_en_date(today)}</time></li>
<li><a class="title-link" href="/news/delayed">Delayed item</a><time>{_en_date(late)}</time></li>
<li><a class="title-link" href="/news/very-old">Ancient item</a><time>{_en_date(stale)}</time></li>
</ul></body></html>"""

    glm = "<html><body>" + "".join([
        _glm_section("2026-08-26", "2026-08-26", "GLM A 上线", "A 内容"),
        _glm_section("2026-8-19", "2026-8-19", "GLM B 上线", "B 内容"),
    ]) + "</body></html>"

    return {
        "/news": news,
        "/news-with-old": news_with_old,
        "/glm-page": glm,
        "/rss-ok.xml": RSS_TMPL.format(
            pub=f"{today:%a}, {today.day} {today:%b} {today.year} 00:00:00 GMT"
        ),
    }


def make_redirects(port: int) -> dict[str, str]:
    return {
        "/redir-loop": f"http://127.0.0.1:{port}/news",
        "/redir-meta": "http://169.254.169.254/latest/meta-data/",
        "/redir-relative": "/rss-ok.xml",
    }


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, str] = {}
    redirects: dict[str, str] = {}
    hits: list[str] = []

    def do_GET(self):  # noqa: N802
        type(self).hits.append(self.path)
        target = self.redirects.get(self.path)
        if target:
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self.routes.get(self.path)
        if body is None:
            self.send_error(404)
            return
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # silence
        pass


class PipelineCase(unittest.TestCase):
    """Isolated project copy + fixture server (bound to 0.0.0.0 for 127.0.0.2)."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("0.0.0.0", 0), _Handler)
        cls.port = cls.server.server_address[1]
        _Handler.routes = make_routes(cls.port)
        _Handler.redirects = make_redirects(cls.port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.hits = []
        self._written: list[Path] = []

    def tearDown(self):
        for root in self._written:
            shutil.rmtree(root, ignore_errors=True)
        self._written = []

    # --- harness -------------------------------------------------------
    def base(self, path: str, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}{path}"

    def source(self, sid: str, **over) -> dict:
        src = {
            "id": sid,
            "label": sid.upper(),
            "type": "html",
            "adapter": "anthropic_html",
            "enabled": True,
        }
        src.update(over)
        return src

    def glm_source(self, path: str = "/glm-page") -> dict:
        return {
            "id": "glm",
            "label": "GLM / 智谱",
            "type": "docs",
            "adapter": "glm_docs",
            "list_url": self.base(path),
            "enabled": True,
        }

    def make_project(
        self,
        sources: list[dict],
        *,
        defaults: dict | None = None,
        stubs: bool = False,
        index_rows: int = 0,
    ) -> Path:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(dir=str(SCRATCH), prefix="proj_"))
        (root / "scripts").mkdir()
        for name in COPY:
            shutil.copy(ROOT / "scripts" / name, root / "scripts" / name)
        if stubs:
            for name in ("audit.py", "rebuild_index.py"):
                (root / "scripts" / name).write_text(STUB, encoding="utf-8")
        shutil.copy(_support.template("taxonomy.yaml"), root / "taxonomy.yaml")
        cfg = {
            "version": 1,
            "defaults": defaults or {
                "window_hours": 36,
                "max_items_per_source": 8,
                "backfill_items": 2,
            },
            "sources": sources,
        }
        (root / "sources.yaml").write_text(
            yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        (root / ".state").mkdir()
        (root / ".state" / "seen.json").write_text(
            json.dumps({"entries": {}}), encoding="utf-8"
        )
        (root / "library").mkdir()
        if index_rows > 0:
            self.write_index(root, [])
        self._written.append(root)
        return root

    def write_index(self, root: Path, rows: list[dict]) -> None:
        with open(root / "index.csv", "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=INDEX_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in INDEX_FIELDS})

    def init_sources(self, root: Path, sids: list[str]) -> None:
        path = root / ".state" / "sources.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"sources": {}}
        data.setdefault("sources", {})
        for sid in sids:
            data["sources"].setdefault(sid, {})["initialized"] = True
        path.write_text(json.dumps(data), encoding="utf-8")

    def mark_seen(self, root: Path, urls: list[str]) -> None:
        path = root / ".state" / "seen.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for url in urls:
            key = dedupe_key(url)
            data["entries"][key] = {"id": key, "path": "library/tech/x/entry.md", "first_seen": iso()}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def write_entry(self, root: Path, entry_id: str, url: str, published: str,
                    source: str = "anthropic", **extra) -> None:
        d = root / "library" / "tech" / entry_id
        d.mkdir(parents=True, exist_ok=True)
        fm = {
            "id": entry_id,
            "title": entry_id,
            "source": source,
            "published": published,
            "archived": published,
            "url": url,
            "canonical_url": url,
            "category": "tech",
            "status": "archived",
        }
        fm.update(extra)
        (d / "entry.md").write_text(
            "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n# x\n",
            encoding="utf-8",
        )

    def archive_entry(self, root: Path, url: str, published: str, source: str) -> None:
        self.write_entry(root, f"fixture-{abs(hash(url)) % 100000}", url, published, source)
        self.mark_seen(root, [url])

    # --- runners -------------------------------------------------------
    def run_fetch(self, root: Path, *args: str) -> dict:
        res = subprocess.run(
            [PY, str(root / "scripts" / "fetch_sources.py"), *args],
            capture_output=True, text=True, cwd=str(root), timeout=180,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def run_gate(self, root: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PY, str(root / "scripts" / "daily_gate.py"), *args],
            capture_output=True, text=True, cwd=str(root), timeout=180,
        )

    def run_digest(self, root: Path, run_file: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PY, str(root / "scripts" / "build_digest.py"), "--run", str(run_file), *args],
            capture_output=True, text=True, cwd=str(root), timeout=120,
        )

    def gate(self, root: Path) -> dict:
        path = root / ".state" / "runs" / "daily-gate.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def fetch_json(self, root: Path) -> dict:
        path = root / ".state" / "runs" / "latest-fetch.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def pending(self, root: Path) -> dict:
        path = root / ".state" / "pending.json"
        if not path.exists():
            return {"entries": {}}
        return json.loads(path.read_text(encoding="utf-8"))


class TestWatermarkWindow(PipelineCase):
    def test_watermarked_source_keeps_item_older_than_36h(self):
        """P1 #1: the archived water mark gates, the 36h window must not truncate."""
        root = self.make_project([self.source("anthropic", list_url=self.base("/news"))])
        self.init_sources(root, ["anthropic"])
        wm_dt = now() - timedelta(days=5)
        self.archive_entry(root, "https://www.anthropic.com/news/ancient", iso(wm_dt), "anthropic")

        out = self.run_fetch(root)
        src = out["sources"][0]
        self.assertEqual(src["mode"], "window:36h")
        self.assertEqual(src["watermark"], iso(wm_dt))

        urls = [c["url"] for c in out["candidates"]]
        self.assertIn("https://www.anthropic.com/news/recent", urls)
        # 48h old: outside window_floor, but newer than the water mark.
        self.assertIn("https://www.anthropic.com/news/delayed", urls)
        self.assertEqual(src["selected"], 2)

    def test_source_without_watermark_still_uses_36h_floor(self):
        """No archived water mark => the hard window still protects history."""
        root = self.make_project([self.source("anthropic", list_url=self.base("/news"))])
        self.init_sources(root, ["anthropic"])

        out = self.run_fetch(root)
        src = out["sources"][0]
        self.assertEqual(src["watermark"], "")
        urls = [c["url"] for c in out["candidates"]]
        self.assertIn("https://www.anthropic.com/news/recent", urls)
        self.assertNotIn("https://www.anthropic.com/news/delayed", urls)
        self.assertEqual(src["selected"], 1)

    def test_lookback_bound_still_applies(self):
        """A water mark older than 30 days must not sweep the whole archive in."""
        root = self.make_project([self.source("anthropic", list_url=self.base("/news-with-old"))])
        self.init_sources(root, ["anthropic"])
        old = now() - timedelta(days=45)
        self.archive_entry(root, "https://www.anthropic.com/news/stale", iso(old), "anthropic")

        out = self.run_fetch(root)
        urls = [c["url"] for c in out["candidates"]]
        self.assertIn("https://www.anthropic.com/news/recent", urls)
        self.assertIn("https://www.anthropic.com/news/delayed", urls)
        # 45 days old is beyond MAX_WATERMARK_LOOKBACK_DAYS.
        self.assertNotIn("https://www.anthropic.com/news/very-old", urls)


class TestPoisonedDates(PipelineCase):
    def _watermark(self, root: Path):
        sources = [{"id": "anthropic", "label": "Anthropic",
                    "list_url": "https://www.anthropic.com/news"}]
        original = fetch_mod.ROOT
        fetch_mod.ROOT = root
        try:
            return fetch_mod.load_archived_watermarks(sources)
        finally:
            fetch_mod.ROOT = original

    def test_unknown_and_future_dates_do_not_poison_watermark(self):
        root = self.make_project([self.source("anthropic", list_url=self.base("/news"))])
        self.init_sources(root, ["anthropic"])
        today = now()
        sane = today - timedelta(days=5)

        self.write_entry(root, "poison-future", "https://www.anthropic.com/news/future",
                         "2030-01-01T00:00:00+08:00", "anthropic")
        self.write_entry(root, "poison-unknown", "https://www.anthropic.com/news/unknown",
                         iso(today), "anthropic", published_unknown=True, date_source="fetched")
        self.write_entry(root, "poison-fetched", "https://www.anthropic.com/news/fetched-date",
                         iso(today), "anthropic", date_source="fetched")
        self.write_entry(root, "sane", "https://www.anthropic.com/news/ancient",
                         iso(sane), "anthropic")

        wm = self._watermark(root)
        self.assertIn("anthropic", wm)
        self.assertEqual(iso(wm["anthropic"]), iso(sane))

        out = self.run_fetch(root)
        self.assertEqual(out["sources"][0]["watermark"], iso(sane))
        urls = [c["url"] for c in out["candidates"]]
        self.assertIn("https://www.anthropic.com/news/delayed", urls)

    def test_card_text_and_url_future_dates_are_ignored(self):
        future = now() + timedelta(days=400)
        past = now() - timedelta(days=10)

        self.assertIsNone(fetch_mod.date_from_text(f"2030 路线图 {future:%Y-%m-%d}"))
        self.assertEqual(
            fetch_mod.date_from_text(f"发布于 {past:%Y/%m/%d}").strftime("%Y-%m-%d"),
            past.strftime("%Y-%m-%d"),
        )
        self.assertIsNone(fetch_mod.date_from_url(f"https://openai.com/index/{future:%Y/%m/%d}/post"))
        self.assertEqual(
            fetch_mod.date_from_url(f"https://openai.com/index/{past:%Y/%m/%d}/post")
            .strftime("%Y-%m-%d"),
            past.strftime("%Y-%m-%d"),
        )
        # An explicit ceiling is honoured too.
        self.assertIsNone(
            fetch_mod.date_from_text(f"{(now() - timedelta(days=3)):%Y-%m-%d}",
                                     max_dt=now() - timedelta(days=10))
        )


class TestRawSnapshots(PipelineCase):
    def test_dry_run_writes_no_raw_snapshot(self):
        root = self.make_project([self.glm_source()])
        out = self.run_fetch(root, "--backfill", "2", "--dry-run")
        self.assertEqual(len(out["candidates"]), 2)
        self.assertFalse((root / ".state" / "raw").exists())
        self.assertFalse((root / ".state" / "candidates").exists())

    def test_old_raw_snapshots_are_pruned(self):
        root = self.make_project([self.glm_source()])
        raw = root / ".state" / "raw"
        old_dir = raw / "2020-01-01"
        old_dir.mkdir(parents=True)
        (old_dir / "glm.html.gz").write_text("old", encoding="utf-8")
        today_dir = raw / now().strftime("%Y-%m-%d")
        today_dir.mkdir(parents=True)
        keep = today_dir / "notes.txt"
        keep.write_text("keep me", encoding="utf-8")

        self.run_fetch(root, "--backfill", "2")

        self.assertFalse(old_dir.exists())
        self.assertTrue((today_dir / "glm.html.gz").exists())
        self.assertTrue(keep.exists())
        self.assertNotIn(str(old_dir), str(list(raw.iterdir())))

    def test_snapshot_written_only_for_selected_sections(self):
        root = self.make_project([self.glm_source()])
        out = self.run_fetch(root, "--backfill", "1")
        self.assertEqual(len(out["candidates"]), 1)
        cand = out["candidates"][0]
        self.assertTrue(cand["raw_file"])
        self.assertTrue((root / cand["raw_file"]).exists())
        snapshots = list((root / ".state" / "raw").rglob("glm.html.gz"))
        self.assertEqual(len(snapshots), 1)
        # Private transport fields never leak into the JSON output.
        self.assertFalse(any(k.startswith("_") for k in cand))


class TestFetchTextSSRF(PipelineCase):
    def setUp(self):
        super().setUp()
        self._original_guard = fetch_mod.assert_public_url

    def tearDown(self):
        fetch_mod.assert_public_url = self._original_guard
        super().tearDown()

    def _stub_guard(self):
        """Treat 127.0.0.2 as public, block real loopback/metadata targets."""
        def guard(url: str) -> None:
            host = (urlsplit(url).hostname or "").lower()
            if host in {"127.0.0.1", "169.254.169.254", "10.0.0.5", "::1"}:
                raise ValueError(f"Prohibited IP address: {host}")
        fetch_mod.assert_public_url = guard

    def test_real_guard_blocks_loopback_and_metadata(self):
        for url in ("http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data/"):
            with self.assertRaises(ValueError):
                fetch_mod.assert_public_url(url)

    def test_public_source_redirected_to_loopback_is_blocked(self):
        self._stub_guard()
        with self.assertRaises(fetch_mod.FetchBlocked) as ctx:
            fetch_mod.fetch_text(self.base("/redir-loop", host="127.0.0.2"))
        self.assertIn("127.0.0.1", str(ctx.exception))
        self.assertNotIn("/news", _Handler.hits)

    def test_public_source_redirected_to_metadata_is_blocked(self):
        self._stub_guard()
        with self.assertRaises(fetch_mod.FetchBlocked) as ctx:
            fetch_mod.fetch_text(self.base("/redir-meta", host="127.0.0.2"))
        self.assertIn("169.254.169.254", str(ctx.exception))
        self.assertNotIn("/latest/meta-data/", _Handler.hits)

    def test_same_host_redirect_still_works(self):
        self._stub_guard()
        body = fetch_mod.fetch_text(self.base("/redir-relative", host="127.0.0.2"))
        self.assertIn("<rss", body)

    def test_loopback_fixture_is_allowed(self):
        body = fetch_mod.fetch_text(self.base("/news"))
        self.assertIn("Recent item", body)


class TestFetchTextDecoding(PipelineCase):
    class _Resp:
        def __init__(self, encoding):
            self.encoding = encoding

    def test_meta_charset_beats_latin1_default(self):
        raw = ('<html><head><meta charset="gb18030"></head>'
               '<body>中文测试内容</body></html>').encode("gb18030")
        text = fetch_mod._decode_body(raw, self._Resp("ISO-8859-1"))
        self.assertIn("中文测试内容", text)

    def test_utf8_body_without_hints(self):
        raw = "<html><body>中文正文</body></html>".encode("utf-8")
        text = fetch_mod._decode_body(raw, self._Resp(None))
        self.assertIn("中文正文", text)


class TestDailyGateClosure(PipelineCase):
    def test_finish_without_archive_fails_and_parks_candidates(self):
        """P1 #1: a selected-but-unarchived candidate must fail the run."""
        root = self.make_project([self.source("anthropic", list_url=self.base("/news"))],
                                 stubs=True, index_rows=1)
        res = self.run_gate(root, "start")
        self.assertEqual(res.returncode, 0, res.stderr)
        cands = [c["url"] for c in self.fetch_json(root)["candidates"]]
        self.assertEqual(len(cands), 2)

        res = self.run_gate(root, "finish")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("[CRON_FAILURE]", res.stderr)
        self.assertIn("unaccounted_candidates", res.stderr)

        gate = self.gate(root)
        self.assertEqual(gate["status"], "failed")
        self.assertEqual(len(gate["unaccounted_candidates"]), 2)

        pending = self.pending(root)
        self.assertEqual(len(pending["entries"]), 2)
        for rec in pending["entries"].values():
            self.assertEqual(rec["reason"], "unaccounted_at_finish")
            self.assertIn(rec["url"], cands)

    def test_fail_candidate_unblocks_finish_and_shows_in_digest(self):
        root = self.make_project([self.source("anthropic", list_url=self.base("/news"))],
                                 stubs=True, index_rows=1)
        res = self.run_gate(root, "start")
        self.assertEqual(res.returncode, 0, res.stderr)
        cands = [c["url"] for c in self.fetch_json(root)["candidates"]]
        self.assertEqual(len(cands), 2)

        for url in cands:
            res = self.run_gate(root, "fail-candidate", url, "--reason", "web_extract 502")
            self.assertEqual(res.returncode, 0, res.stderr)

        gate = self.gate(root)
        self.assertEqual(len(gate["failed_candidates"]), 2)
        self.assertEqual({rec["reason"] for rec in gate["failed_candidates"]}, {"web_extract 502"})
        self.assertEqual({rec["key"] for rec in gate["failed_candidates"]}, {dedupe_key(u) for u in cands})

        pending = self.pending(root)
        self.assertEqual(len(pending["entries"]), 2)
        self.assertEqual({rec["reason"] for rec in pending["entries"].values()}, {"archive_failed"})
        self.assertEqual({rec["url"] for rec in pending["entries"].values()}, set(cands))

        res = self.run_gate(root, "finish")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        out = res.stdout
        self.assertIn("本轮抓取失败候选 2 篇（已入待补队列）", out)
        self.assertIn("未归档候选（已入待补队列）：", out)
        self.assertIn("web_extract 502", out)
        for url in cands:
            self.assertIn(url, out)
        self.assertEqual(self.gate(root)["status"], "ok")

    def test_fail_candidate_requires_a_gate(self):
        root = self.make_project([self.source("anthropic", list_url=self.base("/news"))])
        res = self.run_gate(root, "fail-candidate", "https://www.anthropic.com/news/x",
                            "--reason", "boom")
        self.assertEqual(res.returncode, 1)
        self.assertIn("[CRON_FAILURE]", res.stderr)

    def test_finish_backward_compatible_without_candidates(self):
        root = self.make_project([self.source("anthropic", list_url=self.base("/news"))],
                                 stubs=True, index_rows=1)
        runs = root / ".state" / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        generated = iso()
        (runs / "latest-fetch.json").write_text(
            json.dumps({"generated_at": generated, "sources": []}), encoding="utf-8"
        )
        (runs / "daily-gate.json").write_text(
            json.dumps({
                "started_at": iso(),
                "fetch_generated_at": generated,
                "status": "started",
                "sources": [],
                "not_ok": [],
            }),
            encoding="utf-8",
        )
        res = self.run_gate(root, "finish")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.gate(root)["status"], "ok")

    def test_finish_rebuilds_index_before_real_audit_when_new_entry_archived(self):
        """Real (non-stubbed) rebuild_index + audit must pass when index.csv was stale before finish."""
        root = self.make_project([self.source("anthropic", list_url=self.base("/news"))],
                                 stubs=False, index_rows=0)
        for extra in ("audit.py", "rebuild_index.py", "reconcile_seen.py", "build_html.py", "media_markup.py", "archive_page.html"):
            shutil.copy(ROOT / "scripts" / extra, root / "scripts" / extra)
        # Start with empty index.csv (0 rows), then archive 1 valid entry before finish
        (root / "index.csv").write_text("id,path\n", encoding="utf-8-sig")
        entry_url = "https://www.anthropic.com/news/recent"
        entry_dir = root / "library" / "tech" / "2026-10-27-real-e2e"
        entry_dir.mkdir(parents=True, exist_ok=True)
        summary = "这是一段超过两百字的真实端到端测试中文摘要内容，用于验证索引重建必须在审计校验之前执行。" * 5
        from common import count_summary_chars, write_entry
        write_entry(entry_dir / "entry.md", {
            "id": "2026-10-27-real-e2e",
            "title": "端到端索引顺序测试",
            "original_title": "Real E2E",
            "title_status": "translated",
            "source": "Anthropic",
            "source_type": "vendor_blog",
            "published": iso(),
            "archived": iso(),
            "url": entry_url,
            "canonical_url": entry_url,
            "category": "tech",
            "context": "work",
            "tags": ["Anthropic"],
            "summary_chars": count_summary_chars(summary),
            "status": "archived",
            "content_note": "",
            "language": "en",
        }, f"# 端到端索引顺序测试\n\n## 摘要\n\n{summary}\n\n## 全文\n\n" + ("真实长正文段落内容。" * 100))
        (root / ".state" / "seen.json").write_text(json.dumps({
            "entries": {
                dedupe_key(entry_url): {
                    "id": "2026-10-27-real-e2e",
                    "path": "library/tech/2026-10-27-real-e2e/entry.md",
                    "first_seen": iso(),
                }
            }
        }, ensure_ascii=False), encoding="utf-8")
        runs = root / ".state" / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        gen = iso()
        (runs / "latest-fetch.json").write_text(json.dumps({
            "generated_at": gen,
            "sources": [{"id": "anthropic", "label": "Anthropic", "status": "ok", "selected": 1, "skipped": 0}],
            "candidates": [{"url": entry_url, "title": "Real E2E"}],
        }), encoding="utf-8")
        (runs / "daily-gate.json").write_text(json.dumps({
            "started_at": gen,
            "fetch_generated_at": gen,
            "status": "started",
            "sources": [],
            "not_ok": [],
        }), encoding="utf-8")

        res = self.run_gate(root, "finish")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("新增 1 篇", res.stdout)
        self.assertIn("端到端索引顺序测试", res.stdout)
        self.assertEqual(self.gate(root)["status"], "ok")


class TestDigestScope(PipelineCase):
    def test_concurrent_manual_archive_is_not_in_vendor_digest(self):
        root = self.make_project([self.source("anthropic", list_url=self.base("/news"))],
                                 index_rows=1)
        day = now().strftime("%Y-%m-%d")
        vendor = "https://www.anthropic.com/news/recent"
        manual = "https://manual.example/posts/my-bookmark"
        self.write_index(root, [
            {"id": "v1", "title": "Vendor piece", "source": "Anthropic",
             "published": f"{day}T09:00:00+08:00", "archived": f"{day}T12:00:00+08:00",
             "status": "archived", "url": vendor, "summary": "厂商摘要"},
            {"id": "m1", "title": "Manual bookmark", "source": "Manual",
             "published": f"{day}T08:00:00+08:00", "archived": f"{day}T12:30:00+08:00",
             "status": "archived", "url": manual, "summary": "手动收藏"},
        ])
        run = {
            "generated_at": f"{day}T11:00:00+08:00",
            "sources": [],
            "candidates": [{"url": vendor, "title": "Vendor piece"}],
        }
        run_file = root / "run.json"
        run_file.write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")

        res = self.run_digest(root, run_file)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("新增 1 篇", res.stdout)
        self.assertIn("Vendor piece", res.stdout)
        self.assertNotIn("Manual bookmark", res.stdout)

        # Without a "candidates" key the old pure-time behaviour is kept.
        run.pop("candidates")
        run_file.write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
        res = self.run_digest(root, run_file)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("新增 2 篇", res.stdout)
        self.assertIn("Manual bookmark", res.stdout)

    def test_timestamps_are_compared_as_datetimes(self):
        root = self.make_project([self.source("anthropic", list_url=self.base("/news"))],
                                 index_rows=1)
        day = now().strftime("%Y-%m-%d")
        self.write_index(root, [{
            "id": "e1", "title": "Cross-run article", "source": "Anthropic",
            "published": f"{day}T09:00:00+08:00",
            # Same instant as `since`, different textual form (no seconds).
            "archived": f"{day}T11:00+08:00",
            "status": "archived", "url": "https://www.anthropic.com/news/recent",
            "summary": "摘要",
        }])
        run_file = root / "run.json"
        run_file.write_text(json.dumps({"generated_at": f"{day}T11:00:00+08:00",
                                        "sources": []}), encoding="utf-8")
        res = self.run_digest(root, run_file)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("新增 1 篇", res.stdout)

    def test_gate_autodetected_next_to_run(self):
        root = self.make_project([self.source("anthropic", list_url=self.base("/news"))],
                                 index_rows=1)
        runs = root / ".state" / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        generated = iso()
        run_file = runs / "latest-fetch.json"
        run_file.write_text(json.dumps({
            "generated_at": generated,
            "sources": [{"id": "glm", "label": "GLM / 智谱", "status": "empty",
                         "empty_reason": "文档更新页未解析到任何 div.update section"}],
            "candidates": [{"url": "https://www.anthropic.com/news/recent"}],
        }), encoding="utf-8")
        gate_file = runs / "daily-gate.json"
        gate_file.write_text(json.dumps({
            "fetch_generated_at": generated,
            "status": "started",
            "failed_candidates": [
                {"url": "https://www.anthropic.com/news/recent", "reason": "web_extract 502"},
            ],
        }), encoding="utf-8")

        res = self.run_digest(root, run_file)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("未归档候选（已入待补队列）：", res.stdout)
        self.assertIn("web_extract 502", res.stdout)
        # Failed candidates are listed before the "未达源" section.
        self.assertIn("未达源（空/失败）", res.stdout)
        self.assertLess(
            res.stdout.index("未归档候选（已入待补队列）："),
            res.stdout.index("未达源（空/失败）"),
        )

        # A gate from a different fetch run is ignored.
        gate_file.write_text(json.dumps({
            "fetch_generated_at": "2000-01-01T00:00:00+08:00",
            "status": "started",
            "failed_candidates": [{"url": "x", "reason": "stale gate"}],
        }), encoding="utf-8")
        res = self.run_digest(root, run_file)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("stale gate", res.stdout)

    def test_wechat_repost_does_not_poison_vendor_watermark(self):
        root = self.make_project([self.source("openai", adapter="openai_rss",
                                              feed=self.base("/rss-recent.xml"), type="rss")])
        # A WeChat repost whose source starts with "OpenAI" must never set OpenAI's watermark
        self.write_entry(
            root, "2026-10-27-wx", "https://mp.weixin.qq.com/s/repost123",
            now().isoformat(timespec="seconds"), source="OpenAI 前沿编译", source_type="wechat",
        )
        orig_root = fetch_mod.ROOT
        fetch_mod.ROOT = root
        try:
            wms = fetch_mod.load_archived_watermarks([{
                "id": "openai", "label": "OpenAI", "feed": "https://openai.com/news/rss.xml",
            }])
            self.assertNotIn("openai", wms)
        finally:
            fetch_mod.ROOT = orig_root

    def test_candidate_cache_prunes_stale_non_glm_files(self):
        import os
        root = self.make_project([self.glm_source()])
        cache = root / ".state" / "candidates"
        cache.mkdir(parents=True, exist_ok=True)
        stale_openai = cache / "openai-01.md"
        fresh_openai = cache / "openai-02.md"
        stale_openai.write_text("old staged content", encoding="utf-8")
        fresh_openai.write_text("fresh staged content", encoding="utf-8")
        old_ts = (now() - timedelta(days=95)).timestamp()
        os.utime(stale_openai, (old_ts, old_ts))
        orig_root = fetch_mod.ROOT
        fetch_mod.ROOT = root
        try:
            removed = fetch_mod.cleanup_candidate_cache(set())
            self.assertIn("openai-01.md", removed)
            self.assertFalse(stale_openai.exists())
            self.assertTrue(fresh_openai.exists())
        finally:
            fetch_mod.ROOT = orig_root


if __name__ == "__main__":
    unittest.main()
