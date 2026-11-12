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
            value: (document.getElementById('sort-select') || {}).value || '',
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


SORT_OPTIONS = [["archived", "最近归档优先"], ["published", "最近发布优先"]]


def set_sort(page, value: str) -> None:
    page.select_option("#sort-select", value)
    page.wait_for_timeout(150)


def check_layout(page, failures):
    """Heading/select fit at phone widths and a narrow desktop sidebar."""
    original = page.viewport_size
    for width in (320, 390, 1280):
        page.set_viewport_size({"width": width, "height": 900})
        page.wait_for_timeout(120)
        metrics = page.evaluate(
            """() => {
                const box = (el) => {
                    const r = el.getBoundingClientRect();
                    return { left: r.left, right: r.right, top: r.top, bottom: r.bottom,
                             width: r.width, height: r.height };
                };
                const h2 = document.querySelector('.browse-heading h2');
                const select = document.getElementById('sort-select');
                const controls = document.querySelector('.sidebar-controls');
                const search = document.getElementById('search-input');
                const style = getComputedStyle(select);
                return {
                    heading: box(h2),
                    select: box(select),
                    controls: box(controls),
                    search: box(search),
                    docScroll: document.documentElement.scrollWidth,
                    bodyScroll: document.body.scrollWidth,
                    minHeight: parseFloat(style.minHeight) || 0,
                    backgroundColor: style.backgroundColor,
                    fontWeight: style.fontWeight,
                    fontSize: parseFloat(style.fontSize) || 0,
                    headingFontSize: parseFloat(getComputedStyle(h2).fontSize) || 0,
                    borderWidth: parseFloat(style.borderTopWidth) || 0,
                    borderColor: style.borderTopColor,
                    backgroundImage: style.backgroundImage,
                };
            }"""
        )
        tag = f"at {width}px"
        if metrics["select"]["height"] < 44 or metrics["minHeight"] < 44:
            failures.append(
                f"sort select is below the 44px pointer target {tag}: "
                f"{metrics['select']['height']} / {metrics['minHeight']}"
            )
        if metrics["select"]["left"] < metrics["heading"]["right"] - 0.5:
            failures.append(f"sort select overlaps the heading {tag}: {metrics}")
        if metrics["select"]["right"] > metrics["controls"]["right"] - 20:
            failures.append(f"sort select overflows the sidebar controls {tag}: {metrics}")
        if metrics["select"]["left"] < metrics["controls"]["left"] + 20:
            failures.append(f"sort select starts outside the sidebar padding {tag}: {metrics}")
        if metrics["docScroll"] > width or metrics["bodyScroll"] > width:
            failures.append(
                f"horizontal overflow {tag}: doc={metrics['docScroll']} body={metrics['bodyScroll']}"
            )
        if metrics["select"]["bottom"] > metrics["search"]["top"] - 6:
            failures.append(f"sort select crowds the search box {tag}: {metrics}")
        if metrics["fontSize"] >= metrics["headingFontSize"]:
            failures.append(
                f"sort select is not subordinate to the heading {tag}: "
                f"{metrics['fontSize']} vs {metrics['headingFontSize']}"
            )
        if metrics["fontWeight"] not in ("400", "normal"):
            failures.append(f"sort select is not regular weight {tag}: {metrics['fontWeight']}")
        if metrics["backgroundColor"] not in ("rgba(0, 0, 0, 0)", "transparent"):
            failures.append(f"sort select has a filled background {tag}: {metrics['backgroundColor']}")
        if metrics["borderWidth"] != 1 or metrics["borderColor"] == "rgb(53, 93, 70)":
            failures.append(
                f"sort select border is not weak {tag}: "
                f"{metrics['borderWidth']}px {metrics['borderColor']}"
            )
        if "svg" not in metrics["backgroundImage"]:
            failures.append(f"sort select chevron missing {tag}: {metrics['backgroundImage']}")
    page.set_viewport_size(original)


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

    options = page.eval_on_selector_all(
        "#sort-select option", "els => els.map(e => [e.value, e.textContent.trim()])"
    )
    if options != SORT_OPTIONS:
        failures.append(f"sort select options wrong: {options}")
    legacy_nodes = page.locator(
        "#browse-sort-hint, .sort-row, .sort-btn, #sort-archived, #sort-published"
    )
    if legacy_nodes.count() != 0:
        failures.append("legacy sort hint/buttons are still present")
    check_layout(page, failures)

    state = sort_state(page)
    if state["value"] != "archived":
        failures.append(f"default sort select value wrong: {state}")

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

    # Keyboard focus and native select activation (no custom popup).
    page.evaluate("() => { if (document.activeElement) document.activeElement.blur(); }")
    page.keyboard.press("Tab")
    focused = page.evaluate("() => (document.activeElement ? document.activeElement.id : '')")
    if focused != "sort-select":
        failures.append(f"Tab did not focus the sort select: {focused!r}")
    focus_state = page.evaluate(
        """() => {
            const el = document.activeElement;
            const style = getComputedStyle(el);
            return {
                visible: el.matches(':focus-visible'),
                outlineStyle: style.outlineStyle,
                outlineWidth: style.outlineWidth,
            };
        }"""
    )
    if not focus_state["visible"] or focus_state["outlineStyle"] != "solid":
        failures.append(f"sort select keyboard focus is not visible: {focus_state}")
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(200)
    if page.input_value("#sort-select") != "published":
        page.keyboard.press("Enter")
        page.wait_for_timeout(200)
    if page.input_value("#sort-select") != "published":
        failures.append("keyboard selection did not choose published")
    if card_titles(page) != fixture["publishedTitles"]:
        failures.append(f"published order after keyboard toggle wrong: {card_titles(page)}")
    state = sort_state(page)
    if state["value"] != "published":
        failures.append(f"select value not updated after toggle: {state}")
    if state["stored"] != "published":
        failures.append(f"preference not stored after toggle: {state['stored']!r}")
    if state["keys"] != [STORAGE_KEY]:
        failures.append(f"unexpected stored keys: {state['keys']}")

    # Preference survives a reload.
    page.reload(wait_until="load")
    wait_for_cards(page, len(fixture["publishedTitles"]))
    if sort_state(page)["value"] != "published":
        failures.append("published preference was not restored after reload")
    if card_titles(page) != fixture["publishedTitles"]:
        failures.append("restored published order wrong after reload")

    # Search and filters stay untouched by a sort-only action.
    page.fill("#search-input", fixture["searchNeedle"])
    wait_for_cards(page, 1)
    page.select_option("#category-filter", "tech")
    wait_for_cards(page, 1)
    page.select_option("#source-filter", "合成发布方乙")
    wait_for_cards(page, 1)
    page.click("#tab-auto")
    wait_for_cards(page, 1)
    before = {
        "query": page.input_value("#search-input"),
        "category": page.input_value("#category-filter"),
        "source": page.input_value("#source-filter"),
        "tab": page.locator("#tab-auto").get_attribute("aria-selected"),
        "reset": page.locator("#reset-filters-btn").is_visible(),
        "titles": card_titles(page),
    }
    set_sort(page, "archived")
    after = {
        "query": page.input_value("#search-input"),
        "category": page.input_value("#category-filter"),
        "source": page.input_value("#source-filter"),
        "tab": page.locator("#tab-auto").get_attribute("aria-selected"),
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
    if sort_state(page)["value"] != "published":
        set_sort(page, "published")
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
    set_sort(page, "archived")
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
    if sort_state(page)["value"] != "archived":
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
    set_sort(blocked, "published")
    if card_titles(blocked) != fixture["publishedTitles"]:
        failures.append(f"disabled storage blocked in-memory sorting: {card_titles(blocked)}")
    if blocked.input_value("#sort-select") != "published":
        failures.append("disabled storage left the sort select stale")
    context.close()


def check_encrypted(page, fixture, passphrase, failures, requests):
    unlock(page, len(fixture["archivedTitles"]), passphrase)
    if card_titles(page) != fixture["archivedTitles"]:
        failures.append(f"encrypted archived order wrong: {card_titles(page)}")
    if sort_state(page)["value"] != "archived":
        failures.append("encrypted default sort is not archived")

    set_sort(page, "published")
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
    set_sort(page, "archived")
    page.wait_for_timeout(400)
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
    if sort_state(page)["value"] != "archived":
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
