#!/usr/bin/env python3
"""Batch 2 regression tests: lookback coverage, pending retry/exit, lock-merge.

Everything runs inside a throwaway project under
the platform temporary directory and is served by a loopback fixture
server, so the production ``library/`` and ``.state/`` are never touched. The
fetcher is executed as a subprocess against the copy, which is the only way to
exercise the commit/lock path truthfully (two overlapping processes included).

Rules under test (see docs/plans/2026-10-29-repair-batches.md, batch 2):

* a source with an archived water mark sweeps *every* unseen candidate inside
  the 30-day lookback, including items published at or before the water mark
  (same-timestamp late publication, back-dated announcement);
* day-precision dates are compared at the start of their day for lower window
  bounds (36h / 30d) and at the end of their day when compared with the water
  mark, so the window cannot silently stretch by a day while a same-day
  archive never starves a new item;
* pending records survive rolling out of the upstream feed (stored
  url/title/date/source/extractor are reused), expire 30 days after the first
  enqueue (``queued_at``, legacy ``first_seen``/``added_at``), never have that
  clock restarted by a re-park, and every exit is reported in ``pending_exits``
  with a reason (``expired`` / ``seen`` / ``over_limit``) plus a
  ``pending_expired`` count;
* a failed source keeps its pending queue untouched; pending wins over fresh
  items subject to the per-source (8) and global (10) caps;
* every selected item is persisted as ``awaiting_archive`` so a dying agent
  cannot lose it; the next run drops it once it is seen;
* pending/sources are re-read and merged under ``.state/.lock`` at commit
  time (network outside the lock), so a concurrent run's additions, other
  sources' state and a newer ``last_run`` are never clobbered, and a record
  updated by another process is not deleted;
* ``--dry-run`` reports the same numbers without writing anything.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_sources as fetch_mod  # noqa: E402
from common import dedupe_key, iso, now  # noqa: E402

PY = _support.PYTHON
SCRATCH = _support.scratch_dir("reliability-")
COPY = ("common.py", "fetch_sources.py")

ANTH = "https://www.anthropic.com"
OPENAI = "https://openai.com"


def _en_date(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def _rfc822(d) -> str:
    return f"{d:%a}, {d.day} {d:%b} {d.year} 00:00:00 GMT"


def html_list(items: list[tuple[str, str, str]]) -> str:
    """Anthropic-style list page: (href, title, english date)."""
    rows = "".join(
        f'<li><a class="title-link" href="{href}">{title}</a>'
        f"<time>{date}</time></li>"
        for href, title, date in items
    )
    return f"<html><body><ul>{rows}</ul></body></html>"


def rss_feed(items: list[tuple[str, str, str]]) -> str:
    """RSS feed: (link, title, rfc822 date)."""
    rows = "".join(
        f"<item><title>{title}</title><link>{link}</link>"
        f"<pubDate>{date}</pubDate><description>{title}</description></item>"
        for link, title, date in items
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<rss version=\"2.0\"><channel><title>fixture</title>{rows}</channel></rss>"
    )


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, bytes]] = {}
    holds: dict[str, str] = {}  # path -> release marker file
    hits: list[str] = []

    def _serve(self, status: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        cls = type(self)
        cls.hits.append(self.path)
        release = cls.holds.get(self.path)
        if release:
            Path(release + ".arrived").write_text("1", encoding="utf-8")
            deadline = time.time() + 30
            while time.time() < deadline and not Path(release).exists():
                time.sleep(0.05)
        status, body = cls.routes.get(self.path, (404, b"not found"))
        self._serve(status, body)

    def log_message(self, *args):  # silence
        pass


class ReliabilityCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.routes = {}
        _Handler.holds = {}
        _Handler.hits = []
        self._written: list[Path] = []

    def tearDown(self):
        for root in self._written:
            shutil.rmtree(root, ignore_errors=True)
        self._written = []

    # --- harness -------------------------------------------------------
    def base(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def route(self, path: str, body: str, status: int = 200, ctype: str = "text/html; charset=utf-8"):
        _Handler.routes[path] = (status, body.encode("utf-8"))
        return self.base(path)

    def hold(self, path: str) -> Path:
        marker = SCRATCH / f"hold-{os.getpid()}-{len(self._written)}-{path.strip('/')}"
        marker.parent.mkdir(parents=True, exist_ok=True)
        _Handler.holds[path] = str(marker)
        if marker.exists():
            marker.unlink()
        return marker

    def html_source(self, sid: str = "anthropic", path: str = "/news", **over) -> dict:
        src = {
            "id": sid,
            "label": sid,
            "type": "html",
            "adapter": "anthropic_html",
            "list_url": self.base(path),
            "extractor": "direct",
            "enabled": True,
        }
        src.update(over)
        return src

    def rss_source(self, sid: str = "openai", path: str = "/feed.xml", **over) -> dict:
        src = {
            "id": sid,
            "label": sid,
            "type": "rss",
            "adapter": "openai_rss",
            "feed": self.base(path),
            "extractor": "web_extract",
            "enabled": True,
        }
        src.update(over)
        return src

    def make_project(self, sources: list[dict], *, defaults: dict | None = None) -> Path:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(dir=str(SCRATCH), prefix="proj_"))
        (root / "scripts").mkdir()
        for name in COPY:
            shutil.copy(ROOT / "scripts" / name, root / "scripts" / name)
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
        (root / ".state" / "seen.json").write_text(json.dumps({"entries": {}}), encoding="utf-8")
        (root / "library").mkdir()
        self._written.append(root)
        return root

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
            data["entries"][key] = {
                "id": key,
                "path": "library/tech/x/entry.md",
                "first_seen": iso(),
            }
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

    def archive(self, root: Path, url: str, published: str, source: str = "anthropic") -> None:
        self.write_entry(root, f"wm-{abs(hash(url)) % 100000}", url, published, source)
        self.mark_seen(root, [url])

    def write_pending(self, root: Path, entries: dict[str, dict]) -> None:
        (root / ".state" / "pending.json").write_text(
            json.dumps({"updated_at": iso(), "entries": entries}, ensure_ascii=False),
            encoding="utf-8",
        )

    def pending_record(self, url: str, sid: str = "anthropic", **over) -> dict:
        rec = {
            "url": url,
            "title": f"pending {url.rsplit('/', 1)[-1]}",
            "published": iso(now() - timedelta(days=3)),
            "source": sid,
            "source_id": sid,
            "source_type": "vendor_blog",
            "summary": "stored summary",
            "date_source": "list",
            "date_precision": "day",
            "published_unknown": False,
            "extractor": "direct",
            "language": "en",
            "reason": "awaiting_archive",
            "dropped_at": iso(now() - timedelta(days=1)),
            "queued_at": iso(now() - timedelta(days=1)),
            "dedupe_keys": [],
        }
        rec.update(over)
        return rec

    def pending(self, root: Path) -> dict:
        path = root / ".state" / "pending.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"entries": {}}

    def sources_state(self, root: Path) -> dict:
        path = root / ".state" / "sources.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"sources": {}}

    def state_snapshot(self, root: Path) -> dict[str, str]:
        snap: dict[str, str] = {}
        for path in sorted((root / ".state").rglob("*")):
            if path.is_file():
                snap[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        return snap

    def fetch_cmd(self, root: Path, *args: str) -> list[str]:
        return [PY, str(root / "scripts" / "fetch_sources.py"), *args]

    def run_fetch(self, root: Path, *args: str) -> dict:
        res = subprocess.run(
            self.fetch_cmd(root, *args), capture_output=True, text=True,
            cwd=str(root), timeout=180,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def urls(self, out: dict) -> list[str]:
        return [c["url"] for c in out["candidates"]]


class TestLookbackCoverage(ReliabilityCase):
    """Water mark gate must not hide same-timestamp or back-dated items."""

    def test_same_timestamp_and_backdated_items_are_selected(self):
        wm = (now() - timedelta(days=2)).replace(microsecond=0)
        same = wm
        older = wm - timedelta(hours=6)
        self.route("/feed.xml", rss_feed([
            (f"{OPENAI}/index/same-instant", "Same instant", _rfc822(same)),
            (f"{OPENAI}/index/backdated", "Backdated", _rfc822(older)),
        ]))
        root = self.make_project([self.rss_source("openai")])
        self.init_sources(root, ["openai"])
        self.archive(root, f"{OPENAI}/index/already-archived", iso(wm), "openai")

        out = self.run_fetch(root)
        src = out["sources"][0]
        self.assertEqual(src["watermark"], iso(wm))
        urls = self.urls(out)
        # Previously filtered by `effective_dt <= watermark`.
        self.assertIn(f"{OPENAI}/index/same-instant", urls)
        self.assertIn(f"{OPENAI}/index/backdated", urls)
        self.assertEqual(src["selected"], 2)

    def test_watermarked_source_still_keeps_item_older_than_36h(self):
        wm = now() - timedelta(days=5)
        today = now().date()
        self.route("/news", html_list([
            ("/news/recent", "Recent", _en_date(today)),
            ("/news/delayed", "Delayed", _en_date(today - timedelta(days=2))),
        ]))
        root = self.make_project([self.html_source()])
        self.init_sources(root, ["anthropic"])
        self.archive(root, f"{ANTH}/news/ancient", iso(wm), "anthropic")

        out = self.run_fetch(root)
        urls = self.urls(out)
        self.assertIn(f"{ANTH}/news/recent", urls)
        self.assertIn(f"{ANTH}/news/delayed", urls)

    def test_lookback_boundary_and_coverage_skipped(self):
        today = now().date()
        self.route("/news", html_list([
            ("/news/today", "Today", _en_date(today)),
            ("/news/d29", "Day 29", _en_date(today - timedelta(days=29))),
            ("/news/d30", "Day 30", _en_date(today - timedelta(days=30))),
            ("/news/d31", "Day 31", _en_date(today - timedelta(days=31))),
            ("/news/d400", "Ancient", _en_date(today - timedelta(days=400))),
        ]))
        root = self.make_project([self.html_source()])
        self.init_sources(root, ["anthropic"])
        self.archive(root, f"{ANTH}/news/archived", iso(now() - timedelta(days=10)), "anthropic")

        out = self.run_fetch(root)
        urls = self.urls(out)
        self.assertIn(f"{ANTH}/news/today", urls)
        self.assertIn(f"{ANTH}/news/d29", urls)
        # Day precision is compared in whole calendar days: day 30 is the
        # inclusive boundary, day 31 is out.
        self.assertIn(f"{ANTH}/news/d30", urls)
        self.assertNotIn(f"{ANTH}/news/d31", urls)
        self.assertNotIn(f"{ANTH}/news/d400", urls)
        # Out-of-coverage items are counted, never parked.
        self.assertEqual(out["coverage_skipped"], 2)
        self.assertEqual(out["sources"][0]["coverage_skipped"], 2)
        self.assertEqual(out["pending_total"], len(self.urls(out)))
        self.assertEqual({r["reason"] for r in self.pending(root)["entries"].values()},
                         {"awaiting_archive"})

    def test_no_watermark_uses_calendar_day_36h_floor(self):
        """Day precision is compared at day start, so 36h never becomes ~60h."""
        today = now().date()
        self.route("/news", html_list([
            ("/news/recent", "Recent", _en_date(today)),
            ("/news/delayed", "Delayed", _en_date(today - timedelta(days=2))),
        ]))
        root = self.make_project([self.html_source()])
        self.init_sources(root, ["anthropic"])

        out = self.run_fetch(root)
        src = out["sources"][0]
        self.assertEqual(src["watermark"], "")
        urls = self.urls(out)
        self.assertIn(f"{ANTH}/news/recent", urls)
        self.assertNotIn(f"{ANTH}/news/delayed", urls)
        self.assertEqual(src["selected"], 1)
        self.assertEqual(out["coverage_skipped"], 1)

    def test_first_run_backfill_keeps_newest_n(self):
        today = now().date()
        self.route("/news", html_list([
            (f"/news/i{n}", f"Item {n}", _en_date(today - timedelta(days=n)))
            for n in range(6)
        ]))
        root = self.make_project([self.html_source()])
        out = self.run_fetch(root)
        self.assertEqual(out["sources"][0]["mode"], "backfill:2")
        self.assertEqual(len(self.urls(out)), 2)
        self.assertIn(f"{ANTH}/news/i0", self.urls(out))
        self.assertIn(f"{ANTH}/news/i1", self.urls(out))


class TestPendingRetry(ReliabilityCase):
    def _ok_feed(self, today=None) -> None:
        """A non-empty list page: an empty page is a source failure, not 'ok'."""
        today = today or now().date()
        self.route("/news", html_list([("/news/current", "Current", _en_date(today))]))

    def test_pending_survives_rolling_out_of_feed(self):
        today = now().date()
        self.route("/news", html_list([("/news/recent", "Recent", _en_date(today))]))
        gone = f"{ANTH}/news/rolled-out"
        root = self.make_project([self.html_source()])
        self.init_sources(root, ["anthropic"])
        self.write_pending(root, {dedupe_key(gone): self.pending_record(gone)})

        out = self.run_fetch(root, "--max", "10")
        urls = self.urls(out)
        self.assertIn(gone, urls)
        self.assertIn(f"{ANTH}/news/recent", urls)
        rec = [c for c in out["candidates"] if c["url"] == gone][0]
        self.assertTrue(rec["from_pending"])
        # Stored metadata is what makes the retry possible.
        self.assertEqual(rec["title"], "pending rolled-out")
        self.assertEqual(rec["source"], "anthropic")
        self.assertEqual(rec["extractor"], "direct")
        # Still pending (it was not archived), with the original queue time.
        pend = self.pending(root)["entries"]
        self.assertIn(dedupe_key(gone), pend)
        self.assertTrue(pend[dedupe_key(gone)]["queued_at"])

    def test_pending_ttl_uses_queue_time_not_article_date(self):
        old_article = iso(now() - timedelta(days=400))
        url = f"{ANTH}/news/old-but-queued"
        root = self.make_project([self.html_source()])
        self.init_sources(root, ["anthropic"])
        rec = self.pending_record(url, published=old_article, date_precision="day",
                                  queued_at=iso(now() - timedelta(days=2)))
        self.write_pending(root, {dedupe_key(url): rec})
        self._ok_feed()

        out = self.run_fetch(root)
        self.assertIn(url, self.urls(out))
        self.assertEqual(out["pending_expired"], 0)
        self.assertEqual(out["pending_exits"], [])

    def test_pending_expiry_is_reported_and_removed(self):
        expired = f"{ANTH}/news/expired"
        kept = f"{ANTH}/news/kept"
        root = self.make_project([self.html_source()])
        self.init_sources(root, ["anthropic"])
        self.write_pending(root, {
            dedupe_key(expired): self.pending_record(
                expired, queued_at=iso(now() - timedelta(days=31))),
            dedupe_key(kept): self.pending_record(
                kept, queued_at=iso(now() - timedelta(days=29))),
        })
        self._ok_feed()

        out = self.run_fetch(root)
        urls = self.urls(out)
        self.assertNotIn(expired, urls)
        self.assertIn(kept, urls)
        self.assertEqual(out["pending_expired"], 1)
        self.assertEqual(out["sources"][0]["pending_expired"], 1)
        exits = out["pending_exits"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["url"], expired)
        self.assertEqual(exits[0]["source_id"], "anthropic")
        self.assertEqual(exits[0]["reason"], "expired")
        self.assertNotIn(dedupe_key(expired), self.pending(root)["entries"])
        self.assertIn(dedupe_key(kept), self.pending(root)["entries"])

    def test_legacy_pending_uses_first_seen_or_added_at(self):
        via_first = f"{ANTH}/news/legacy-first"
        via_added = f"{ANTH}/news/legacy-added"
        fresh = f"{ANTH}/news/legacy-fresh"
        root = self.make_project([self.html_source()])
        self.init_sources(root, ["anthropic"])
        expired_rec = self.pending_record(via_first)
        expired_rec.pop("queued_at")
        expired_rec["first_seen"] = iso(now() - timedelta(days=45))
        added_rec = self.pending_record(via_added)
        added_rec.pop("queued_at")
        added_rec["added_at"] = iso(now() - timedelta(days=40))
        fresh_rec = self.pending_record(fresh)
        fresh_rec.pop("queued_at")
        fresh_rec["first_seen"] = iso(now() - timedelta(days=3))
        self.write_pending(root, {
            dedupe_key(via_first): expired_rec,
            dedupe_key(via_added): added_rec,
            dedupe_key(fresh): fresh_rec,
        })
        self._ok_feed()

        out = self.run_fetch(root)
        self.assertEqual(out["pending_expired"], 2)
        self.assertEqual({e["url"] for e in out["pending_exits"]}, {via_first, via_added})
        self.assertIn(fresh, self.urls(out))

    def test_repark_does_not_extend_queue_ttl(self):
        url = f"{ANTH}/news/repark"
        queued = iso(now() - timedelta(days=20))
        root = self.make_project([self.html_source()])
        self.init_sources(root, ["anthropic"])
        self.write_pending(root, {
            dedupe_key(url): self.pending_record(url, queued_at=queued,
                                                 dropped_at=iso(now() - timedelta(days=20))),
        })
        today = now().date()
        self.route("/news", html_list([(f"/news/repark", "Repark", _en_date(today))]))

        self.run_fetch(root)
        self.assertEqual(self.pending(root)["entries"][dedupe_key(url)]["queued_at"], queued)

    def test_source_failure_keeps_pending_untouched(self):
        url = f"{ANTH}/news/held"
        stale = iso(now() - timedelta(days=45))
        root = self.make_project([self.html_source(path="/broken")])
        self.init_sources(root, ["anthropic"])
        self.write_pending(root, {
            dedupe_key(url): self.pending_record(url, queued_at=stale),
        })
        self.route("/broken", "boom", status=500)
        before = self.pending(root)["entries"]

        out = self.run_fetch(root)
        src = out["sources"][0]
        self.assertEqual(src["status"], "error")
        self.assertEqual(self.urls(out), [])
        self.assertEqual(out["pending_exits"], [])
        self.assertEqual(out["pending_total"], 1)
        self.assertEqual(self.pending(root)["entries"], before)

    def test_seen_pending_is_cleaned(self):
        seen_url = f"{ANTH}/news/now-seen"
        root = self.make_project([self.html_source()])
        self.init_sources(root, ["anthropic"])
        self.write_pending(root, {dedupe_key(seen_url): self.pending_record(seen_url)})
        self.mark_seen(root, [seen_url])
        self._ok_feed()

        out = self.run_fetch(root)
        exits = out["pending_exits"]
        self.assertEqual([e["reason"] for e in exits], ["seen"])
        self.assertEqual(exits[0]["url"], seen_url)
        self.assertNotIn(dedupe_key(seen_url), self.pending(root)["entries"])
        self.assertEqual(out["pending_total"], len(self.pending(root)["entries"]))

    def test_pending_wins_and_quota_is_respected(self):
        today = now().date()
        pending_urls = [f"{ANTH}/news/p{n}" for n in (1, 2)]
        fresh = [(f"/news/f{n}", f"Fresh {n}", _en_date(today)) for n in range(9)]
        self.route("/news", html_list(fresh))
        root = self.make_project([self.html_source()])
        self.init_sources(root, ["anthropic"])
        self.write_pending(root, {
            dedupe_key(u): self.pending_record(u, queued_at=iso(now() - timedelta(days=1)))
            for u in pending_urls
        })

        out = self.run_fetch(root)
        src = out["sources"][0]
        urls = self.urls(out)
        self.assertEqual(len(urls), 8)  # per-source cap
        self.assertEqual(src["selected"], 8)
        self.assertTrue(set(pending_urls) <= set(urls))
        self.assertEqual(src["pending_selected"], 2)
        self.assertEqual(src["dropped_by_max"], 3)
        parked = self.pending(root)["entries"]
        reasons = [rec["reason"] for rec in parked.values()]
        self.assertEqual(reasons.count("per_source_cap"), 3)

    def test_global_max_keeps_pending_first(self):
        today = now().date()
        self.route("/news-a", html_list(
            [(f"/news/a{n}", f"A {n}", _en_date(today)) for n in range(6)]))
        self.route("/news-b", html_list(
            [(f"/news/b{n}", f"B {n}", _en_date(today)) for n in range(6)]))
        root = self.make_project([
            self.html_source("a", path="/news-a"),
            self.html_source("b", path="/news-b"),
        ], defaults={"window_hours": 36, "max_items_per_source": 8, "backfill_items": 2})
        self.init_sources(root, ["a", "b"])
        pending_a = f"{ANTH}/news/a-pending"
        self.write_pending(root, {
            dedupe_key(pending_a): self.pending_record(
                pending_a, sid="a", queued_at=iso(now() - timedelta(days=1))),
        })

        out = self.run_fetch(root, "--max", "10")
        urls = self.urls(out)
        self.assertEqual(len(urls), 10)
        self.assertIn(pending_a, urls)
        self.assertEqual(out["global_dropped"], 3)

    def test_selected_items_are_parked_until_seen(self):
        today = now().date()
        self.route("/news", html_list([
            ("/news/one", "One", _en_date(today)),
            ("/news/two", "Two", _en_date(today)),
        ]))
        root = self.make_project([self.html_source()])
        self.init_sources(root, ["anthropic"])

        out = self.run_fetch(root)
        selected = self.urls(out)
        parked = self.pending(root)["entries"]
        self.assertEqual(set(parked), {dedupe_key(u) for u in selected})
        for rec in parked.values():
            self.assertEqual(rec["reason"], "awaiting_archive")
            self.assertTrue(rec["queued_at"])

        self.mark_seen(root, selected)
        out = self.run_fetch(root)
        self.assertEqual(out["pending_total"], 0)
        self.assertEqual({e["reason"] for e in out["pending_exits"]}, {"seen"})
        self.assertEqual(self.pending(root)["entries"], {})


class TestDryRun(ReliabilityCase):
    def test_dry_run_reports_truthfully_and_writes_nothing(self):
        today = now().date()
        self.route("/news", html_list([
            ("/news/recent", "Recent", _en_date(today)),
            ("/news/d400", "Ancient", _en_date(today - timedelta(days=400))),
        ]))
        expired = f"{ANTH}/news/expired"
        root = self.make_project([self.html_source()])
        self.init_sources(root, ["anthropic"])
        self.write_pending(root, {
            dedupe_key(expired): self.pending_record(
                expired, queued_at=iso(now() - timedelta(days=45))),
        })
        self.init_sources(root, ["anthropic"])
        before = self.state_snapshot(root)

        dry = self.run_fetch(root, "--dry-run")
        self.assertEqual(self.state_snapshot(root), before)
        self.assertFalse((root / ".state" / "runs").exists())
        self.assertFalse((root / ".state" / "candidates").exists())
        self.assertFalse((root / ".state" / "raw").exists())
        self.assertEqual(dry["coverage_skipped"], 1)
        self.assertEqual(dry["pending_expired"], 1)
        self.assertEqual([e["url"] for e in dry["pending_exits"]], [expired])

        real = self.run_fetch(root)
        for key in ("coverage_skipped", "pending_expired", "pending_total",
                    "global_dropped", "skipped_total"):
            self.assertEqual(dry[key], real[key], key)
        self.assertEqual([e["url"] for e in dry["pending_exits"]],
                         [e["url"] for e in real["pending_exits"]])

    def test_source_subset_keeps_other_pending(self):
        today = now().date()
        self.route("/news-a", html_list([("/news/a0", "A0", _en_date(today))]))
        self.route("/news-b", html_list([("/news/b0", "B0", _en_date(today))]))
        other = f"{ANTH}/news/b-pending"
        root = self.make_project([
            self.html_source("a", path="/news-a"),
            self.html_source("b", path="/news-b"),
        ])
        self.init_sources(root, ["a", "b"])
        self.write_pending(root, {
            dedupe_key(other): self.pending_record(other, sid="b",
                                                   queued_at=iso(now() - timedelta(days=2))),
        })

        self.run_fetch(root, "--source", "a")
        self.assertIn(dedupe_key(other), self.pending(root)["entries"])
        # The other source's state is untouched as well (still just initialized).
        state = self.sources_state(root)["sources"]
        self.assertEqual(state["b"], {"initialized": True})
        self.assertNotIn("last_run", state["b"])
        self.assertTrue(state["a"]["initialized"])


class TestPendingMergeUnit(unittest.TestCase):
    """Pure merge helpers (commit-time, lock-held) in isolation."""

    def rec(self, url: str, **over) -> dict:
        base = {
            "url": url,
            "source_id": "anthropic",
            "reason": "awaiting_archive",
            "queued_at": iso(now() - timedelta(days=1)),
            "dropped_at": iso(now() - timedelta(days=1)),
            "updated_at": "2026-10-01T00:00:00+08:00",
        }
        base.update(over)
        return base

    def test_keeps_other_process_additions_and_earliest_queued_at(self):
        mine = "https://x/1"
        theirs = "https://x/2"
        old = iso(now() - timedelta(days=10))
        disk = {dedupe_key(theirs): self.rec(theirs),
                dedupe_key(mine): self.rec(mine, queued_at=old, updated_at="2026-10-01T00:00:00+08:00")}
        merged, removals, evicted = fetch_mod.merge_pending_entries(
            disk_entries=disk,
            baseline_entries={dedupe_key(mine): self.rec(mine, queued_at=old,
                                                       updated_at="2026-10-01T00:00:00+08:00")},
            upserts={dedupe_key(mine): self.rec(mine, queued_at=iso(), reason="per_source_cap")},
            remove_keys=set(),
        )
        self.assertEqual(set(merged), {dedupe_key(mine), dedupe_key(theirs)})
        self.assertEqual(merged[dedupe_key(mine)]["queued_at"], old)
        self.assertEqual(merged[dedupe_key(mine)]["reason"], "per_source_cap")
        self.assertEqual(removals, set())
        self.assertEqual(evicted, [])

    def test_only_deletes_records_untouched_by_other_processes(self):
        stale = "https://x/stale"
        touched = "https://x/touched"
        base = {
            dedupe_key(stale): self.rec(stale, updated_at="2026-10-01T00:00:00+08:00"),
            dedupe_key(touched): self.rec(touched, updated_at="2026-10-01T00:00:00+08:00"),
        }
        disk = {
            dedupe_key(stale): dict(base[dedupe_key(stale)]),
            dedupe_key(touched): self.rec(touched, reason="archive_failed",
                                          updated_at="2026-10-29T09:00:00+08:00"),
        }
        merged, removals, _ = fetch_mod.merge_pending_entries(
            disk_entries=disk, baseline_entries=base, upserts={},
            remove_keys=set(base),
        )
        self.assertEqual(removals, {dedupe_key(stale)})
        self.assertNotIn(dedupe_key(stale), merged)
        self.assertIn(dedupe_key(touched), merged)

    def test_limit_evictions_are_reported(self):
        disk = {f"k{n}": self.rec(f"https://x/{n}", queued_at=iso(now() - timedelta(days=n + 1)))
                for n in range(300)}
        merged, _, evicted = fetch_mod.merge_pending_entries(
            disk_entries=disk, baseline_entries=dict(disk), upserts={}, remove_keys=set(),
        )
        self.assertEqual(len(merged), 300)
        self.assertEqual(evicted, [])

        merged, _, evicted = fetch_mod.merge_pending_entries(
            disk_entries=dict(disk), baseline_entries=dict(disk),
            upserts={"new": self.rec("https://x/new", queued_at=iso(now()))},
            remove_keys=set(),
        )
        self.assertEqual(len(merged), 300)
        self.assertEqual(len(evicted), 1)
        self.assertEqual(evicted[0][2], "over_limit")

    def test_source_merge_applies_streak_from_disk(self):
        fetched_at = now()
        prev = {
            "initialized": True,
            "last_run": iso(fetched_at - timedelta(hours=2)),
            "failures": 2,
        }
        delta = {"status": "error", "mode": "window:36h", "error": "boom",
                 "watermark": "", "observed_newest": ""}
        merged = fetch_mod.merge_source_record(prev, delta, fetched_at)
        self.assertEqual(merged["failures"], 3)
        self.assertEqual(merged["last_status"], "error")
        self.assertTrue(merged["needs_attention"])
        self.assertTrue(merged["initialized"])

    def test_source_merge_does_not_override_newer_run(self):
        fetched_at = now()
        prev = {
            "initialized": True,
            "last_run": iso(fetched_at + timedelta(minutes=5)),
            "last_status": "ok",
            "failures": 0,
            "observed_newest": "newer",
        }
        merged = fetch_mod.merge_source_record(
            prev, {"status": "error", "mode": "window:36h", "error": "old"}, fetched_at)
        self.assertEqual(merged["last_status"], "ok")
        self.assertEqual(merged["failures"], 0)
        self.assertEqual(merged["observed_newest"], "newer")

    def test_window_bounds_use_day_start_watermark_uses_day_end(self):
        item = {"published": "2026-10-27T00:00:00+08:00", "date_precision": "day"}
        self.assertEqual(fetch_mod.window_dt(item).day, 27)
        self.assertEqual(fetch_mod.window_dt(item).hour, 0)
        self.assertEqual(fetch_mod.effective_dt(item).hour, 23)
        precise = {"published": "2026-10-27T13:05:00+08:00", "date_precision": "datetime"}
        self.assertEqual(fetch_mod.window_dt(precise), fetch_mod.effective_dt(precise))


class TestMergeIntegration(ReliabilityCase):
    """The lock-held merge as the subprocess actually performs it."""

    def test_failure_streak_is_applied_on_top_of_disk(self):
        self.route("/broken", "boom", status=500)
        root = self.make_project([self.html_source(path="/broken")])
        (root / ".state" / "sources.json").write_text(json.dumps({
            "sources": {
                "anthropic": {
                    "initialized": True,
                    "last_run": iso(now() - timedelta(hours=3)),
                    "last_status": "empty",
                    "failures": 2,
                    "consecutive_failures": 2,
                }
            }
        }), encoding="utf-8")

        out = self.run_fetch(root)
        rec = self.sources_state(root)["sources"]["anthropic"]
        self.assertEqual(rec["failures"], 3)          # 2 on disk + this failure
        self.assertEqual(rec["last_status"], "error")
        self.assertTrue(rec["needs_attention"])       # third strike
        self.assertEqual(out["sources"][0]["streak"], 3)
        self.assertTrue(out["sources"][0]["needs_attention"])

    def test_queue_bound_evicts_and_reports_over_limit(self):
        url = "https://www.anthropic.com/news/base"
        entries = {}
        for n in range(305):
            u = f"https://www.anthropic.com/news/q{n}"
            entries[dedupe_key(u)] = self.pending_record(
                u, queued_at=iso(now() - timedelta(days=1, minutes=n)))
        self.route("/news", html_list([("/news/base", "Base", _en_date(now().date()))]))
        root = self.make_project([self.html_source()])
        self.init_sources(root, ["anthropic"])
        self.write_pending(root, entries)

        out = self.run_fetch(root)
        self.assertEqual(out["pending_total"], 300)
        self.assertEqual(len(self.pending(root)["entries"]), 300)
        exits = out["pending_exits"]
        over = [e for e in exits if e["reason"] == "over_limit"]
        self.assertEqual(len(over), len(exits))
        self.assertGreaterEqual(len(over), 1)
        self.assertTrue(all(e["url"] and e["source_id"] == "anthropic" for e in over))
        # Pending wins: the 8 oldest-queued records are selected, the fresh item
        # is parked instead (it is not lost, just waiting behind the queue).
        self.assertEqual([c["url"] for c in out["candidates"]],
                         [f"https://www.anthropic.com/news/q{n}" for n in range(8)])
        parked = self.pending(root)["entries"]
        self.assertEqual(parked[dedupe_key(url)]["reason"], "per_source_cap")


class TestConcurrentRuns(ReliabilityCase):
    def test_overlapping_runs_merge_pending_and_source_state(self):
        today = now().date()
        gate = self.hold("/slow-a")
        self.route("/slow-a", html_list(
            [(f"/news/a{n}", f"A {n}", _en_date(today)) for n in range(4)]))
        self.route("/news-b", html_list(
            [(f"/news/b{n}", f"B {n}", _en_date(today)) for n in range(4)]))
        root = self.make_project([
            self.html_source("a", path="/slow-a"),
            self.html_source("b", path="/news-b"),
        ], defaults={"window_hours": 36, "max_items_per_source": 1, "backfill_items": 1})
        # Both sources are already initialized, so they run in window mode (a
        # backfill would deliberately not park the quota leftovers).
        self.init_sources(root, ["a", "b"])

        proc_a = subprocess.Popen(
            self.fetch_cmd(root, "--source", "a"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(root),
        )
        try:
            deadline = time.time() + 20
            arrived = Path(str(gate) + ".arrived")
            while time.time() < deadline and not arrived.exists():
                self.assertIsNone(proc_a.poll(), "process A exited before the overlap")
                time.sleep(0.05)
            self.assertTrue(arrived.exists(), "process A never reached the slow source")

            out_b = self.run_fetch(root, "--source", "b")
            self.assertEqual(out_b["sources"][0]["id"], "b")
            self.assertEqual(out_b["pending_total"], 4)  # B sees only its own batch

            gate.write_text("go", encoding="utf-8")
            stdout_a, stderr_a = proc_a.communicate(timeout=120)
        finally:
            if proc_a.poll() is None:
                proc_a.kill()
                proc_a.communicate()
        self.assertEqual(proc_a.returncode, 0, stderr_a)
        out_a = json.loads(stdout_a)

        parked = self.pending(root)["entries"]
        a_keys = {dedupe_key(f"{ANTH}/news/a{n}") for n in range(4)}
        b_keys = {dedupe_key(f"{ANTH}/news/b{n}") for n in range(4)}
        self.assertTrue(a_keys & set(parked), "A's drops missing")
        self.assertTrue(b_keys & set(parked), "B's entries were clobbered by A")
        # A must report the merged truth, not its own stale baseline.
        self.assertEqual(out_a["pending_total"], len(parked))
        self.assertEqual(len(parked), 8)  # 1 selected + 3 dropped per source

        state = self.sources_state(root)["sources"]
        self.assertEqual(set(state), {"a", "b"})
        for sid in ("a", "b"):
            self.assertTrue(state[sid]["initialized"])
            self.assertEqual(state[sid]["last_status"], "ok")
            self.assertEqual(state[sid]["failures"], 0)
            self.assertEqual(state[sid]["pending"], 4)


class TestBatch2ReviewFixes(ReliabilityCase):
    def glm_source(self, sid: str = "glm", path: str = "/glm", **over) -> dict:
        src = {
            "id": sid,
            "label": "GLM",
            "type": "docs",
            "adapter": "glm_docs",
            "list_url": self.base(path),
            "extractor": "direct",
            "enabled": True,
        }
        src.update(over)
        return src

    def glm_page(self, sections: list[tuple[str, str, str, str]]) -> str:
        parts = []
        for sid, date, title, body in sections:
            parts.append(
                f'<div class="update" id="{sid}">'
                f'<span data-component-part="update-label">{date}</span>'
                f'<h3 data-component-part="update-description">{title}</h3>'
                f'<div data-component-part="update-content"><p>{body}</p></div></div>'
            )
        return "<html><body>" + "".join(parts) + "</body></html>"

    def test_glm_selected_and_parked_docs_rollout_retains_content_and_raw(self):
        today = now().strftime("%Y-%m-%d")
        v1 = self.glm_page([
            ("s1", today, "Section One", "Body one"),
            ("s2", today, "Section Two", "Body two"),
        ])
        v2 = self.glm_page([
            ("s3", today, "Section Three", "Body three"),
        ])
        self.route("/glm", v1)
        root = self.make_project(
            [self.glm_source()],
            defaults={"window_hours": 36, "max_items_per_source": 8, "backfill_items": 1},
        )
        self.init_sources(root, ["glm"])
        # Run 1: window mode with global --max 1 -> s1 selected, s2 parked by global_max
        out1 = self.run_fetch(root, "--max", "1")
        self.assertEqual(len(out1["candidates"]), 1)
        c1 = out1["candidates"][0]
        self.assertTrue(c1["url"].endswith("#s1"))
        self.assertTrue(c1["content_file"])
        self.assertTrue(Path(c1["content_file"]).exists())
        self.assertEqual(Path(c1["content_file"]).read_text(encoding="utf-8").strip(), "Body one")
        self.assertTrue(c1["raw_file"])
        raw1_path = root / c1["raw_file"]
        self.assertTrue(raw1_path.exists())

        # Check s2 is parked in pending and has content/cache/raw preserved
        pend1 = self.pending(root)["entries"]
        s2_key = f"{self.base('/glm')}#s2"
        self.assertIn(s2_key, pend1)
        s2_rec = pend1[s2_key]
        self.assertTrue(s2_rec.get("cache_name"))
        cache2_path = root / ".state" / "candidates" / s2_rec["cache_name"]
        self.assertTrue(cache2_path.exists(), f"Parked docs cache {cache2_path} must exist before rollout")
        self.assertEqual(cache2_path.read_text(encoding="utf-8").strip(), "Body two")

        # Run 2: page rolls off s1 & s2; v2 only has s3.
        self.route("/glm", v2)
        out2 = self.run_fetch(root, "--max", "10")
        cands2 = {c["url"]: c for c in out2["candidates"]}
        self.assertIn(c1["url"], cands2)
        retried_s1 = cands2[c1["url"]]
        self.assertTrue(retried_s1.get("content_file"), "Rolled off s1 must retain content_file")
        self.assertTrue(Path(retried_s1["content_file"]).exists())
        self.assertEqual(Path(retried_s1["content_file"]).read_text(encoding="utf-8").strip(), "Body one")
        self.assertTrue(retried_s1.get("cache_name"))

        self.assertIn(s2_key, cands2)
        retried_s2 = cands2[s2_key]
        self.assertTrue(retried_s2.get("content_file"), "Rolled off s2 (previously quota-parked) must retain content_file")
        self.assertTrue(Path(retried_s2["content_file"]).exists())
        self.assertEqual(Path(retried_s2["content_file"]).read_text(encoding="utf-8").strip(), "Body two")

    def test_glm_source_failure_does_not_purge_pending_caches(self):
        today = now().strftime("%Y-%m-%d")
        v1 = self.glm_page([("s1", today, "Section One", "Body one")])
        self.route("/glm", v1)
        root = self.make_project([self.glm_source()])
        out1 = self.run_fetch(root, "--backfill", "1")
        self.assertEqual(len(out1["candidates"]), 1)
        c1 = out1["candidates"][0]
        cache_file = Path(c1["content_file"])
        self.assertTrue(cache_file.exists())

        # Run 2: source 500 error
        self.route("/glm", "server error", status=500)
        out2 = self.run_fetch(root)
        self.assertEqual(out2["sources"][0]["status"], "error")
        # Cache file must NOT be purged
        self.assertTrue(cache_file.exists(), "Cache file must not be purged on source error")

    def test_glm_same_day_page_change_does_not_clobber_old_raw(self):
        today = now().strftime("%Y-%m-%d")
        v1 = self.glm_page([("s1", today, "Section One", "Body one")])
        v2 = self.glm_page([("s2", today, "Section Two", "Body two")])
        self.route("/glm", v1)
        root = self.make_project([self.glm_source()])
        out1 = self.run_fetch(root, "--backfill", "1")
        raw1_rel = out1["candidates"][0]["raw_file"]
        raw1_path = root / raw1_rel
        self.assertTrue(raw1_path.exists())
        import gzip
        with gzip.open(raw1_path, "rt", encoding="utf-8") as f:
            raw1_text = f.read()
        self.assertIn("Section One", raw1_text)

        # Run 2: same day, page content changes to v2
        self.route("/glm", v2)
        out2 = self.run_fetch(root, "--backfill", "1")
        # Raw 1 must still contain Section One (not overwritten)
        with gzip.open(raw1_path, "rt", encoding="utf-8") as f:
            raw1_text_after = f.read()
        self.assertIn("Section One", raw1_text_after, "Old raw snapshot must not be overwritten by new page version")
        raw2_rel = out2["candidates"][0]["raw_file"]
        raw2_path = root / raw2_rel
        self.assertTrue(raw2_path.exists())
        with gzip.open(raw2_path, "rt", encoding="utf-8") as f:
            raw2_text = f.read()
        self.assertIn("Section Two", raw2_text)

    def test_daily_gate_park_candidates_preserves_queued_at_ttl_and_docs_paths_and_schema(self):
        import common
        import daily_gate as gate_mod
        root = self.make_project([])
        orig_root = gate_mod.ROOT
        orig_common_root = common.ROOT
        gate_mod.ROOT = root
        common.ROOT = root
        try:
            old_queued = iso(now() - timedelta(days=15))
            url = "http://example.com/glm#s1"
            park_key = dedupe_key(url, keep_fragment=True)
            initial_pending = {
                "schema": 2,
                "custom_meta": "keep_me",
                "updated_at": old_queued,
                "entries": {
                    park_key: {
                        "url": url,
                        "title": "GLM Section",
                        "source_id": "glm",
                        "queued_at": old_queued,
                        "content_file": "/tmp/c.md",
                        "cache_name": "glm-2026-10-29-s1.md",
                        "raw_file": ".state/raw/2026-10-29/glm.html.gz",
                        "reason": "awaiting_archive",
                    }
                }
            }
            (root / ".state" / "pending.json").write_text(json.dumps(initial_pending), encoding="utf-8")

            # Park via daily_gate with a bare record
            gate_mod.park_candidates([{"url": url}], "archive_failed", None)

            saved = json.loads((root / ".state" / "pending.json").read_text(encoding="utf-8"))
            self.assertEqual(saved.get("schema"), 2)
            self.assertEqual(saved.get("custom_meta"), "keep_me")
            entry = saved["entries"][park_key]
            self.assertEqual(entry["queued_at"], old_queued, "TTL clock must not be reset on re-park")
            self.assertEqual(entry["content_file"], "/tmp/c.md")
            self.assertEqual(entry["cache_name"], "glm-2026-10-29-s1.md")
            self.assertEqual(entry["raw_file"], ".state/raw/2026-10-29/glm.html.gz")
            self.assertEqual(entry["reason"], "archive_failed")
            self.assertTrue(entry.get("parked_at"))
        finally:
            gate_mod.ROOT = orig_root
            common.ROOT = orig_common_root

    def test_merge_pending_entries_protects_same_second_non_time_updates(self):
        url = "https://x/same-sec"
        key = dedupe_key(url)
        same_time = "2026-10-29T12:00:00+08:00"
        base = {
            key: {
                "url": url,
                "title": "Title Base",
                "source_id": "x",
                "updated_at": same_time,
                "queued_at": same_time,
                "reason": "awaiting_archive",
            }
        }
        # Another process in the same second updated title and content_file
        disk = {
            key: {
                "url": url,
                "title": "Title Updated Concurrently",
                "content_file": "/path/to/new.md",
                "source_id": "x",
                "updated_at": same_time,
                "queued_at": same_time,
                "reason": "awaiting_archive",
            }
        }
        merged, removals, _ = fetch_mod.merge_pending_entries(
            disk_entries=disk,
            baseline_entries=base,
            upserts={},
            remove_keys={key},
        )
        self.assertNotIn(key, removals, "Concurrent update in same second must not be removed")
        self.assertIn(key, merged)
        self.assertEqual(merged[key]["title"], "Title Updated Concurrently")

    def test_expired_pending_item_rediscovered_from_feed(self):
        today = now().date()
        rel_path = "/news/keep-alive"
        url = f"{ANTH}{rel_path}"
        self.route("/news", html_list([(rel_path, "Still in feed", _en_date(today))]))
        root = self.make_project([self.html_source("anthropic", path="/news")])
        self.init_sources(root, ["anthropic"])

        expired_time = iso(now() - timedelta(days=35))
        key = dedupe_key(url)
        self.write_pending(root, {
            key: self.pending_record(url, sid="anthropic", queued_at=expired_time, updated_at=expired_time)
        })

        out = self.run_fetch(root)
        self.assertEqual(out["pending_expired"], 1)
        exits = [e for e in out["pending_exits"] if e["url"] == url]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["reason"], "expired")

        cand_urls = [c["url"] for c in out["candidates"]]
        self.assertIn(url, cand_urls)
        parked = self.pending(root)["entries"]
        self.assertIn(key, parked)
        self.assertNotEqual(parked[key]["queued_at"], expired_time)
        new_q = fetch_mod.parse_dt(parked[key]["queued_at"])
        self.assertGreater(new_q, now() - timedelta(days=1))


if __name__ == "__main__":
    unittest.main()
