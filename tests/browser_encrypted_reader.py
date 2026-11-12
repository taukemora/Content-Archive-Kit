#!/usr/bin/env python3
"""Real-Chromium acceptance probe for the encrypted reader (synthetic data).

Run with the isolated Playwright environment supplied by the parent agent:

  PLAYWRIGHT_BROWSERS_PATH=... /path/to/ca-pages-browser-venv/bin/python \
      tests/browser_encrypted_reader.py --dir <built-site> --passphrase ...

The script never touches production keys, library or network.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright, expect, TimeoutError as PlaywrightTimeoutError

TITLE_A = "加密标题甲"
TITLE_B = "加密标题乙"
NEEDLE_A = "ALPHANEEDLE"
NEEDLE_B = "BETANEEDLE"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401 - silence test server
        pass


def serve(directory: Path, handler_cls=QuietHandler):
    handler = functools.partial(handler_cls, directory=str(directory))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


MEDIA_SIZE_SCRIPT = """
(() => {
  const original = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src');
  if (!original || !original.set) return;
  window.__caMediaSizes = [];
  Object.defineProperty(HTMLImageElement.prototype, 'src', {
    configurable: original.configurable,
    enumerable: original.enumerable,
    get: original.get,
    set(value) {
      const before = this.getBoundingClientRect().height;
      original.set.call(this, value);
      if (String(value).startsWith('blob:')) {
        window.__caMediaSizes.push({
          before,
          after: this.getBoundingClientRect().height,
          naturalWidth: this.naturalWidth,
          complete: this.complete,
        });
      }
    },
  });
})();
"""


def make_media_handler(events, state, lock, slow_ms: int, delay_ms: int):
    """Delay known media objects during the measured page load.

    The first pass only discovers object paths, so the probe knows exactly
    which requests are media. The measured second pass sets ``phase`` to
    ``delayed`` and ``media`` to that path set; the first such request gets the
    long delay and the rest a short one. Index and article body requests are
    never delayed, and no request-order guessing is involved.
    """

    class MediaHandler(QuietHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            rel = path.lstrip("/")
            entry = None
            delay = 0.0
            gate = None
            if "/objects/" in path:
                with lock:
                    if state.get("phase") == "delayed" and rel in state.get("media", set()):
                        state["seq"] += 1
                        delay = slow_ms / 1000.0 if state["seq"] == 1 else delay_ms / 1000.0
                    entry = {
                        "path": rel,
                        "start": time.monotonic(),
                        "end": None,
                        "delay": delay,
                        "phase": state.get("phase", "boot"),
                        "label": state.get("label", "boot"),
                        "seq": state.get("seq", 0),
                    }
                    events.append(entry)
                    gate = state.get("gate") if rel in state.get("media", set()) else None
                # A response barrier makes the in-flight window deterministic:
                # the test holds the first wave open, acts while it is still in
                # flight, and only then releases the bytes.
                if gate is not None:
                    gate.wait(20)
                if delay:
                    time.sleep(delay)
            try:
                super().do_GET()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                if entry is not None:
                    entry["end"] = time.monotonic()

    return MediaHandler


MEDIA_PROBE_SCRIPT = """
(() => {
  const originalFetch = window.fetch;
  window.__caFetchProbes = [];
  window.fetch = function (input, init) {
    try {
      const raw = typeof input === 'string' ? input : (input && input.url) || '';
      const abs = new URL(raw, window.location.href);
      if (abs.origin === window.location.origin && abs.pathname.indexOf('/objects/') === 0) {
        const rel = abs.pathname.slice(1);
        const nodes = document.querySelectorAll(
          '[data-ca-src="' + rel + '"], [data-ca-href="' + rel + '"]'
        );
        const pane = document.getElementById('reader-pane');
        const paneRect = pane ? pane.getBoundingClientRect() : null;
        window.__caFetchProbes.push({
          rel: rel,
          at: performance.now(),
          occurrenceCount: nodes.length,
          rects: Array.prototype.map.call(nodes, function (node) {
            const r = node.getBoundingClientRect();
            return { top: r.top, bottom: r.bottom, width: r.width, height: r.height };
          }),
          pane: paneRect ? {
            top: paneRect.top, bottom: paneRect.bottom,
            width: paneRect.width, height: paneRect.height,
          } : null,
          paneDisplay: pane ? getComputedStyle(pane).display : null,
          paneScrollTop: pane ? pane.scrollTop : null,
        });
      }
    } catch (e) { /* the observer must never disturb the real request */ }
    return originalFetch.apply(this, arguments);
  };
})();
"""

RECT_SCRIPT = (
    "els => els.map(e => { const r = e.getBoundingClientRect();"
    " return { rel: e.getAttribute('data-ca-src') || e.getAttribute('data-ca-href'),"
    " top: r.top, bottom: r.bottom, width: r.width, height: r.height }; })"
)


def _in_prefetch_window(rect: dict, pane: dict, factor: float = 2.0) -> bool:
    """True when one occurrence sits inside the scheduler's +/- window."""
    if not pane or pane["width"] <= 0 or pane["height"] <= 0:
        return False
    if rect["width"] <= 0 or rect["height"] <= 0:
        return False
    return (
        rect["bottom"] >= pane["top"] - factor * pane["height"]
        and rect["top"] <= pane["bottom"] + factor * pane["height"]
    )


def _frame_state(probe: dict) -> str:
    """Classify one request-time probe: near / far / unmeasurable / hidden.

    Classification uses the geometry, pane and scroll position observed at the
    instant the browser actually issued the media request, so layout reflow
    after earlier images decode cannot retroactively relabel a request.
    """
    pane = probe.get("pane")
    if (
        probe.get("paneDisplay") == "none"
        or not pane
        or pane["width"] <= 0
        or pane["height"] <= 0
    ):
        return "hidden"
    measurable = [
        rect for rect in probe.get("rects", []) if rect["width"] > 0 and rect["height"] > 0
    ]
    if not measurable:
        return "unmeasurable"
    if any(_in_prefetch_window(rect, pane) for rect in measurable):
        return "near"
    return "far"


def _peak_concurrency(events: list[dict]) -> int:
    points: list[tuple[float, int]] = []
    for event in events:
        end = event["end"] if event["end"] is not None else event["start"]
        points.append((event["start"], 1))
        points.append((end, -1))
    points.sort(key=lambda point: (point[0], point[1]))
    peak = 0
    current = 0
    for _moment, delta in points:
        current += delta
        peak = max(peak, current)
    return peak


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True)
    parser.add_argument("--passphrase", required=True)
    parser.add_argument(
        "--mode",
        choices=("normal", "tamper", "tamper-site", "tamper-article", "tamper-image", "decode-error", "media"),
        default="normal",
    )
    parser.add_argument("--tamper-rel", default="")
    parser.add_argument(
        "--article-title",
        default="",
        help="card title to open in media mode (default: the first card)",
    )
    parser.add_argument(
        "--mobile-article-title",
        default=TITLE_B,
        help="card title to open in the mobile media phase",
    )
    parser.add_argument(
        "--repeat-link-text",
        default="重复图4附件",
        help="visible text of the duplicate attachment link in media mode",
    )
    parser.add_argument("--slow-first-ms", type=int, default=1500)
    parser.add_argument("--image-delay-ms", type=int, default=120)
    parser.add_argument(
        "--browser",
        default=os.environ.get("CA_PLAYWRIGHT_BROWSER", ""),
        help="optional Chromium executable (generic override for a mismatched Playwright cache)",
    )
    args = parser.parse_args()

    site_dir = Path(args.dir).resolve()
    if args.mode == "tamper-site":
        site_path = site_dir / "site.json"
        site = json.loads(site_path.read_text(encoding="utf-8"))
        site["kdf"]["iterations"] = 1
        site_path.write_text(json.dumps(site), encoding="utf-8")
    elif args.mode.startswith("tamper"):
        rel = args.tamper_rel
        if not rel:
            site = json.loads((site_dir / "site.json").read_text(encoding="utf-8"))
            rel = site["index"]["file"]
        target = site_dir / rel
        blob = bytearray(target.read_bytes())
        blob[-1] ^= 0x01
        target.write_bytes(bytes(blob))

    media_events: list[dict] = []
    media_state = {
        "phase": "boot",
        "seq": 0,
        "media": set(),
        "label": "boot",
        "gate": None,
    }
    media_lock = threading.Lock()
    media_rels: list[str] = []
    requested: set[str] = set()
    pane = None
    geometry: list[dict] = []
    decoded: list[dict] = []
    media_probes: list[dict] = []
    mobile_detail: dict = {}
    handler_cls = (
        make_media_handler(media_events, media_state, media_lock, args.slow_first_ms, args.image_delay_ms)
        if args.mode == "media"
        else QuietHandler
    )
    server, base = serve(site_dir, handler_cls)
    failures: list[str] = []
    requests: list[str] = []
    console_errors: list[str] = []
    runtime_errors: list[str] = []

    try:
        with sync_playwright() as pw:
            launch_options = {"headless": True}
            if args.browser:
                launch_options["executable_path"] = args.browser
            browser = pw.chromium.launch(**launch_options)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on("request", lambda req: requests.append(req.url))
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
            page.on("pageerror", lambda err: runtime_errors.append(str(err)))
            if args.mode == "media":
                page.add_init_script(MEDIA_SIZE_SCRIPT)
                page.add_init_script(MEDIA_PROBE_SCRIPT)

            page.goto(base + "/index.html", wait_until="load")
            expect(page.locator("#ca-unlock")).to_be_visible()
            if page.locator(".article-card").count() != 0:
                failures.append("articles visible before unlock")
            text = page.locator("body").inner_text()
            for needle in (TITLE_A, TITLE_B, NEEDLE_A):
                if needle in text:
                    failures.append(f"plaintext before unlock: {needle}")
            if any("site.json" in url or "objects/" in url for url in requests):
                failures.append("locked page fetched article data")

            if args.mode in ("tamper", "tamper-site"):
                page.fill("#ca-passphrase", args.passphrase)
                page.click("#ca-unlock-submit")
                expect(page.locator("#ca-unlock-error")).to_be_visible(timeout=30000)
                if page.locator(".article-card").count() != 0:
                    failures.append("tampered site rendered cards")
                if NEEDLE_A in page.locator("body").inner_text():
                    failures.append("tampered site leaked body")
            elif args.mode in ("tamper-article", "tamper-image", "decode-error"):
                page.fill("#ca-passphrase", args.passphrase)
                page.click("#ca-unlock-submit")
                expect(page.locator(".article-card")).to_have_count(2, timeout=30000)
                page.locator(".article-card", has_text=TITLE_A).click()
                if args.mode == "tamper-article":
                    expect(page.locator(".article-body .alert-warning")).to_be_visible(timeout=30000)
                    if NEEDLE_A in page.locator(".article-body").inner_text():
                        failures.append("tampered article body was rendered")
                else:
                    expect(page.locator(".article-body .img-missing")).to_be_visible(timeout=30000)
                    if NEEDLE_A not in page.locator(".article-body").inner_text():
                        failures.append("image failure broke the rest of the article")
                    if page.locator(".article-body img[data-ca-src]").count():
                        src = page.locator(".article-body img[data-ca-src]").first.get_attribute("src")
                        if src and src.startswith("blob:"):
                            failures.append("failed image retained a blob URL")
            elif args.mode == "media":
                def unlock(pg):
                    pg.fill("#ca-passphrase", args.passphrase)
                    pg.click("#ca-unlock-submit")

                def media_hits(label: str, rels=None) -> list[dict]:
                    with media_lock:
                        return [
                            dict(event)
                            for event in media_events
                            if event.get("phase") == "delayed"
                            and event.get("label") == label
                            and (rels is None or event["path"] in rels)
                        ]

                def wait_for_starts(
                    pg, label: str, count: int, timeout: float = 15.0, rels=None
                ) -> bool:
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        if len({hit["path"] for hit in media_hits(label, rels)}) >= count:
                            return True
                        pg.wait_for_timeout(50)
                    return False

                def wait_for_requests_ended(
                    label: str, timeout: float = 15.0, rels=None
                ) -> bool:
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        hits_now = media_hits(label, rels)
                        if hits_now and all(hit["end"] is not None for hit in hits_now):
                            return True
                        time.sleep(0.05)
                    return False

                def wait_quiescent(
                    pg, label: str, idle: float = 0.6, timeout: float = 10.0, rels=None
                ) -> None:
                    deadline = time.monotonic() + timeout
                    latest = None
                    stable_since = None
                    while time.monotonic() < deadline:
                        current_latest = max(
                            (hit["start"] for hit in media_hits(label, rels)), default=0.0
                        )
                        if current_latest != latest:
                            latest = current_latest
                            stable_since = time.monotonic()
                        elif stable_since is not None and time.monotonic() - stable_since >= idle:
                            return
                        pg.wait_for_timeout(50)

                def rect_map(pg) -> dict[str, list[dict]]:
                    rects = pg.eval_on_selector_all(
                        ".article-body img[data-ca-src], .article-body a[data-ca-href]",
                        RECT_SCRIPT,
                    )
                    grouped: dict[str, list[dict]] = {}
                    for rect in rects:
                        grouped.setdefault(rect["rel"], []).append(rect)
                    return grouped

                def pane_box(pg) -> dict:
                    return pg.evaluate(
                        "() => { const r = document.getElementById('reader-pane')"
                        ".getBoundingClientRect(); return { x: r.x, y: r.y,"
                        " top: r.top, bottom: r.bottom,"
                        " width: r.width, height: r.height }; }"
                    )

                rel_set: set[str] = set()
                page.fill("#ca-passphrase", args.passphrase)
                page.click("#ca-unlock-submit")
                page.wait_for_function(
                    "() => document.querySelectorAll('.article-card').length >= 1",
                    timeout=30000,
                )
                if args.article_title:
                    card = page.locator(".article-card", has_text=args.article_title).first
                else:
                    card = page.locator(".article-card").first
                desktop_gate = None
                if card.count() == 0:
                    failures.append(f"article card not found: {args.article_title!r}")
                else:
                    # Discovery pass: learn the article id and every media
                    # occurrence (images and attachment links share objects).
                    card.click()
                    page.wait_for_function(
                        "() => document.querySelectorAll('.article-body img[data-ca-src]').length >= 1",
                        timeout=30000,
                    )
                    media_rels = page.eval_on_selector_all(
                        ".article-body img[data-ca-src], .article-body a[data-ca-href]",
                        "els => els.map(e => e.getAttribute('data-ca-src')"
                        " || e.getAttribute('data-ca-href'))",
                    )
                    article_id = page.evaluate(
                        "() => { const c = document.querySelector('.article-card.active');"
                        " return c ? c.getAttribute('data-id') : null; }"
                    )
                    if len(set(media_rels)) < 6:
                        failures.append(f"media fixture has only {len(set(media_rels))} objects")
                    if len(media_rels) < len(set(media_rels)) + 2:
                        failures.append("media fixture does not repeat an object")
                    if not article_id:
                        failures.append("could not resolve the open article id")
                    else:
                        # Measured pass: reload the same article with known
                        # media objects, so the server delays those exact
                        # requests instead of guessing from arrival order.
                        page.goto("about:blank")
                        page.goto(
                            base + "/index.html#" + urllib.parse.quote(article_id, safe=""),
                            wait_until="load",
                        )
                        rel_set = set(media_rels)
                        desktop_gate = threading.Event()
                        with media_lock:
                            media_state["media"] = set(media_rels)
                            media_state["phase"] = "delayed"
                            media_state["label"] = "media-desktop"
                            media_state["seq"] = 0
                            media_state["gate"] = desktop_gate
                        unlock(page)
                        page.wait_for_function(
                            "() => document.querySelectorAll('.article-body img[data-ca-src]').length >= 6",
                            timeout=30000,
                        )
                pane = pane_box(page)
                # Geometry at the instant the measured page settled, before any
                # response bytes were released: every image still shows its
                # pending placeholder box.
                initial_rects = rect_map(page)
                if not wait_for_starts(page, "media-desktop", 3, rels=rel_set):
                    failures.append("the desktop first wave never reached three concurrent requests")
                if desktop_gate is not None:
                    desktop_gate.set()
                # Wait for real decoded pixels, not merely a blob URL.
                try:
                    page.wait_for_function(
                        "() => { const imgs = [...document.querySelectorAll("
                        "'.article-body img[data-ca-src]')];"
                        " return imgs.filter(i => i.src.startsWith('blob:')"
                        " && i.complete && i.naturalWidth > 0).length >= 2; }",
                        timeout=20000,
                    )
                except PlaywrightTimeoutError:
                    failures.append("fewer than two desktop images decoded")
                # Wait until the deliberately slow request has actually ended so
                # its interval is sealed from measured bytes, not from a sleep.
                if not wait_for_requests_ended("media-desktop", timeout=15, rels=rel_set):
                    failures.append("a desktop media request never completed")
                wait_quiescent(page, "media-desktop", rels=rel_set)
                probes = page.evaluate("window.__caFetchProbes || []")
                rest_rects = rect_map(page)
                geometry = page.eval_on_selector_all(
                    ".article-body img[data-ca-src]", RECT_SCRIPT
                )
                decoded = page.eval_on_selector_all(
                    ".article-body img[data-ca-src]",
                    "els => els.map(e => ({ rel: e.getAttribute('data-ca-src'),"
                    " src: e.getAttribute('src'), ready: e.complete && e.naturalWidth > 0,"
                    " width: e.naturalWidth }))",
                )
                media_sizes = page.evaluate("window.__caMediaSizes || []")
                with media_lock:
                    snapshot_time = time.monotonic()
                    snapshot = [dict(event) for event in media_events]
                hits = []
                for event in snapshot:
                    if (
                        event["path"] in rel_set
                        and event.get("phase") == "delayed"
                        and event.get("label") == "media-desktop"
                    ):
                        # A request that is still in flight at snapshot time was
                        # started and must be sealed, not treated as absent.
                        if event["end"] is None:
                            event["end"] = snapshot_time
                        hits.append(event)
                hits.sort(key=lambda event: event["start"])
                requested = {event["path"] for event in hits}
                media_probes = [probe for probe in probes if probe["rel"] in rel_set]
                fold_top = pane["y"] if pane else 0
                fold_bottom = pane["y"] + pane["height"] if pane else 0
                if not pane or pane["width"] <= 0 or pane["height"] <= 0:
                    failures.append("reader pane was not measurable")
                if len(hits) < 4:
                    failures.append(f"expected at least four prefetched objects, saw {len(hits)}")
                else:
                    slow_hits = [event for event in hits if event["delay"] >= args.slow_first_ms / 2000.0]
                    if not slow_hits:
                        failures.append("the deliberately slow media request was not delayed")
                    else:
                        slow = slow_hits[0]
                        others = [event for event in hits if event is not slow]
                        if not any(event["start"] < slow["end"] for event in others):
                            failures.append("other images waited for the slow image to finish")
                    # Peak concurrent image requests.
                    peak = _peak_concurrency(hits)
                    if peak > 3:
                        failures.append(f"more than three concurrent media requests: {peak}")
                    if peak < 2:
                        failures.append(f"media requests were effectively serial: peak {peak}")
                    # The image intersecting the pane belongs to the first wave.
                    visible_initial = {
                        rel
                        for rel, rects in initial_rects.items()
                        if any(
                            rect["width"] > 0
                            and rect["height"] > 0
                            and rect["bottom"] >= fold_top
                            and rect["top"] <= fold_bottom
                            for rect in rects
                        )
                    }
                    earliest = min(event["start"] for event in hits)
                    first_wave = {
                        event["path"]
                        for event in hits
                        if event["start"] <= earliest + 0.15 and event.get("phase") == "delayed"
                    }
                    if not visible_initial:
                        failures.append("no occurrence intersected the pane before the first wave")
                    elif not visible_initial <= first_wave:
                        failures.append(
                            "a pane-visible object was not in the first request wave: "
                            f"visible={len(visible_initial)} "
                            f"first={len(first_wave & visible_initial)}"
                        )
                # Every media request must be justified by the geometry that
                # existed when the browser issued it. Judging later requests with
                # the initial layout is wrong: earlier images decode and reflow
                # the gallery in both directions.
                for probe in media_probes:
                    state = _frame_state(probe)
                    if state == "far":
                        tops = ", ".join(
                            f"{rect['top']:.0f}" for rect in probe.get("rects", [])
                        )
                        failures.append(
                            "an object with every occurrence outside the "
                            f"two-screen window was fetched: {probe['rel'][-12:]} "
                            f"top=[{tops}] pane={probe['pane']} "
                            f"scrollTop={probe['paneScrollTop']}"
                        )
                    elif state in ("hidden", "unmeasurable"):
                        failures.append(
                            "media was requested while the reader pane was not "
                            f"measurable: {probe['rel'][-12:]} ({state})"
                        )
                if len(media_probes) != len(hits):
                    failures.append(
                        f"browser fetch probes ({len(media_probes)}) disagree with "
                        f"server media requests ({len(hits)})"
                    )
                # No blob src assignment may pass through a zero-height window
                # between setting the URL and decoding it.
                if not media_sizes:
                    failures.append("no blob src assignment was observed")
                collapsed = [
                    size
                    for size in media_sizes
                    if size["before"] >= 120 and size["after"] < 120 and not size["naturalWidth"]
                ]
                if collapsed:
                    failures.append(
                        f"pending image collapsed to zero height before decoding: {len(collapsed)}"
                    )
                # Distant objects (outside the two-screen prefetch window at rest)
                # must not be started before scrolling. Duplicate occurrences are
                # ranked by their best node, not by a spanning bounding box.
                if pane and pane["width"] > 0 and pane["height"] > 0:
                    far_at_rest = {
                        rel
                        for rel, rects in rest_rects.items()
                        if not any(_in_prefetch_window(rect, pane) for rect in rects)
                    }
                    fetched_far = requested & far_at_rest
                    if fetched_far:
                        failures.append(
                            f"far objects were fetched before scrolling: {sorted(fetched_far)}"
                        )
                    if not far_at_rest:
                        failures.append("fixture has no object outside the prefetch window")
                    if media_rels[-1] not in far_at_rest:
                        failures.append("fixture did not place the repeated object beyond the prefetch window")
                    ready_by_rel = {item["rel"]: item["ready"] for item in decoded}
                    decoded_far = sorted(rel for rel in far_at_rest if ready_by_rel.get(rel))
                    if decoded_far:
                        failures.append(
                            f"an object outside the prefetch window was decoded before scrolling: {decoded_far}"
                        )
                    if (
                        not geometry
                        or geometry[0]["bottom"] <= fold_top
                        or geometry[0]["top"] >= fold_bottom
                    ):
                        failures.append("the first image is not visible at rest")
                    if len(geometry) < 2:
                        failures.append("fixture has no second image")
                    else:
                        target = geometry[1]
                        if target["top"] <= fold_bottom:
                            failures.append(
                                "fixture did not place the second image below the fold: "
                                f"top={target['top']:.0f} fold={fold_bottom:.0f}"
                            )
                        elif not _in_prefetch_window(target, pane):
                            failures.append("the second image is outside the two-screen prefetch window")
                        elif not decoded[1]["ready"]:
                            failures.append("the below-fold prefetched image was not decoded")
                if any(item["ready"] and not item["width"] for item in decoded):
                    failures.append("a hydrated image has zero naturalWidth")

                page.eval_on_selector(
                    "#reader-pane", "el => { el.scrollTop = el.scrollHeight; }"
                )
                try:
                    page.wait_for_function(
                        "(rel) => { const imgs = [...document.querySelectorAll("
                        "'.article-body img[data-ca-src]')];"
                        " const same = imgs.filter(img => img.getAttribute('data-ca-src') === rel);"
                        " return same.length >= 2 && same.every(img => img.src.startsWith('blob:')"
                        " && img.complete && img.naturalWidth > 0); }",
                        arg=media_rels[-1],
                        timeout=30000,
                    )
                except PlaywrightTimeoutError:
                    failures.append(
                        "scrolling did not hydrate every occurrence of the repeated object"
                    )
                attachment = page.locator(".article-body a", has_text=args.repeat_link_text)
                if attachment.count() != 1:
                    failures.append("the shared attachment link was not found")
                else:
                    href = attachment.first.get_attribute("href") or ""
                    if not href.startswith("blob:"):
                        failures.append("the attachment link was not hydrated to a blob URL")
                    if attachment.first.get_attribute("data-ca-href") is not None:
                        failures.append("the hydrated attachment kept its encrypted marker")
                with media_lock:
                    after_scroll = [dict(event) for event in media_events]
                duplicate_events = [
                    event
                    for event in after_scroll
                    if event.get("phase") == "delayed"
                    and event.get("label") == "media-desktop"
                    and event["path"] == media_rels[-1]
                ]
                if not duplicate_events:
                    failures.append("scrolling did not request the last image")
                elif len(duplicate_events) != 1:
                    failures.append(
                        f"the repeated object was requested {len(duplicate_events)} times"
                    )

                # Stop the desktop scheduler before measuring the mobile page,
                # so its trailing requests cannot be mistaken for mobile work.
                page.close()

                # Mobile: hiding the pane (real 390x844 layout) must pause new
                # downloads, and showing it again must resume the same scheduler
                # without a remount. The fixture keeps a fourth job pending and
                # eligible while the pane hides: the first three responses are
                # held at the server, so concurrency is saturated at the cap and
                # nothing else can start between the barrier and the hide.
                #
                # Mobile discovery pass: learn this article's object set (the
                # desktop fixture uses different objects) so the measured pass
                # can gate and time exactly those requests.
                with media_lock:
                    media_state["label"] = "media-mobile-discovery"
                    media_state["phase"] = "boot"
                    media_state["gate"] = None
                mobile = browser.new_page(viewport={"width": 390, "height": 844})
                mobile.add_init_script(MEDIA_SIZE_SCRIPT)
                mobile.add_init_script(MEDIA_PROBE_SCRIPT)
                mobile.on("pageerror", lambda err: runtime_errors.append(str(err)))
                mobile.goto(base + "/index.html", wait_until="load")
                unlock(mobile)
                expect(mobile.locator(".article-card")).to_have_count(2, timeout=30000)
                mobile.locator(".article-card", has_text=args.mobile_article_title).click()
                expect(mobile.locator("#reader-pane")).to_be_visible(timeout=30000)
                mobile.wait_for_function(
                    "() => document.querySelectorAll('.article-body img[data-ca-src]').length >= 4",
                    timeout=30000,
                )
                mobile_rels = set(
                    mobile.eval_on_selector_all(
                        ".article-body img[data-ca-src], .article-body a[data-ca-href]",
                        "els => els.map(e => e.getAttribute('data-ca-src')"
                        " || e.getAttribute('data-ca-href'))",
                    )
                )
                mobile_id = mobile.evaluate(
                    "() => { const c = document.querySelector('.article-card.active');"
                    " return c ? c.getAttribute('data-id') : null; }"
                )
                if len(mobile_rels) < 4:
                    failures.append(f"mobile fixture has only {len(mobile_rels)} objects")
                if not mobile_id:
                    failures.append("could not resolve the open mobile article id")
                mobile_rel_set = set(mobile_rels)
                mobile_gate = threading.Event()
                with media_lock:
                    media_state["media"] = set(mobile_rels)
                    media_state["phase"] = "delayed"
                    media_state["label"] = "media-mobile"
                    media_state["seq"] = 0
                    media_state["gate"] = mobile_gate
                # Close the discovery page so none of its trailing requests can
                # be mistaken for measured mobile work, then open the measured
                # page directly on the article.
                mobile.close()
                mobile = browser.new_page(viewport={"width": 390, "height": 844})
                mobile.add_init_script(MEDIA_SIZE_SCRIPT)
                mobile.add_init_script(MEDIA_PROBE_SCRIPT)
                mobile.on("pageerror", lambda err: runtime_errors.append(str(err)))
                mobile.goto(
                    base + "/index.html#" + urllib.parse.quote(mobile_id or "", safe=""),
                    wait_until="load",
                )
                unlock(mobile)
                expect(mobile.locator("#reader-pane")).to_be_visible(timeout=30000)
                mobile.wait_for_function(
                    "() => document.querySelectorAll('.article-body img[data-ca-src]').length >= 4",
                    timeout=30000,
                )
                mobile_pane = pane_box(mobile)
                mobile_rects = rect_map(mobile)
                if not wait_for_starts(mobile, "media-mobile", 3, rels=mobile_rel_set):
                    failures.append("the mobile first wave never reached three concurrent requests")
                at_hide_set = {hit["path"] for hit in media_hits("media-mobile", mobile_rel_set)}
                if len(at_hide_set) != 3:
                    failures.append(
                        "the mobile pane started "
                        f"{len(at_hide_set)} media requests before hiding instead of exactly three"
                    )
                mobile.evaluate(
                    "() => { const body = document.querySelector('.article-body');"
                    " body.setAttribute('data-ca-probe-mark', 'kept'); }"
                )
                scroll_before_hide = mobile.evaluate(
                    "() => document.getElementById('reader-pane').scrollTop"
                )
                body_fetches_before = len(
                    [
                        probe
                        for probe in mobile.evaluate("window.__caFetchProbes || []")
                        if not probe["occurrenceCount"]
                    ]
                )
                # Use the real back-button handler, invoked synchronously so the
                # measured "after hide" window starts at the display:none
                # transition rather than after click actionability.
                mobile.evaluate("() => document.getElementById('mobile-back-btn').click()")
                with media_lock:
                    hidden_at = time.monotonic()
                expect(mobile.locator("#reader-pane")).to_be_hidden(timeout=30000)
                hidden_box = mobile.evaluate(
                    "() => { const p = document.getElementById('reader-pane');"
                    " const r = p.getBoundingClientRect();"
                    " return { display: getComputedStyle(p).display,"
                    " width: r.width, height: r.height }; }"
                )
                if (
                    hidden_box["display"] != "none"
                    or hidden_box["width"] > 0
                    or hidden_box["height"] > 0
                ):
                    failures.append(f"the mobile pane was not actually hidden: {hidden_box}")
                # Release the held bytes only after the pane is hidden: their
                # completions must not start the remaining pending job.
                mobile_gate.set()
                if not wait_for_requests_ended("media-mobile", timeout=15, rels=mobile_rel_set):
                    failures.append("the held mobile first wave never completed")
                mobile.wait_for_timeout(600)
                late = {
                    hit["path"]
                    for hit in media_hits("media-mobile", mobile_rel_set)
                    if hit["start"] > hidden_at
                }
                if late:
                    failures.append(
                        f"a hidden mobile pane kept downloading: {len(late)} new objects"
                    )
                # Re-show the existing mount instead of remounting, so this
                # proves the scheduler observes the pane becoming measurable
                # and resumes its one remaining eligible job without any scroll.
                mobile.evaluate(
                    "() => document.getElementById('app-layout').classList.add('mobile-reading')"
                )
                expect(mobile.locator("#reader-pane")).to_be_visible(timeout=30000)
                resumed: list[dict] = []
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    resumed = [
                        hit
                        for hit in media_hits("media-mobile", mobile_rel_set)
                        if hit["start"] > hidden_at
                    ]
                    if resumed:
                        break
                    mobile.wait_for_timeout(100)
                if not resumed:
                    failures.append("re-showing the mobile pane did not resume pending media")
                elif len({hit["path"] for hit in resumed}) != 1:
                    failures.append(
                        "re-showing the mobile pane started "
                        f"{len({hit['path'] for hit in resumed})} new objects instead of one"
                    )
                elif resumed[0]["path"] in at_hide_set:
                    failures.append("the resumed request duplicated an already-started object")
                mobile_after = mobile.evaluate(
                    "() => ({ mark: document.querySelector('.article-body')"
                    ".getAttribute('data-ca-probe-mark'),"
                    " scrollTop: document.getElementById('reader-pane').scrollTop,"
                    " bodyFetches: (window.__caFetchProbes || [])"
                    ".filter(p => !p.occurrenceCount).length })"
                )
                if mobile_after["mark"] != "kept":
                    failures.append("re-showing the mobile pane remounted the article body")
                if mobile_after["bodyFetches"] != body_fetches_before:
                    failures.append("re-showing the mobile pane re-fetched the article body")
                if mobile_after["scrollTop"] != scroll_before_hide:
                    failures.append(
                        "re-showing the mobile pane changed the scroll position: "
                        f"{scroll_before_hide} -> {mobile_after['scrollTop']}"
                    )
                mobile_probes = [
                    probe
                    for probe in mobile.evaluate("window.__caFetchProbes || []")
                    if probe["occurrenceCount"] > 0
                ]
                for probe in mobile_probes:
                    state = _frame_state(probe)
                    if state in ("far", "hidden", "unmeasurable"):
                        failures.append(
                            "the mobile pane issued a media request that the "
                            f"geometry did not justify: {probe['rel'][-12:]} ({state})"
                        )
                # The resumed job must be one that was pending and eligible.
                if resumed:
                    target_rects = mobile_rects.get(resumed[0]["path"], [])
                    if not any(
                        _in_prefetch_window(rect, mobile_pane) for rect in target_rects
                    ):
                        failures.append("the resumed object was not inside the prefetch window")
                try:
                    mobile.wait_for_function(
                        "() => [...document.querySelectorAll('.article-body img[data-ca-src]')]"
                        " .some(img => img.src.startsWith('blob:') && img.complete"
                        " && img.naturalWidth > 0);",
                        timeout=20000,
                    )
                except PlaywrightTimeoutError:
                    failures.append("no mobile image decoded after re-showing the pane")
                mobile_sizes = mobile.evaluate("window.__caMediaSizes || []")
                if any(
                    size["before"] >= 120 and size["after"] < 120 and not size["naturalWidth"]
                    for size in mobile_sizes
                ):
                    failures.append("a mobile pending image collapsed before decoding")
                mobile_peak = _peak_concurrency(media_hits("media-mobile", mobile_rel_set))
                if mobile_peak > 3:
                    failures.append(
                        f"more than three concurrent mobile media requests: {mobile_peak}"
                    )
                mobile_detail = {
                    "pane": mobile_pane,
                    "firstWave": sorted(at_hide_set),
                    "resumed": sorted({hit["path"] for hit in resumed}),
                    "hiddenBox": hidden_box,
                    "peak": mobile_peak,
                    "probes": mobile_probes,
                    "sizes": mobile_sizes,
                }
                mobile.close()
            else:
                # Wrong passphrase must fail closed and keep the overlay.
                page.fill("#ca-passphrase", "wrong horse battery staple")
                page.click("#ca-unlock-submit")
                expect(page.locator("#ca-unlock-error")).to_be_visible(timeout=30000)
                if page.locator(".article-card").count() != 0:
                    failures.append("wrong passphrase rendered cards")
                if TITLE_A in page.locator("body").inner_text():
                    failures.append("wrong passphrase leaked title")

                page.fill("#ca-passphrase", args.passphrase)
                page.click("#ca-unlock-submit")
                expect(page.locator(".article-card")).to_have_count(2, timeout=30000)
                expect(page.locator("#ca-unlock")).to_be_hidden()
                if TITLE_A not in page.locator("body").inner_text():
                    failures.append("correct passphrase did not render titles")

                # Chinese full-text search on the decrypted index.
                page.fill("#search-input", NEEDLE_A)
                page.wait_for_timeout(300)
                if page.locator(".article-card").count() != 1:
                    failures.append("search did not filter to one article")
                page.fill("#search-input", "")
                page.wait_for_timeout(200)

                # Click beta then quickly alpha: the stale beta load must not win.
                page.locator(".article-card", has_text=TITLE_B).click()
                page.locator(".article-card", has_text=TITLE_A).click()
                expect(page.locator(".article-body")).to_contain_text(NEEDLE_A, timeout=30000)
                page.wait_for_timeout(500)
                body_text = page.locator(".article-body").inner_text()
                if NEEDLE_B in body_text:
                    failures.append("stale async body from the previous article was rendered")

                # Normal acceptance requires decoded pixels from a valid PNG;
                # an error placeholder must never make the happy path pass.
                page.wait_for_function(
                    "() => { const i = document.querySelector('.article-body img[data-ca-src]');"
                    " return i && i.src.startsWith('blob:') && i.complete && i.naturalWidth > 0; }",
                    timeout=30000,
                )
                if page.locator(".article-body .img-missing").count():
                    failures.append("a valid image was replaced by an error placeholder")

                storage = page.evaluate(
                    "() => ({local: localStorage.length, session: sessionStorage.length})"
                )
                if storage["local"] or storage["session"]:
                    failures.append(f"browser storage not empty: {storage}")
                fragment = page.evaluate("() => location.hash")
                if TITLE_A in fragment or "2026-01-01" in fragment:
                    failures.append(f"fragment leaks archive slug: {fragment}")

                # Exit while a body load is in flight: must not reappear.
                page.locator(".article-card", has_text=TITLE_A).click()
                page.click("#ca-lock-btn")
                expect(page.locator("#ca-unlock")).to_be_visible()
                page.wait_for_timeout(800)
                after = page.locator("body").inner_text()
                for needle in (TITLE_A, NEEDLE_A):
                    if needle in after:
                        failures.append(f"plaintext survived exit/in-flight load: {needle}")
                if page.locator(".article-card").count() != 0:
                    failures.append("cards survived exit")
                if page.evaluate("() => location.hash") != "":
                    failures.append("fragment survived exit")

                # bfcache/back: pagehide must clear, back must not show plaintext.
                page.fill("#ca-passphrase", args.passphrase)
                page.click("#ca-unlock-submit")
                expect(page.locator(".article-card")).to_have_count(2, timeout=30000)
                page.goto(base + "/site.json", wait_until="load")
                page.go_back(wait_until="load")
                expect(page.locator("#ca-unlock")).to_be_visible(timeout=30000)
                page.wait_for_timeout(300)
                back_text = page.locator("body").inner_text()
                for needle in (TITLE_A, NEEDLE_A):
                    if needle in back_text:
                        failures.append(f"bfcache/back restored plaintext: {needle}")
                if page.locator(".article-card").count() != 0:
                    failures.append("bfcache/back restored cards")

                # Mobile layout: unlock, open an article, return to the list.
                mobile = browser.new_page(viewport={"width": 390, "height": 844})
                mobile.goto(base + "/index.html", wait_until="load")
                mobile.fill("#ca-passphrase", args.passphrase)
                mobile.click("#ca-unlock-submit")
                expect(mobile.locator(".article-card")).to_have_count(2, timeout=30000)
                mobile.locator(".article-card", has_text=TITLE_A).click()
                expect(mobile.locator("#reader-pane")).to_be_visible(timeout=30000)
                if "mobile-reading" not in (mobile.locator("#app-layout").get_attribute("class") or ""):
                    failures.append("mobile article view did not switch")
                mobile.click("#mobile-back-btn")
                if "mobile-reading" in (mobile.locator("#app-layout").get_attribute("class") or ""):
                    failures.append("mobile back button did not return to the list")
                mobile.close()

            browser.close()
    finally:
        server.shutdown()
        server.server_close()

    origin = base
    for url in requests:
        if url.startswith("blob:"):
            if not url.startswith("blob:" + origin + "/"):
                failures.append(f"foreign blob URL: {url}")
            continue
        if not url.startswith(origin):
            failures.append(f"non-same-origin request: {url}")
        if ".js" not in url and "site.json" not in url and "objects/" not in url and not url.endswith("/index.html"):
            failures.append(f"unexpected request: {url}")
    for message in console_errors:
        if "Content Security Policy" in message or "CSP" in message:
            failures.append(f"CSP violation: {message}")
    for message in runtime_errors:
        failures.append(f"page error: {message}")

    print(
        json.dumps(
            {
                "ok": not failures,
                "failures": failures,
                "media_events": media_events if args.mode == "media" else [],
                "media_detail": (
                    {
                        "rels": media_rels,
                        "requested": sorted(requested),
                        "pane": pane,
                        "geometry": geometry,
                        "decoded": decoded,
                        "probes": media_probes,
                        "mobile": mobile_detail,
                    }
                    if args.mode == "media"
                    else {}
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
