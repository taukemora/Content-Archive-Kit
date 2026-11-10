#!/usr/bin/env python3
"""Phase 3A tests: source visibility, water mark, quota, no-date, GLM, dead config.

Every test builds a throwaway project copy under the temporary directory and
serves fixtures from a loopback HTTP server, so the real library/.state is never
touched.
"""
from __future__ import annotations

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

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))

from common import TZ, dedupe_key, iso, now  # noqa: E402
PY = _support.PYTHON
SCRATCH = _support.scratch_dir("p3a-")
COPY = ("common.py", "fetch_sources.py", "daily_gate.py", "build_digest.py")

RSS_TMPL = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>fixture</title>
{items}
</channel></rss>"""


def rss_item(title: str, link: str, pub: str) -> str:
    return (
        "<item>"
        f"<title>{title}</title><link>{link}</link>"
        f"<pubDate>{pub}</pubDate><description>{title} summary</description>"
        "</item>"
    )


def en_date(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def make_routes() -> dict[str, str]:
    today = now().date()
    d0 = today
    d1 = today - timedelta(days=1)
    d2 = today - timedelta(days=3)

    news_ok = f"""<html><body><ul>
<li><a class="title-link" href="/news/alpha">Alpha dated</a><time>{en_date(d0)}</time></li>
<li><a class="title-link" href="/news/{d1:%Y-%m-%d}/beta">Beta url dated</a></li>
<li><a class="title-link" href="/news/gamma">Gamma no date</a></li>
</ul></body></html>"""

    news_two = f"""<html><body><ul>
<li><a class="title-link" href="/news/one">News One</a><time>{en_date(d0)}</time></li>
<li><a class="title-link" href="/news/two">News Two</a><time>{en_date(d1)}</time></li>
</ul></body></html>"""

    kimi_two = f"""<html><body><main>
<div class="menu-card"><a href="/en/blog/kimi-a" aria-label="Kimi A"></a>
  <h4>Kimi A</h4><span>{d0:%Y-%m-%d}</span></div>
<div class="menu-card"><a href="/en/blog/kimi-b" aria-label="Kimi B"></a>
  <h4>Kimi B</h4><span>{d1:%Y-%m-%d}</span></div>
</main></body></html>"""

    def glm_section(sec_id: str, date_text: str, title: str, body: str) -> str:
        return f"""<div class="update" id="{sec_id}">
  <div><button data-component-part="update-label">{date_text}</button>
    <div data-component-part="update-description">{title}</div></div>
  <div data-component-part="update-content"><p>{body}</p></div>
</div>"""

    glm_two = "<html><body>" + "".join([
        glm_section("2026-08-26", "2026-08-26", "GLM A 上线", "A 内容"),
        glm_section("2026-8-19", "2026-8-19", "GLM B 上线", "B 内容"),
        glm_section("2026-06-16", "2026-06-16", "GLM C 上线", "C 内容"),
    ]) + "</body></html>"

    glm_one = "<html><body>" + glm_section(
        "2026-8-19", "2026-8-19", "GLM 示例旗舰模型上线", "旧版内容"
    ) + "</body></html>"

    return {
        "/rss-ok.xml": RSS_TMPL.format(items="".join([
            rss_item("RSS One", "https://openai.com/index/one", f"{d0:%a}, {d0.day} {d0:%b} {d0.year} 00:00:00 GMT"),
            rss_item("RSS Two", "https://openai.com/index/two", f"{d1:%a}, {d1.day} {d1:%b} {d1.year} 00:00:00 GMT"),
        ])),
        "/rss-empty.xml": RSS_TMPL.format(items=""),
        "/rss-broken.xml": "<html><body><h1>Just a moment…</h1></body></html>",
        "/rss-five.xml": RSS_TMPL.format(items="".join([
            rss_item(f"Five {i}", f"https://openai.com/index/five-{i}",
                     f"{d0:%a}, {d0.day} {d0:%b} {d0.year} 0{i}:00:00 GMT")
            for i in range(1, 6)
        ])),
        "/news-ok": news_ok,
        "/news-two": news_two,
        "/news-empty": "<html><body><ul></ul></body></html>",
        "/kimi-two": kimi_two,
        "/glm-two": glm_two,
        "/glm-one": glm_one,
    }


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, str] = {}

    def do_GET(self):  # noqa: N802
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


class ProjectCase(unittest.TestCase):
    """Creates an isolated project and a loopback fixture server."""

    @classmethod
    def setUpClass(cls):
        _Handler.routes = make_routes()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def base(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def make_project(self, sources: list[dict], defaults: dict | None = None) -> Path:
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
        (root / ".state" / "seen.json").write_text(
            json.dumps({"entries": {}}), encoding="utf-8"
        )
        (root / "library").mkdir()
        self._written = getattr(self, "_written", [])
        self._written.append(root)
        return root

    def tearDown(self):
        for root in getattr(self, "_written", []):
            shutil.rmtree(root, ignore_errors=True)
        self._written = []

    # --- helpers -------------------------------------------------------
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

    def run_fetch(self, root: Path, *args: str) -> dict:
        res = subprocess.run(
            [PY, str(root / "scripts" / "fetch_sources.py"), *args],
            capture_output=True, text=True, cwd=str(root), timeout=180,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def state(self, root: Path) -> dict:
        return json.loads((root / ".state" / "sources.json").read_text(encoding="utf-8"))

    def pending(self, root: Path) -> dict:
        path = root / ".state" / "pending.json"
        if not path.exists():
            return {"entries": {}}
        return json.loads(path.read_text(encoding="utf-8"))

    def init_sources(self, root: Path, sids: list[str]) -> None:
        """Mark sources as initialized so selection runs in window mode."""
        path = root / ".state" / "sources.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"sources": {}}
        data.setdefault("sources", {})
        for sid in sids:
            data["sources"].setdefault(sid, {})["initialized"] = True
        path.write_text(json.dumps(data), encoding="utf-8")

    def seen(self, root: Path, urls: list[str], published: str = "", source: str = "") -> None:
        path = root / ".state" / "seen.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for url in urls:
            key = dedupe_key(url)
            data["entries"][key] = {"id": key, "path": "library/tech/x/entry.md",
                                    "first_seen": iso()}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def archive_entry(self, root: Path, url: str, published: str, source: str) -> None:
        """Create a library entry so the water mark derives from it."""
        entry_id = f"fixture-{abs(hash(url)) % 100000}"
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
        (d / "entry.md").write_text(
            "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n# x\n",
            encoding="utf-8",
        )
        self.seen(root, [url])


class TestSourceVisibility(ProjectCase):
    def test_empty_rss_is_not_ok_and_never_initialized(self):
        root = self.make_project([
            self.source("openai", adapter="openai_rss", feed=self.base("/rss-empty.xml"),
                        type="rss"),
        ])
        out = self.run_fetch(root)
        src = out["sources"][0]
        self.assertEqual(src["status"], "empty")
        self.assertIn("entries=0", src["empty_reason"])
        self.assertEqual(src["selected"], 0)

        st = self.state(root)["sources"]["openai"]
        self.assertNotEqual(st.get("initialized"), True)
        self.assertNotIn("last_newest", st)
        self.assertEqual(st["failures"], 1)
        self.assertFalse(st["needs_attention"])

    def test_broken_feed_html_is_empty_not_ok(self):
        root = self.make_project([
            self.source("openai", adapter="openai_rss", feed=self.base("/rss-broken.xml"),
                        type="rss"),
        ])
        out = self.run_fetch(root)
        self.assertEqual(out["sources"][0]["status"], "empty")

    def test_empty_html_list_is_empty(self):
        root = self.make_project([
            self.source("anthropic", list_url=self.base("/news-empty")),
        ])
        out = self.run_fetch(root)
        self.assertEqual(out["sources"][0]["status"], "empty")
        self.assertEqual(self.state(root)["sources"]["anthropic"]["failures"], 1)

    def test_three_consecutive_failures_set_needs_attention(self):
        root = self.make_project([
            self.source("openai", adapter="openai_rss", feed=self.base("/rss-empty.xml"),
                        type="rss"),
        ])
        for _ in range(3):
            out = self.run_fetch(root)
        src = out["sources"][0]
        self.assertEqual(src["status"], "empty")
        self.assertTrue(src["needs_attention"])
        self.assertEqual(src["streak"], 3)
        self.assertEqual(self.state(root)["sources"]["openai"]["failures"], 3)

    def test_ok_run_resets_failure_streak(self):
        root = self.make_project([
            self.source("openai", adapter="openai_rss", feed=self.base("/rss-empty.xml"),
                        type="rss"),
        ])
        self.run_fetch(root)
        cfg = yaml.safe_load((root / "sources.yaml").read_text(encoding="utf-8"))
        cfg["sources"][0]["feed"] = self.base("/rss-ok.xml")
        (root / "sources.yaml").write_text(
            yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        out = self.run_fetch(root)
        self.assertEqual(out["sources"][0]["status"], "ok")
        self.assertFalse(out["sources"][0]["needs_attention"])
        st = self.state(root)["sources"]["openai"]
        self.assertEqual(st["failures"], 0)
        self.assertTrue(st["initialized"])


    def test_unknown_source_id_fails_loudly(self):
        root = self.make_project([self.source("anthropic", list_url=self.base("/news-two"))])
        res = subprocess.run(
            [PY, str(root / "scripts" / "fetch_sources.py"), "--source", "nope", "--dry-run"],
            capture_output=True, text=True, cwd=str(root), timeout=120,
        )
        self.assertEqual(res.returncode, 2)
        self.assertIn("未知的源 id", res.stderr)

    def test_source_flag_limits_run_to_one_source(self):
        root = self.make_project([
            self.source("anthropic", list_url=self.base("/news-two")),
            self.source("kimi", adapter="kimi_html", list_url=self.base("/kimi-two")),
        ])
        self.init_sources(root, ["anthropic", "kimi"])
        out = self.run_fetch(root, "--source", "kimi")
        self.assertEqual([s["id"] for s in out["sources"]], ["kimi"])
        self.assertEqual({c["source_id"] for c in out["candidates"]}, {"kimi"})


class TestNoDate(ProjectCase):
    def test_no_date_item_survives_with_fetched_date(self):
        root = self.make_project([self.source("anthropic", list_url=self.base("/news-ok"))])
        self.init_sources(root, ["anthropic"])
        out = self.run_fetch(root)

        self.assertEqual(out["sources"][0]["mode"], "window:36h")
        self.assertEqual(len(out["candidates"]), 3)
        self.assertEqual(out["no_date_total"], 1)
        gamma = [c for c in out["candidates"] if c["url"].endswith("/news/gamma")]
        self.assertEqual(len(gamma), 1)
        g = gamma[0]
        self.assertTrue(g["published_unknown"])
        self.assertEqual(g["date_source"], "fetched")
        self.assertEqual(g["published"][:10], now().strftime("%Y-%m-%d"))
        self.assertEqual(out["sources"][0]["no_date"], 1)
        self.assertEqual(out["sources"][0]["no_date_selected"], 1)
        self.assertEqual(out["no_date_selected_total"], 1)

        # URL fallback keeps the real publication date and marks its source.
        beta = [c for c in out["candidates"] if c["url"].endswith("/beta")][0]
        self.assertFalse(beta["published_unknown"])
        self.assertEqual(beta["date_source"], "url")
        self.assertEqual(beta["date_precision"], "day")

        alpha = [c for c in out["candidates"] if c["url"].endswith("/news/alpha")][0]
        self.assertEqual(alpha["date_source"], "list")

    def test_no_date_item_participates_in_window(self):
        # Backfill bypass would hide the bug; window mode must keep it.
        root = self.make_project([self.source("anthropic", list_url=self.base("/news-ok"))])
        self.init_sources(root, ["anthropic"])
        out = self.run_fetch(root, "--window-hours", "6")
        urls = [c["url"] for c in out["candidates"]]
        self.assertTrue(any(u.endswith("/news/gamma") for u in urls))


class TestWatermarkAndQuota(ProjectCase):
    def test_date_only_padding_does_not_starve_same_day_items(self):
        root = self.make_project([self.source("anthropic", list_url=self.base("/news-ok"))])
        self.init_sources(root, ["anthropic"])
        today = now().strftime("%Y-%m-%d")
        self.archive_entry(
            root, "https://www.anthropic.com/news/older", f"{today}T00:00:00+08:00",
            "anthropic",
        )
        out = self.run_fetch(root)
        src = out["sources"][0]
        self.assertEqual(src["watermark"], f"{today}T00:00:00+08:00")
        urls = [c["url"] for c in out["candidates"]]
        # Same-day date-only item must still be selectable (§3 padding).
        self.assertIn("https://www.anthropic.com/news/alpha", urls)

    def test_items_at_or_before_watermark_are_skipped(self):
        root = self.make_project([self.source("anthropic", list_url=self.base("/news-ok"))])
        self.init_sources(root, ["anthropic"])
        today = now().strftime("%Y-%m-%d")
        self.archive_entry(
            root, "https://www.anthropic.com/news/alpha", f"{today}T00:00:00+08:00",
            "anthropic",
        )
        out = self.run_fetch(root)
        urls = [c["url"] for c in out["candidates"]]
        self.assertNotIn("https://www.anthropic.com/news/alpha", urls)
        self.assertEqual(out["sources"][0]["skipped"], 1)

    def test_fetch_never_advances_watermark(self):
        root = self.make_project([self.source("anthropic", list_url=self.base("/news-ok"))])
        self.init_sources(root, ["anthropic"])
        today = now().strftime("%Y-%m-%d")
        self.archive_entry(
            root, "https://www.anthropic.com/news/older", f"{today}T00:00:00+08:00",
            "anthropic",
        )
        self.run_fetch(root)
        first = self.state(root)["sources"]["anthropic"]["last_newest"]
        self.run_fetch(root)
        second = self.state(root)["sources"]["anthropic"]["last_newest"]
        self.assertEqual(first, second)
        self.assertEqual(first, f"{today}T00:00:00+08:00")

    def test_max_round_robin_and_global_dropped(self):
        root = self.make_project([
            self.source("anthropic", list_url=self.base("/news-two")),
            self.source("kimi", adapter="kimi_html", list_url=self.base("/kimi-two")),
        ])
        self.init_sources(root, ["anthropic", "kimi"])
        out = self.run_fetch(root, "--max", "3")
        self.assertEqual(len(out["candidates"]), 3)
        self.assertEqual(out["global_dropped"], 1)
        by_id = {s["id"]: s for s in out["sources"]}
        self.assertEqual(sum(s["selected"] for s in out["sources"]), 3)
        self.assertEqual(by_id["anthropic"]["selected"], 2)
        self.assertEqual(by_id["kimi"]["selected"], 1)
        self.assertEqual(by_id["kimi"]["dropped_by_max"], 1)
        # No source may exceed its round-robin share.
        self.assertNotEqual(by_id["anthropic"]["selected"], 4)

        pending = self.pending(root)
        # Selected work is durable too, until archive marks its URL seen.
        self.assertEqual(len(pending["entries"]), 4)
        awaiting = [r for r in pending["entries"].values()
                    if r["reason"] == "awaiting_archive"]
        self.assertEqual({r["url"] for r in awaiting},
                         {c["url"] for c in out["candidates"]})
        deferred = [r for r in pending["entries"].values()
                    if r["reason"] == "global_max"]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0]["source_id"], "kimi")

    def test_pending_item_is_picked_first_next_run(self):
        root = self.make_project([
            self.source("anthropic", list_url=self.base("/news-two")),
            self.source("kimi", adapter="kimi_html", list_url=self.base("/kimi-two")),
        ])
        self.init_sources(root, ["anthropic", "kimi"])
        out = self.run_fetch(root, "--max", "3")
        selected = [c["url"] for c in out["candidates"]]
        self.seen(root, selected)

        pending = self.pending(root)
        deferred = [r for r in pending["entries"].values()
                    if r["reason"] == "global_max"]
        self.assertEqual(len(deferred), 1)
        pending_url = deferred[0]["url"]
        out2 = self.run_fetch(root, "--max", "1")
        self.assertEqual([c["url"] for c in out2["candidates"]], [pending_url])
        self.assertEqual(out2["candidates"][0]["source_id"], "kimi")

    def test_five_items_two_per_run_all_archived_in_three_runs(self):
        """§9: with --max 2, D3–D5 must still land on later runs."""
        root = self.make_project([
            self.source("openai", adapter="openai_rss", feed=self.base("/rss-five.xml"),
                        type="rss"),
        ])
        self.init_sources(root, ["openai"])
        archived: list[str] = []
        for _ in range(3):
            out = self.run_fetch(root, "--max", "2")
            urls = [c["url"] for c in out["candidates"]]
            self.assertLessEqual(len(urls), 2)
            archived.extend(urls)
            # Simulate a successful archive: seen + library entry (water mark).
            for cand in out["candidates"]:
                self.archive_entry(root, cand["url"], cand["published"], "openai")

        self.assertEqual(len(archived), 5)
        self.assertEqual(len(set(archived)), 5)
        self.assertEqual(
            sorted(u.rsplit("-", 1)[-1] for u in archived), ["1", "2", "3", "4", "5"]
        )

    def test_backfill_filters_seen(self):
        root = self.make_project([self.source("anthropic", list_url=self.base("/news-ok"))])
        self.seen(root, ["https://www.anthropic.com/news/alpha"])
        out = self.run_fetch(root, "--backfill", "5")
        urls = [c["url"] for c in out["candidates"]]
        self.assertNotIn("https://www.anthropic.com/news/alpha", urls)
        self.assertEqual(len(urls), 2)
        self.assertEqual(out["sources"][0]["mode"], "backfill:5")

    def test_explicit_backfill_still_works_after_initialized(self):
        root = self.make_project([self.source("anthropic", list_url=self.base("/news-ok"))])
        first = self.run_fetch(root)
        self.assertEqual(first["sources"][0]["mode"], "backfill:2")
        self.assertTrue(self.state(root)["sources"]["anthropic"]["initialized"])

        second = self.run_fetch(root, "--backfill", "1")
        self.assertEqual(second["sources"][0]["mode"], "backfill:1")
        self.assertEqual(len(second["candidates"]), 1)

    def test_stale_days_reported(self):
        # GLM fixture's newest section is old, so the source is stale.
        root = self.make_project([{
            "id": "glm",
            "label": "GLM / 智谱",
            "type": "docs",
            "adapter": "glm_docs",
            "list_url": self.base("/glm-one"),
            "enabled": True,
        }])
        out = self.run_fetch(root, "--backfill", "1")
        self.assertGreaterEqual(out["sources"][0]["stale_days"], 14)
        self.assertEqual(out["sources"][0]["newest"][:10], "2026-08-19")


class TestGlm(ProjectCase):
    def glm_source(self, path: str = "/glm-two") -> dict:
        return {
            "id": "glm",
            "label": "GLM / 智谱",
            "type": "docs",
            "adapter": "glm_docs",
            "list_url": f"http://127.0.0.1:{self.port}{path}",
            "enabled": True,
        }

    def test_real_section_ids_padded_dates_and_raw_snapshot(self):
        root = self.make_project([self.glm_source()])
        out = self.run_fetch(root, "--backfill", "2")
        self.assertEqual(len(out["candidates"]), 2)
        by_date = {c["published"][:10]: c for c in out["candidates"]}
        self.assertEqual(sorted(by_date), ["2026-08-19", "2026-08-26"])

        a = by_date["2026-08-26"]
        b = by_date["2026-08-19"]
        self.assertTrue(a["url"].endswith("#2026-08-26"))
        self.assertTrue(b["url"].endswith("#2026-8-19"))  # real, unpadded page id
        for cand in (a, b):
            self.assertIsNotNone(cand["raw_file"])
            self.assertTrue((root / cand["raw_file"]).exists())
            self.assertTrue(cand["content_file"])
            self.assertTrue(Path(cand["content_file"]).exists())
        # One whole-page snapshot per run, shared by every section.
        self.assertEqual(a["raw_file"], b["raw_file"])
        self.assertEqual(len(list((root / ".state" / "raw").rglob("glm.html.gz"))), 1)
        self.assertEqual(b["title"], "GLM B 上线")

        cache = root / ".state" / "candidates"
        files = sorted(p.name for p in cache.glob("glm-*.md"))
        self.assertEqual(len(files), 2, files)

    def test_dry_run_writes_no_candidate_cache(self):
        root = self.make_project([self.glm_source()])
        out = self.run_fetch(root, "--backfill", "2", "--dry-run")
        self.assertEqual(len(out["candidates"]), 2)
        self.assertFalse((root / ".state" / "candidates").exists())
        self.assertFalse((root / ".state" / "sources.json").exists())
        self.assertFalse((root / ".state" / "pending.json").exists())
        self.assertFalse((root / ".state" / "runs").exists())

    def test_legacy_url_alias_still_counts_as_seen(self):
        """Old entries were keyed as #<date>-<slug>; those must not re-archive."""
        import fetch_sources as F

        list_url = "https://docs.bigmodel.cn/cn/update/new-releases"
        dt = now().replace(year=2026, month=8, day=19, hour=0, minute=0, second=0,
                          microsecond=0)
        cand = F.candidate(
            "GLM 示例旗舰模型上线",
            f"{list_url}#2026-8-19",
            "GLM / 智谱",
            dt=dt,
            date_source="list",
            precision="day",
            dedupe_keys=[
                f"{list_url}#2026-08-19-glm-5-3-新一代旗舰模型上线",
                f"{list_url}#2026-8-19-glm-5-3-新一代旗舰模型上线",
                f"{list_url}#2026-08-19",
            ],
        )
        self.assertTrue(cand["url"].endswith("#2026-8-19"))
        # Both the padded and the unpadded legacy shapes are recognised.
        for legacy in ("#2026-08-19-glm-5-3-新一代旗舰模型上线",
                       "#2026-8-19-glm-5-3-新一代旗舰模型上线"):
            seen = {dedupe_key(f"{list_url}{legacy}")}
            self.assertTrue(F.is_seen(cand, seen), legacy)
        self.assertFalse(F.is_seen(cand, {dedupe_key(f"{list_url}#2025-01-01-x")}))

    def test_anchored_sections_are_distinct_in_one_source(self):
        import fetch_sources as F

        a = f"{self.base('/glm-two')}#2026-08-26"
        b = f"{self.base('/glm-two')}#2026-8-19"
        self.assertNotEqual(F.internal_key(a), F.internal_key(b))

    def test_orphan_candidate_cache_cleanup(self):
        root = self.make_project([self.glm_source()])
        cache = root / ".state" / "candidates"
        cache.mkdir(parents=True)
        (cache / "glm-2020-01-01-old-section.md").write_text("stale", encoding="utf-8")
        (cache / "glm-2020-01-02-new-section.md").write_text("stale", encoding="utf-8")
        self.run_fetch(root, "--backfill", "1")
        names = {p.name for p in cache.glob("glm-*.md")}
        self.assertNotIn("glm-2020-01-01-old-section.md", names)
        self.assertNotIn("glm-2020-01-02-new-section.md", names)
        self.assertEqual(len(names), 1)


class TestDigestSurfacesHealth(ProjectCase):
    def write_index(self, root: Path, rows: list[dict]) -> None:
        import csv
        with open(root / "index.csv", "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    def run_digest(self, root: Path, run_file: Path) -> str:
        res = subprocess.run(
            [PY, str(root / "scripts" / "build_digest.py"), "--run", str(run_file),
             "--limit", "10"],
            capture_output=True, text=True, cwd=str(root), timeout=120,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def test_digest_reports_dropped_no_date_stale_and_needs_attention(self):
        root = self.make_project([self.source("anthropic", list_url=self.base("/news-ok"))])
        today = now().strftime("%Y-%m-%d")
        self.write_index(root, [{
            "id": "e1", "title": "正常文章", "source": "Anthropic",
            "published": f"{today}T10:00:00+08:00", "archived": f"{today}T12:00:00+08:00",
            "status": "archived", "content_note": "", "url": "https://x/1",
            "summary": "摘要",
        }])
        run = {
            "generated_at": f"{today}T11:00:00+08:00",
            "global_dropped": 2,
            "no_date_total": 1,
            "pending_total": 3,
            "sources": [
                {"id": "anthropic", "label": "Anthropic", "status": "ok",
                 "selected": 1, "skipped": 4, "no_date": 3, "no_date_selected": 1,
                 "stale_days": 20, "dropped_by_max": 2, "needs_attention": True,
                 "streak": 3},
                {"id": "glm", "label": "GLM / 智谱", "status": "empty",
                 "empty_reason": "文档更新页未解析到任何 div.update section",
                 "selected": 0, "skipped": 0, "no_date": 0, "stale_days": 62},
            ],
        }
        run_file = root / "run.json"
        run_file.write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")

        out = self.run_digest(root, run_file)
        self.assertIn("新增 1 篇", out)
        self.assertIn("因配额顺延 2 篇", out)
        self.assertIn("其中 1 条无日期，已按抓取日期暂存", out)
        self.assertIn("无日期候选 1 条", out)
        self.assertIn("待补队列 3 篇", out)
        self.assertIn("空/失败 1 源", out)
        self.assertIn("该源 20 天无更新", out)
        self.assertIn("该源 62 天无更新", out)
        self.assertIn("未达源（空/失败）", out)
        self.assertIn("GLM / 智谱：文档更新页未解析到任何 div.update section", out)
        self.assertIn("## 源状态", out)
        self.assertIn("需人工关注", out)

    def test_digest_without_new_rows_lists_sources(self):
        root = self.make_project([self.source("anthropic", list_url=self.base("/news-ok"))])
        self.write_index(root, [{
            "id": "e1", "title": "旧文章", "source": "Anthropic",
            "published": "2020-01-01T00:00:00+08:00",
            "archived": "2020-01-01T00:00:00+08:00",
            "status": "archived", "content_note": "", "url": "https://x/1",
            "summary": "摘要",
        }])
        run = {
            "generated_at": now().isoformat(timespec="seconds"),
            "global_dropped": 0, "no_date_total": 0, "pending_total": 0,
            "sources": [{"id": "kimi", "label": "Kimi", "status": "error",
                         "error": "timed out", "selected": 0, "skipped": 0}],
        }
        run_file = root / "run.json"
        run_file.write_text(json.dumps(run), encoding="utf-8")
        out = self.run_digest(root, run_file)
        self.assertIn("今日无新增。", out)
        self.assertIn("但不是全源正常", out)
        self.assertIn("Kimi：timed out", out)


class TestDailyGate(ProjectCase):
    def test_start_records_empty_sources_and_keeps_running(self):
        root = self.make_project([
            self.source("openai", adapter="openai_rss", feed=self.base("/rss-empty.xml"),
                        type="rss"),
            self.source("anthropic", list_url=self.base("/news-two")),
        ])
        res = subprocess.run(
            [PY, str(root / "scripts" / "daily_gate.py"), "start"],
            capture_output=True, text=True, cwd=str(root), timeout=180,
        )
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn("空/失败源 1 个", res.stderr)

        gate = json.loads((root / ".state" / "runs" / "daily-gate.json").read_text(encoding="utf-8"))
        self.assertEqual(gate["status"], "started")
        self.assertEqual([s["id"] for s in gate["not_ok"]], ["openai"])
        self.assertEqual(gate["not_ok"][0]["status"], "empty")
        self.assertTrue((root / ".state" / "runs" / "latest-fetch.json").exists())

    def test_start_fails_when_every_source_errors(self):
        root = self.make_project([
            self.source("broken", adapter="does_not_exist"),
            self.source("broken2", adapter="also_missing"),
        ])
        res = subprocess.run(
            [PY, str(root / "scripts" / "daily_gate.py"), "start"],
            capture_output=True, text=True, cwd=str(root), timeout=120,
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("[CRON_FAILURE]", res.stderr)
        gate = json.loads((root / ".state" / "runs" / "daily-gate.json").read_text(encoding="utf-8"))
        self.assertEqual(gate["status"], "failed")


class TestDeadConfigRemoved(unittest.TestCase):
    def test_taxonomy_has_no_classification_block(self):
        tx = yaml.safe_load(_support.template("taxonomy.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("classification", tx)
        self.assertIn("categories", tx)
        self.assertIn("contexts", tx)

    def test_sources_defaults_have_no_dead_keys(self):
        cfg = yaml.safe_load(_support.template("sources.yaml").read_text(encoding="utf-8"))
        defaults = cfg.get("defaults", {})
        self.assertNotIn("request_delay_seconds", defaults)
        self.assertNotIn("first_run_days", defaults)
        for src in cfg["sources"]:
            self.assertNotIn("request_delay_seconds", src)
            self.assertNotIn("first_run_days", src)

    def test_no_code_reads_removed_config(self):
        offenders = []
        for path in (ROOT / "scripts").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for key in ("request_delay_seconds", "first_run_days", "low_confidence_action",
                        "min_confidence"):
                if key in text:
                    offenders.append(f"{path.name}:{key}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
