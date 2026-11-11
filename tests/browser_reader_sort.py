#!/usr/bin/env python3
"""Real-Chromium acceptance probe for the reader's archived/published sort.

Invoked by ``tests/test_reader_sort.py`` with an isolated Playwright
interpreter. Everything is synthetic: the probe serves a scratch directory,
uses a generated test key/passphrase, and never touches production data.

  python tests/browser_reader_sort.py --dir <site> --mode offline|encrypted \
      --fixture <fixture.json> [--passphrase ...] [--browser <chromium>]
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
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

STORAGE_KEY = "ca.reader.sort"


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
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


DISABLE_STORAGE = """
(() => {
  try {
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get() { throw new Error('storage disabled by test'); },
    });
  } catch (e) {
    try {
      Object.defineProperty(Storage.prototype, 'getItem', {
        configurable: true,
        value() { throw new Error('storage disabled by test'); },
      });
      Object.defineProperty(Storage.prototype, 'setItem', {
        configurable: true,
        value() { throw new Error('storage disabled by test'); },
      });
    } catch (e2) { /* reported below */ }
  }
})();
"""


def card_titles(page) -> list[str]:
    return page.eval_on_selector_all(
        ".article-card .card-title", "els => els.map(e => e.textContent.trim())"
    )


def active_title(page):
    if page.locator(".article-card.active").count() == 0:
        return None
    return page.locator(".article-card.active .card-title").inner_text().strip()


def sort_state(page) -> dict:
    return page.evaluate(
        """() => ({
            archived: document.getElementById('sort-archived').getAttribute('aria-pressed'),
            published: document.getElementById('sort-published').getAttribute('aria-pressed'),
            hint: (document.getElementById('browse-sort-hint') || {}).textContent || '',
            listScroll: document.getElementById('article-list').scrollTop,
            readerScroll: document.getElementById('reader-pane').scrollTop,
            stored: (() => {
                try { return window.localStorage.getItem('ca.reader.sort'); }
                catch (e) { return '<storage-error>'; }
            })(),
            keys: (() => {
                try {
                    return Object.keys(window.localStorage).sort();
                } catch (e) { return ['<storage-error>']; }
            })(),
        })"""
    )


def wait_for_cards(page, count: int):
    page.wait_for_function(
        "(n) => document.querySelectorAll('.article-card').length === n", arg=count, timeout=30000
    )


def unlock(page, count: int, passphrase: str):
    page.fill("#ca-passphrase", passphrase)
    page.click("#ca-unlock-submit")
    wait_for_cards(page, count)
    expect(page.locator("#ca-unlock")).to_be_hidden(timeout=30000)


def check_offline(page, fixture, failures):
    wait_for_cards(page, len(fixture["archivedTitles"]))
    order = card_titles(page)
    if order != fixture["archivedTitles"]:
        failures.append(f"default archived order wrong: {order}")

    state = sort_state(page)
    if state["archived"] != "true" or state["published"] != "false":
        failures.append(f"default aria-pressed wrong: {state}")
    if "最近归档" not in state["hint"]:
        failures.append(f"sort hint does not describe archived mode: {state['hint']!r}")
    heights = page.eval_on_selector_all(
        "#sort-archived, #sort-published",
        "els => els.map(e => parseFloat(getComputedStyle(e).minHeight) || 0)",
    )
    if any(height < 44 for height in heights):
        failures.append(f"sort controls are below the 44px touch target: {heights}")
    group = page.locator("[role=group][aria-label='排序方式']")
    if group.count() != 1:
        failures.append("sort control group is missing")
    filter_box = page.locator("#category-filter").bounding_box()
    sort_box = page.locator("#sort-archived").bounding_box()
    if not filter_box or not sort_box or sort_box["y"] < filter_box["y"] + filter_box["height"] - 2:
        failures.append("sort control is not placed below the list filters")

    # Unknown and invalid publication dates are shown honestly.
    for entry_id in ("CC", "LL"):
        card = page.locator(".article-card", has_text=f"排序文章 {entry_id}")
        if card.count() != 1:
            failures.append(f"card for 排序文章 {entry_id} missing")
        elif "日期未知" not in card.inner_text():
            failures.append(f"unknown date not shown for 排序文章 {entry_id}")
    jj_card = page.locator(".article-card", has_text="排序文章 JJ")
    if jj_card.count() != 1 or "发布 2026-01-09" not in jj_card.inner_text():
        failures.append("card date is not the source-local date-only value")

    # Keyboard activation of the native button.
    page.locator("#sort-published").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#sort-published")).to_have_attribute("aria-pressed", "true")
    if card_titles(page) != fixture["publishedTitles"]:
        failures.append(f"published order after keyboard toggle wrong: {card_titles(page)}")
    state = sort_state(page)
    if state["published"] != "true" or state["archived"] != "false":
        failures.append(f"aria-pressed not updated after toggle: {state}")
    if state["stored"] != "published":
        failures.append(f"preference not stored after toggle: {state['stored']!r}")
    if state["keys"] != [STORAGE_KEY]:
        failures.append(f"unexpected stored keys: {state['keys']}")

    # Preference survives a reload.
    page.reload(wait_until="load")
    wait_for_cards(page, len(fixture["publishedTitles"]))
    if sort_state(page)["published"] != "true":
        failures.append("published preference was not restored after reload")
    if card_titles(page) != fixture["publishedTitles"]:
        failures.append("restored published order wrong after reload")

    # Search and filters stay untouched by a sort-only action.
    page.fill("#search-input", fixture["searchNeedle"])
    wait_for_cards(page, 1)
    page.select_option("#category-filter", "tech")
    wait_for_cards(page, 1)
    before = {
        "query": page.input_value("#search-input"),
        "category": page.input_value("#category-filter"),
        "reset": page.locator("#reset-filters-btn").is_visible(),
        "titles": card_titles(page),
    }
    page.click("#sort-archived")
    page.wait_for_timeout(120)
    after = {
        "query": page.input_value("#search-input"),
        "category": page.input_value("#category-filter"),
        "reset": page.locator("#reset-filters-btn").is_visible(),
        "titles": card_titles(page),
    }
    if before != after:
        failures.append(f"sort changed filter/search state: before={before} after={after}")
    if before["titles"] != [fixture["searchTitle"]]:
        failures.append(f"filtered result wrong: {before['titles']}")
    page.click("#reset-filters-btn")
    wait_for_cards(page, len(fixture["archivedTitles"]))

    # Selection, body and scroll survive a sort-only action.
    if sort_state(page)["published"] != "true":
        page.click("#sort-published")
    page.eval_on_selector("#article-list", "el => { el.scrollTop = 160; }")
    page.locator(".article-card", has_text=fixture["selectTitle"]).click()
    expect(page.locator(".article-card.active")).to_have_count(1)
    if active_title(page) != fixture["selectTitle"]:
        failures.append(f"selection did not open {fixture['selectTitle']}: {active_title(page)}")
    page.eval_on_selector(
        "#reader-pane", "el => { el.scrollTop = Math.min(240, el.scrollHeight - el.clientHeight - 1); }"
    )
    marks = page.evaluate(
        """() => {
            const body = document.querySelector('#reader-content .article-body');
            if (!body) return { ok: false };
            body.setAttribute('data-test-mark', 'offline-body');
            return { ok: true, scroll: document.getElementById('reader-pane').scrollTop };
        }"""
    )
    if not marks["ok"] or marks["scroll"] <= 0:
        failures.append(f"reader body/scroll setup failed: {marks}")
    before_scroll = sort_state(page)
    page.wait_for_timeout(100)
    page.click("#sort-archived")
    page.wait_for_timeout(180)
    after_scroll = sort_state(page)
    if active_title(page) != fixture["selectTitle"]:
        failures.append(f"sort changed the selected article: {active_title(page)}")
    if not page.evaluate(
        "() => !!document.querySelector('#reader-content .article-body[data-test-mark=\"offline-body\"]')"
    ):
        failures.append("sort re-rendered (remounted) the reader body")
    if abs(after_scroll["listScroll"] - before_scroll["listScroll"]) > 2:
        failures.append(
            f"list scroll lost on sort: {before_scroll['listScroll']} -> {after_scroll['listScroll']}"
        )
    if abs(after_scroll["readerScroll"] - before_scroll["readerScroll"]) > 2:
        failures.append(
            f"reader scroll lost on sort: {before_scroll['readerScroll']} -> {after_scroll['readerScroll']}"
        )
    if card_titles(page) != fixture["archivedTitles"]:
        failures.append(f"archived order after toggle wrong: {card_titles(page)}")
    if after_scroll["stored"] != "archived":
        failures.append(f"archived preference not stored: {after_scroll['stored']!r}")

    # A corrupt stored value is ignored instead of breaking the list.
    page.evaluate(f"() => window.localStorage.setItem('{STORAGE_KEY}', 'PUBLISHED')")
    page.reload(wait_until="load")
    wait_for_cards(page, len(fixture["archivedTitles"]))
    if sort_state(page)["archived"] != "true":
        failures.append("malformed preference was accepted")
    if card_titles(page) != fixture["archivedTitles"]:
        failures.append(f"order wrong after malformed preference: {card_titles(page)}")

    # Storage that throws (private mode / disabled site data) must not break sorting.
    context = page.context.browser.new_context(viewport={"width": 1280, "height": 900})
    blocked = context.new_page()
    blocked.add_init_script(DISABLE_STORAGE)
    blocked.goto(page.url.rsplit("/", 1)[0] + "/index.html", wait_until="load")
    wait_for_cards(blocked, len(fixture["archivedTitles"]))
    disabled = blocked.evaluate(
        "() => { try { window.localStorage; return false; } catch (e) { return true; } }"
    )
    if not disabled:
        failures.append("probe could not disable localStorage")
    if card_titles(blocked) != fixture["archivedTitles"]:
        failures.append("disabled storage changed the default order")
    blocked.click("#sort-published")
    blocked.wait_for_timeout(120)
    if card_titles(blocked) != fixture["publishedTitles"]:
        failures.append(f"disabled storage blocked in-memory sorting: {card_titles(blocked)}")
    if blocked.evaluate(
        "() => (document.getElementById('sort-published').getAttribute('aria-pressed'))"
    ) != "true":
        failures.append("disabled storage left aria-pressed stale")
    context.close()


def check_encrypted(page, fixture, passphrase, failures, requests):
    unlock(page, len(fixture["archivedTitles"]), passphrase)
    if card_titles(page) != fixture["archivedTitles"]:
        failures.append(f"encrypted archived order wrong: {card_titles(page)}")
    if sort_state(page)["archived"] != "true":
        failures.append("encrypted default sort is not archived")

    page.click("#sort-published")
    page.wait_for_timeout(150)
    if card_titles(page) != fixture["publishedTitles"]:
        failures.append(f"encrypted published order wrong: {card_titles(page)}")
    if sort_state(page)["stored"] != "published":
        failures.append("encrypted mode did not store the sort preference")

    # Open an article with real encrypted media, then prove a sort-only action
    # neither remounts the body nor refetches ciphertext.
    card = page.locator(".article-card", has_text=fixture["mediaTitle"])
    if card.count() != 1:
        failures.append(f"encrypted media article missing: {fixture['mediaTitle']}")
        return
    card.click()
    page.wait_for_function(
        """() => { const i = document.querySelector('#reader-content .article-body img');\
 return !!i && i.src.startsWith('blob:') && i.complete && i.naturalWidth > 0; }""",
        timeout=30000,
    )
    page.wait_for_timeout(400)
    object_requests_before = len([u for u in requests if "/objects/" in u])
    page.evaluate(
        """() => {
            const body = document.querySelector('#reader-content .article-body');
            body.setAttribute('data-test-mark', 'encrypted-body');
            const img = body.querySelector('img');
            img.setAttribute('data-test-img', 'keep');
            window.__caSortedImgSrc = img.getAttribute('src');
        }"""
    )
    page.eval_on_selector("#article-list", "el => { el.scrollTop = 120; }")
    page.eval_on_selector(
        "#reader-pane", "el => { el.scrollTop = Math.min(180, el.scrollHeight - el.clientHeight - 1); }"
    )
    before = sort_state(page)
    page.click("#sort-archived")
    page.wait_for_timeout(500)
    after = sort_state(page)
    if card_titles(page) != fixture["archivedTitles"]:
        failures.append(f"encrypted archived order wrong after report: {card_titles(page)}")
    if active_title(page) != fixture["mediaTitle"]:
        failures.append(f"encrypted sort changed the selected article: {active_title(page)}")
    if not page.evaluate(
        "() => !!document.querySelector('#reader-content .article-body[data-test-mark=\"encrypted-body\"]')"
    ):
        failures.append("encrypted sort remounted the reader body")
    if not page.evaluate(
        "() => !!document.querySelector('#reader-content .article-body img[data-test-img=\"keep\"]')"
    ):
        failures.append("encrypted sort replaced the hydrated media element")
    if not page.evaluate(
        """() => { const img = document.querySelector('#reader-content .article-body img');\
 return !!img && img.getAttribute('src') === window.__caSortedImgSrc && img.src.startsWith('blob:'); }"""
    ):
        failures.append("encrypted sort lost the decoded blob image")
    object_requests_after = len([u for u in requests if "/objects/" in u])
    if object_requests_after != object_requests_before:
        failures.append(
            "sort-only action refetched encrypted objects: "
            f"{object_requests_before} -> {object_requests_after}"
        )
    if abs(after["listScroll"] - before["listScroll"]) > 2:
        failures.append(f"encrypted list scroll lost on sort: {before['listScroll']} -> {after['listScroll']}")
    if abs(after["readerScroll"] - before["readerScroll"]) > 2:
        failures.append(
            f"encrypted reader scroll lost on sort: {before['readerScroll']} -> {after['readerScroll']}"
        )
    if after["keys"] != [STORAGE_KEY]:
        failures.append(f"encrypted mode stored unexpected keys: {after['keys']}")
    if after["stored"] != "archived":
        failures.append(f"encrypted archived preference not stored: {after['stored']!r}")
    stored_values = page.evaluate(
        "() => Object.keys(window.localStorage).map(k => window.localStorage.getItem(k))"
    )
    if any(passphrase in str(value) for value in stored_values):
        failures.append("passphrase was written to browser storage")

    # Preference and order survive a lock/reload cycle in the same profile.
    page.reload(wait_until="load")
    unlock(page, len(fixture["archivedTitles"]), passphrase)
    if sort_state(page)["archived"] != "true":
        failures.append("encrypted archived preference was not restored after reload")
    if card_titles(page) != fixture["archivedTitles"]:
        failures.append("encrypted restored order wrong after reload")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True)
    parser.add_argument("--mode", choices=("offline", "legacy", "encrypted"), required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--passphrase", default="")
    parser.add_argument("--browser", default=os.environ.get("CA_PLAYWRIGHT_BROWSER", ""))
    args = parser.parse_args()

    fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    directory = Path(args.dir).resolve()
    server, base = serve(directory)
    failures: list[str] = []
    runtime_errors: list[str] = []
    console_errors: list[str] = []
    requests: list[str] = []

    try:
        with sync_playwright() as pw:
            launch_options = {"headless": True}
            if args.browser:
                launch_options["executable_path"] = args.browser
            browser = pw.chromium.launch(**launch_options)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on("request", lambda req: requests.append(req.url))
            page.on("pageerror", lambda err: runtime_errors.append(str(err)))
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
            )
            page.goto(base + "/" + ("legacy.html" if args.mode == "legacy" else "index.html"),
                      wait_until="load")
            if args.mode in ("offline", "legacy"):
                check_offline(page, fixture, failures)
            else:
                check_encrypted(page, fixture, args.passphrase, failures, requests)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()

    for message in console_errors:
        failures.append(f"console error: {message}")
    for message in runtime_errors:
        failures.append(f"page error: {message}")
    for url in requests:
        if url.startswith(base):
            continue
        if url.startswith("blob:") or url.startswith("data:"):
            continue
        failures.append(f"unexpected request: {url}")

    print(json.dumps({"ok": not failures, "failures": failures}, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
