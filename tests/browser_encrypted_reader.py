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

from playwright.sync_api import sync_playwright, expect

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
    media_state = {"phase": "boot", "seq": 0, "media": set(), "label": "boot"}
    media_lock = threading.Lock()
    media_rels: list[str] = []
    requested: set[str] = set()
    pane = None
    geometry: list[dict] = []
    decoded: list[dict] = []
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
                        with media_lock:
                            media_state["media"] = set(media_rels)
                            media_state["phase"] = "delayed"
                            media_state["label"] = "media-desktop"
                            media_state["seq"] = 0
                        page.fill("#ca-passphrase", args.passphrase)
                        page.click("#ca-unlock-submit")
                        page.wait_for_function(
                            "() => document.querySelectorAll('.article-body img[data-ca-src]').length >= 6",
                            timeout=30000,
                        )
                pane = page.evaluate(
                    "() => { const r = document.getElementById('reader-pane')"
                    ".getBoundingClientRect(); return { x: r.x, y: r.y,"
                    " width: r.width, height: r.height }; }"
                )
                img_refs = page.eval_on_selector_all(
                    ".article-body img[data-ca-src]",
                    "els => els.map(e => e.getAttribute('data-ca-src'))",
                )
                geometry = page.eval_on_selector_all(
                    ".article-body img[data-ca-src]",
                    "els => els.map(e => { const r = e.getBoundingClientRect();"
                    " return { top: r.top, bottom: r.bottom }; })",
                )
                # Wait for real decoded pixels, not merely a blob URL.
                page.wait_for_function(
                    "() => { const imgs = [...document.querySelectorAll("
                    "'.article-body img[data-ca-src]')];"
                    " return imgs.filter(i => i.src.startsWith('blob:')"
                    " && i.complete && i.naturalWidth > 0).length >= 2; }",
                    timeout=20000,
                )
                # Let the deliberately slow first image finish so its request
                # interval is recorded; a serial implementation still shows the
                # second start after the first end.
                page.wait_for_timeout(args.slow_first_ms + 500)
                decoded = page.eval_on_selector_all(
                    ".article-body img[data-ca-src]",
                    "els => els.map(e => ({ rel: e.getAttribute('data-ca-src'),"
                    " src: e.getAttribute('src'), ready: e.complete && e.naturalWidth > 0,"
                    " width: e.naturalWidth }))",
                )
                final_geometry = page.eval_on_selector_all(
                    ".article-body img[data-ca-src]",
                    "els => els.map(e => { const r = e.getBoundingClientRect();"
                    " return { top: r.top, bottom: r.bottom }; })",
                )
                media_sizes = page.evaluate("window.__caMediaSizes || []")
                with media_lock:
                    snapshot_time = time.monotonic()
                    snapshot = [dict(event) for event in media_events]
                rel_set = set(media_rels)
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
                fold_top = pane["y"] if pane else 0
                fold_bottom = pane["y"] + pane["height"] if pane else 0
                if len(hits) < 2:
                    failures.append(f"expected several media requests, saw {len(hits)}")
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
                    points = []
                    for event in hits:
                        points.append((event["start"], 1))
                        points.append((event["end"], -1))
                    points.sort(key=lambda point: (point[0], point[1]))
                    peak = 0
                    current = 0
                    for _, delta in points:
                        current += delta
                        peak = max(peak, current)
                    if peak > 3:
                        failures.append(f"more than three concurrent media requests: {peak}")
                    if peak < 2:
                        failures.append(f"media requests were effectively serial: peak {peak}")
                    # The image intersecting the pane belongs to the first wave.
                    visible = [
                        rel
                        for rel, rect in zip(img_refs, geometry)
                        if rect["bottom"] >= fold_top and rect["top"] <= fold_bottom
                    ]
                    earliest = min(event["start"] for event in hits)
                    first_wave = {
                        event["path"]
                        for event in hits
                        if event["start"] <= earliest + 0.15 and event.get("phase") == "delayed"
                    }
                    if visible and not set(visible) <= first_wave:
                        failures.append(
                            "the visible image was not in the first request wave: "
                            f"visible={len(visible)} first={len(first_wave & set(visible))}"
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
                # Distant objects (outside the two-screen prefetch window) must
                # not be started before scrolling. Duplicate occurrences are
                # ranked by their best node, not by a spanning bounding box.
                if not pane:
                    failures.append("reader pane was not measurable")
                else:
                    window_top = fold_top - 2 * pane["height"]
                    window_bottom = fold_bottom + 2 * pane["height"]
                    near = {
                        rel
                        for rel, rect in zip(img_refs, geometry)
                        if rect["bottom"] >= window_top and rect["top"] <= window_bottom
                    }
                    far = set(media_rels) - near
                    if requested & far:
                        failures.append(
                            f"far objects were fetched before scrolling: {sorted(requested & far)}"
                        )
                    if media_rels[-1] not in far:
                        failures.append("fixture did not place the repeated object beyond the prefetch window")
                    if (
                        not final_geometry
                        or final_geometry[0]["bottom"] <= fold_top
                        or final_geometry[0]["top"] >= fold_bottom
                    ):
                        failures.append("the first image is not visible at rest")
                    if len(final_geometry) < 2:
                        failures.append("fixture has no second image")
                    else:
                        target = final_geometry[1]
                        if target["top"] <= fold_bottom:
                            failures.append(
                                "fixture did not place the second image below the fold: "
                                f"top={target['top']:.0f} fold={fold_bottom:.0f}"
                            )
                        elif target["top"] > fold_bottom + 2 * pane["height"]:
                            failures.append("the second image is outside the two-screen prefetch window")
                        elif not decoded[1]["ready"]:
                            failures.append("the below-fold prefetched image was not decoded")
                if any(item["ready"] and not item["width"] for item in decoded):
                    failures.append("a hydrated image has zero naturalWidth")
                if decoded and decoded[-1]["ready"]:
                    failures.append("a far image was decoded before scrolling")

                page.eval_on_selector(
                    "#reader-pane", "el => { el.scrollTop = el.scrollHeight; }"
                )
                page.wait_for_function(
                    "(rel) => { const imgs = [...document.querySelectorAll("
                    "'.article-body img[data-ca-src]')];"
                    " const same = imgs.filter(img => img.getAttribute('data-ca-src') === rel);"
                    " return same.length >= 2 && same.every(img => img.src.startsWith('blob:')"
                    " && img.complete && img.naturalWidth > 0); }",
                    arg=media_rels[-1],
                    timeout=30000,
                )
                attachment = page.locator(".article-body a", has_text="重复图4附件")
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
                # downloads, and showing it again must resume the same
                # scheduler without a remount.
                with media_lock:
                    media_state["label"] = "media-mobile"
                    media_state["seq"] = 0
                mobile = browser.new_page(viewport={"width": 390, "height": 844})
                mobile.add_init_script(MEDIA_SIZE_SCRIPT)
                mobile.on("pageerror", lambda err: runtime_errors.append(str(err)))
                mobile.goto(base + "/index.html", wait_until="load")
                mobile.fill("#ca-passphrase", args.passphrase)
                mobile.click("#ca-unlock-submit")
                expect(mobile.locator(".article-card")).to_have_count(2, timeout=30000)
                mobile.locator(".article-card", has_text=TITLE_A).click()
                expect(mobile.locator("#reader-pane")).to_be_visible(timeout=30000)
                mobile.wait_for_function(
                    "() => document.querySelectorAll('.article-body img[data-ca-src]').length >= 6",
                    timeout=30000,
                )
                mobile.wait_for_timeout(80)
                with media_lock:
                    at_hide = {
                        event["path"] for event in media_events
                        if event.get("label") == "media-mobile"
                    }
                if not at_hide:
                    failures.append("the mobile pane never started a media request")
                # Use the real back-button handler, but invoke it synchronously
                # so the measured "after hide" window starts at the actual
                # display:none transition rather than after click actionability.
                mobile.evaluate("() => document.getElementById('mobile-back-btn').click()")
                with media_lock:
                    hidden_at = time.monotonic()
                expect(mobile.locator("#reader-pane")).to_be_hidden(timeout=30000)
                mobile.wait_for_timeout(2200)
                with media_lock:
                    late = {
                        event["path"]
                        for event in media_events
                        if event.get("label") == "media-mobile"
                        and event["start"] > hidden_at
                    }
                if late:
                    failures.append(f"a hidden mobile pane kept downloading: {len(late)} new objects")
                # Re-show the existing mount instead of remounting, so this
                # proves the scheduler observes the pane becoming measurable
                # and resumes its pending queue.
                mobile.evaluate(
                    "() => document.getElementById('app-layout').classList.add('mobile-reading')"
                )
                expect(mobile.locator("#reader-pane")).to_be_visible(timeout=30000)
                resumed = False
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    with media_lock:
                        after_show = {
                            event["path"]
                            for event in media_events
                            if event.get("label") == "media-mobile"
                            and event["start"] > hidden_at
                        }
                    if after_show:
                        resumed = True
                        break
                    mobile.wait_for_timeout(100)
                if not resumed:
                    failures.append("re-showing the mobile pane did not resume pending media")
                mobile.wait_for_function(
                    "() => [...document.querySelectorAll('.article-body img[data-ca-src]')]"
                    " .some(img => img.src.startsWith('blob:') && img.complete"
                    " && img.naturalWidth > 0);",
                    timeout=20000,
                )
                mobile_sizes = mobile.evaluate("window.__caMediaSizes || []")
                if any(
                    size["before"] >= 120 and size["after"] < 120 and not size["naturalWidth"]
                    for size in mobile_sizes
                ):
                    failures.append("a mobile pending image collapsed before decoding")
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
