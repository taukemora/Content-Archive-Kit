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
import socket
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

TITLE_A = "加密标题甲"
TITLE_B = "加密标题乙"
NEEDLE_A = "ALPHANEEDLE"
NEEDLE_B = "BETANEEDLE"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401 - silence test server
        pass


def serve(directory: Path):
    handler = functools.partial(QuietHandler, directory=str(directory))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True)
    parser.add_argument("--passphrase", required=True)
    parser.add_argument(
        "--mode",
        choices=("normal", "tamper", "tamper-site", "tamper-article", "tamper-image"),
        default="normal",
    )
    parser.add_argument("--tamper-rel", default="")
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

    server, base = serve(site_dir)
    failures: list[str] = []
    requests: list[str] = []
    console_errors: list[str] = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on("request", lambda req: requests.append(req.url))
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

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
            elif args.mode in ("tamper-article", "tamper-image"):
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
                        failures.append("image tamper broke the rest of the article")
                    if page.locator(".article-body img[data-ca-src]").count():
                        src = page.locator(".article-body img[data-ca-src]").first.get_attribute("src")
                        if src and src.startswith("blob:"):
                            failures.append("tampered image was hydrated")
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

                # Image must be a decrypted same-origin blob, never library/.
                page.wait_for_function(
                    "() => { const i = document.querySelector('.article-body img[data-ca-src]');"
                    " return i && i.src.startsWith('blob:'); }",
                    timeout=30000,
                )
                img_src = page.locator(".article-body img[data-ca-src]").first.get_attribute("src")
                if not img_src or not img_src.startswith("blob:"):
                    failures.append("image was not hydrated to a blob URL")

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

    print(json.dumps({"ok": not failures, "failures": failures}, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
