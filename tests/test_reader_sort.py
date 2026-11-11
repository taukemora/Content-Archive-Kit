#!/usr/bin/env python3
"""Reader sort order: archived (default) vs published.

Two behaviour layers plus the data contract:

* ``SortDataContractTests`` builds a synthetic archive with ``build_html`` and
  checks the exported sort fields: full frontmatter timestamps survive the
  date-only ``index.csv`` truncation, offset-aware values are normalised to UTC,
  date-only values stay date-only, and unknown/invalid dates sort last.
* ``NodeSortBehaviourTests`` extracts the shipped inline sort module from
  ``scripts/archive_page.html`` and drives the real JavaScript in Node: order of
  both modes, stable ties, legacy catalogs without the new fields, and
  defensive preference storage (missing, malformed, throwing).
* ``BrowserSortAcceptance`` is opt-in (``CA_RUN_BROWSER_TESTS=1``): it exercises
  the offline reader and a real encrypted site in Chromium, including
  selection/body/scroll retention and preference persistence.

All fixtures are synthetic (``example.com``); nothing here reads a private
archive, key or configuration.
"""
from __future__ import annotations

import base64
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
SCRIPTS = ROOT / "scripts"
SCRATCH = _support.SCRATCH
SCRATCH.mkdir(parents=True, exist_ok=True)
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_html import _parse_date_fields, build_html  # noqa: E402
from common import write_entry  # noqa: E402

TEMPLATE = SCRIPTS / "archive_page.html"
SORT_SCRIPT_RE = re.compile(
    r"/\* CA-SORT-MODULE-BEGIN \*/\s*(.*?)\s*/\* CA-SORT-MODULE-END \*/", re.S
)

PASS = "synthetic-reader-sort-passphrase"

# id, frontmatter published, archived, category, collection, published_unknown.
# The archived instants are deliberately written in mixed offsets: a plain
# string sort puts ``ii`` (09:00+08:00) above ``jj`` (04:00+00:00) even though
# ``jj`` is the later instant.
FIXTURES = [
    ("aa", "2026-01-01", "2026-01-02T02:00:00Z", "coding", "manual", False),
    ("bb", "2026-01-02T23:30:00-05:00", "2026-01-03T01:00:00+08:00", "tech", "auto", False),
    ("cc", "2026-1-3", "2026-01-04T10:00:00+08:00", "coding", "manual", False),
    ("dd", "2026-01-04T00:00:00Z", "2026-01-05T00:30:00+00:00", "tech", "auto", False),
    ("ee", "2026-01-04T23:00:00+08:00", "2026-01-06T09:00:00+08:00", "coding", "manual", False),
    ("ff", "2026-01-05T12:00:00+08:00", "2026-01-07T23:00:00-05:00", "tech", "auto", False),
    ("gg", "2026-01-06", "2026-01-09T12:00:00+08:00", "coding", "manual", False),
    ("hh", "2026-01-07T10:00:00+08:00", "2026-01-09T23:00:00Z", "tech", "auto", False),
    ("ii", "2026-01-08T00:00:00+00:00", "2026-01-10T09:00:00+08:00", "coding", "manual", False),
    ("jj", "2026-01-09T23:30:00-05:00", "2026-01-10T04:00:00+00:00", "tech", "auto", False),
    ("kk", "2026-01-08T12:00:00Z", "2026-01-10T04:00:00+00:00", "coding", "manual", False),
    ("ll", "2025-12-20", "2026-01-01T00:00:00+08:00", "tech", "auto", True),
]

# jj/kk share an archived instant, so the id tiebreak (ascending) decides.
EXPECTED_ARCHIVED = ["jj", "kk", "ii", "hh", "gg", "ff", "ee", "dd", "cc", "bb", "aa", "ll"]
# Known dates descending; cc (invalid) and ll (unknown flag) last, ordered by
# archived descending.
EXPECTED_PUBLISHED = ["jj", "kk", "ii", "hh", "gg", "ff", "ee", "dd", "bb", "aa", "cc", "ll"]

EXPECTED_PUBLISHED_SORT = {
    "aa": "2026-01-01",
    "bb": "2026-01-03T04:30:00Z",
    "cc": "",
    "dd": "2026-01-04T00:00:00Z",
    "ee": "2026-01-04T15:00:00Z",
    "ff": "2026-01-05T04:00:00Z",
    "gg": "2026-01-06",
    "hh": "2026-01-07T02:00:00Z",
    "ii": "2026-01-08T00:00:00Z",
    "jj": "2026-01-10T04:30:00Z",
    "kk": "2026-01-08T12:00:00Z",
    "ll": "",
}

EXPECTED_ARCHIVED_SORT = {
    "aa": "2026-01-02T02:00:00Z",
    "bb": "2026-01-02T17:00:00Z",
    "cc": "2026-01-04T02:00:00Z",
    "dd": "2026-01-05T00:30:00Z",
    "ee": "2026-01-06T01:00:00Z",
    "ff": "2026-01-08T04:00:00Z",
    "gg": "2026-01-09T04:00:00Z",
    "hh": "2026-01-09T23:00:00Z",
    "ii": "2026-01-10T01:00:00Z",
    "jj": "2026-01-10T04:00:00Z",
    "kk": "2026-01-10T04:00:00Z",
    "ll": "2025-12-31T16:00:00Z",
}

# Display stays the source-local calendar date: jj is 2026-01-09 locally even
# though its UTC instant falls on 2026-01-10.
EXPECTED_DISPLAY = {
    "aa": "2026-01-01",
    "bb": "2026-01-02",
    "cc": "",
    "dd": "2026-01-04",
    "ee": "2026-01-04",
    "ff": "2026-01-05",
    "gg": "2026-01-06",
    "hh": "2026-01-07",
    "ii": "2026-01-08",
    "jj": "2026-01-09",
    "kk": "2026-01-08",
    "ll": "2025-12-20",
}

SEARCH_NEEDLE = "ZETANEEDLE"
LABELS = {"coding": "编程与开发", "tech": "科技与 AI"}
CSV_FIELDS = [
    "id", "published", "title", "original_title", "source", "source_type",
    "category", "subcategory", "context", "tags", "url", "summary_chars",
    "images", "status", "content_note", "relevance", "path", "archived",
    "language", "collection", "summary",
]


def title_for(entry_id: str) -> str:
    return f"排序文章 {entry_id.upper()}"


def body_for(entry_id: str) -> str:
    paragraphs = [
        f"这是合成排序测试文章 {entry_id.upper()} 的第 {index} 段正文，仅用于撑开阅读区域。"
        for index in range(1, 13)
    ]
    if entry_id == "hh":
        paragraphs.insert(3, f"独特检索词 {SEARCH_NEEDLE} 只出现在这一篇合成文章中。")
    return "\n\n".join(paragraphs)


def _row(entry_id: str, published: str, archived: str, category: str,
         collection: str, unknown: bool) -> dict:
    # index.csv only keeps the first ten characters of ``published``; the test
    # rows reproduce that truncation so build_html must use the frontmatter.
    return {
        "id": entry_id,
        "path": f"library/{category}/{entry_id}/entry.md",
        "title": title_for(entry_id),
        "original_title": title_for(entry_id),
        "published": published[:10],
        "archived": archived,
        "category": category,
        "source": "合成发布方甲" if category == "coding" else "合成发布方乙",
        "source_type": "web",
        "status": "archived",
        "collection": collection,
        "summary": f"合成摘要：{title_for(entry_id)}。",
        "url": f"https://example.com/sort/{entry_id}",
        "tags": ["排序样本"],
        "images": 0,
        "summary_chars": 20,
        "published_unknown": unknown,
    }


def build_archive(root: Path) -> list[dict]:
    """Write synthetic entries and return index.csv-shaped rows."""
    rows: list[dict] = []
    for entry_id, published, archived, category, collection, unknown in FIXTURES:
        entry_dir = root / "library" / category / entry_id
        fm = {
            "id": entry_id,
            "title": title_for(entry_id),
            "original_title": title_for(entry_id),
            "published": published,
            "archived": archived,
            "category": category,
            "source": "合成发布方甲" if category == "coding" else "合成发布方乙",
            "source_type": "web",
            "author": "合成作者",
            "url": f"https://example.com/sort/{entry_id}",
            "collection": collection,
            "tags": ["排序样本"],
            "status": "archived",
            "published_unknown": unknown,
        }
        body = body_for(entry_id)
        if entry_id == "hh":
            image = entry_dir / "images" / "mark.png"
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGOQS2kC"
                "AAGoAQVqBVcuAAAAAElFTkSuQmCC"
            ))
            body += "\n\n![合成封面](images/mark.png)"
        write_entry(entry_dir / "entry.md", fm, body)
        rows.append(_row(entry_id, published, archived, category, collection, unknown))
    return rows


def write_index_csv(root: Path, rows: list[dict]) -> None:
    with (root / "index.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in CSV_FIELDS})


def embedded_articles(html_doc: str) -> list[dict]:
    match = re.search(
        r'<script id="archive-data" type="application/json">\s*(.*?)\s*</script>',
        html_doc,
        re.S,
    )
    if not match:
        raise AssertionError("archive-data block missing")
    return json.loads(match.group(1))


class SyntheticArchiveCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(dir=str(SCRATCH), prefix="test_reader_sort_"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.archive = self.tmpdir / "archive"
        self.archive.mkdir()
        self.rows = build_archive(self.archive)

    def build_offline(self) -> str:
        return build_html(self.archive, self.rows, LABELS)


class SortDataContractTests(SyntheticArchiveCase):
    def test_archived_sort_normalises_offsets(self):
        articles = embedded_articles(self.build_offline())
        self.assertEqual([a["id"] for a in articles], EXPECTED_ARCHIVED)
        for article in articles:
            with self.subTest(entry=article["id"]):
                self.assertEqual(article["archived_sort"], EXPECTED_ARCHIVED_SORT[article["id"]])
                # The raw display value is preserved for the provenance note.
                self.assertTrue(article["archived"].startswith("20"))

    def test_published_uses_full_frontmatter_timestamp(self):
        articles = {a["id"]: a for a in embedded_articles(self.build_offline())}
        for entry_id, expected in EXPECTED_PUBLISHED_SORT.items():
            with self.subTest(entry=entry_id):
                self.assertEqual(articles[entry_id]["published_sort"], expected)
        # index.csv only carried 2026-01-02 for bb; the frontmatter timestamp
        # must survive with its offset converted to UTC.
        self.assertEqual(articles["bb"]["published"], "2026-01-02")
        self.assertEqual(articles["jj"]["published"], "2026-01-09")

    def test_date_only_stays_date_only(self):
        articles = {a["id"]: a for a in embedded_articles(self.build_offline())}
        self.assertEqual(articles["aa"]["published_sort"], "2026-01-01")
        self.assertEqual(articles["gg"]["published_sort"], "2026-01-06")
        self.assertNotIn("T", articles["aa"]["published_sort"])
        self.assertNotIn("T", articles["gg"]["published_sort"])
        self.assertEqual(articles["aa"]["published"], "2026-01-01")

    def test_unknown_and_invalid_published_sort_last(self):
        articles = {a["id"]: a for a in embedded_articles(self.build_offline())}
        self.assertEqual(articles["cc"]["published_sort"], "")
        self.assertTrue(articles["cc"]["published_unknown"])
        self.assertEqual(articles["cc"]["published"], "")
        self.assertEqual(articles["ll"]["published_sort"], "")
        self.assertTrue(articles["ll"]["published_unknown"])
        # A known date that only has date precision is still sortable.
        self.assertTrue(articles["gg"]["published_sort"])

    def test_every_article_carries_sort_fields(self):
        for article in embedded_articles(self.build_offline()):
            with self.subTest(entry=article["id"]):
                self.assertIn("published_sort", article)
                self.assertIn("archived_sort", article)
                self.assertIsInstance(article["published_sort"], str)
                self.assertIsInstance(article["archived_sort"], str)

    def test_naive_offsets_and_invalid_dates(self):
        # Naive values are read in the archive timezone, matching common.iso().
        self.assertEqual(_parse_date_fields("2026-01-10T09:00:00")[1], "2026-01-10T01:00:00Z")
        self.assertEqual(_parse_date_fields("2026-01-10T09:00:00+08:00")[1], "2026-01-10T01:00:00Z")
        self.assertEqual(_parse_date_fields("2026-01-09 23:30:00-05:00")[0], "2026-01-09")
        for bad in ("2026-02-31", "2026-13-01", "not-a-date", "2026-02-30T10:00:00Z", ""):
            with self.subTest(value=bad):
                self.assertEqual(_parse_date_fields(bad), ("", "", False))

    def test_invalid_offsets_are_rejected_not_silently_shifted(self):
        # ``+02:99`` is not a 3h39m offset; minutes > 59 and hours > 23 are
        # malformed metadata, so the value must be reported invalid.
        for bad in (
            "2026-01-01T12:00:00+02:99",
            "2026-01-01T12:00:00-02:99",
            "2026-01-01T12:00:00+24:00",
            "2026-01-01T12:00:00-24:00",
            "2026-01-01T12:00:00+99:99",
        ):
            with self.subTest(value=bad):
                self.assertEqual(_parse_date_fields(bad), ("", "", False))

    def test_utc_conversion_overflow_is_invalid_not_an_exception(self):
        # Converting these to UTC would leave the representable year range;
        # bad metadata must never abort the whole build.
        for bad in (
            "0001-01-01T00:00:00+23:00",
            "9999-12-31T23:59:59-23:00",
        ):
            with self.subTest(value=bad):
                try:
                    result = _parse_date_fields(bad)
                except Exception as exc:  # noqa: BLE001 - the point of the test
                    self.fail(f"{bad} raised {type(exc).__name__}: {exc}")
                self.assertEqual(result, ("", "", False))

    def test_small_years_keep_their_written_identity(self):
        display, sort_key, valid = _parse_date_fields("0099-01-01T00:00:00Z")
        self.assertTrue(valid)
        self.assertEqual(display, "0099-01-01")
        self.assertEqual(sort_key, "0099-01-01T00:00:00Z")

    def test_subsecond_precision_survives_and_orders_correctly(self):
        whole = _parse_date_fields("2026-01-01T00:00:00Z")
        early = _parse_date_fields("2026-01-01T00:00:00.100Z")
        late = _parse_date_fields("2026-01-01T00:00:00.900Z")
        for parsed in (whole, early, late):
            self.assertTrue(parsed[2])
        # Fractions must not be truncated to the same whole second.
        self.assertNotEqual(early[1], late[1])
        # Build a catalog whose members share a whole second and prove the
        # exported order uses second+fraction, not a plain ISO string compare
        # (where ``Z`` sorts after ``.`` and would put the whole second last).
        root = self.tmpdir / "subsecond"
        root.mkdir()
        rows = []
        for entry_id, published in (
            ("whole", "2026-01-01T00:00:00Z"),
            ("early", "2026-01-01T00:00:00.100Z"),
            ("late", "2026-01-01T00:00:00.900Z"),
        ):
            fm = {
                "id": entry_id,
                "title": title_for(entry_id),
                "original_title": title_for(entry_id),
                "published": published,
                "archived": "2026-01-02T00:00:00Z",
                "category": "coding",
                "source": "合成发布方甲",
                "source_type": "web",
                "url": f"https://example.com/subsecond/{entry_id}",
                "collection": "manual",
                "tags": ["排序样本"],
                "status": "archived",
            }
            entry_dir = root / "library" / "coding" / entry_id
            write_entry(entry_dir / "entry.md", fm, body_for(entry_id))
            rows.append(_row(entry_id, published, "2026-01-02T00:00:00Z", "coding", "manual", False))
        articles = embedded_articles(build_html(root, rows, LABELS))
        self.assertEqual([a["id"] for a in articles], ["late", "early", "whole"])

    def test_fetched_date_source_without_flag_is_unknown_and_last(self):
        # Old records predate ``published_unknown``; ``date_source='fetched'``
        # alone means the date is a crawl fallback, never a publish time.
        root = self.tmpdir / "fetched-source"
        root.mkdir()
        known_fm = {
            "id": "known",
            "title": title_for("known"),
            "original_title": title_for("known"),
            "published": "2026-01-01T00:00:00Z",
            "archived": "2026-01-02T00:00:00Z",
            "category": "coding",
            "source": "合成发布方甲",
            "source_type": "web",
            "url": "https://example.com/fetched/known",
            "collection": "manual",
            "tags": ["排序样本"],
            "status": "archived",
        }
        fetched_fm = dict(known_fm)
        fetched_fm.update({
            "id": "fetched",
            "title": title_for("fetched"),
            "original_title": title_for("fetched"),
            "published": "2026-01-20T09:00:00+08:00",
            # No published_unknown key on purpose.
            "date_source": "fetched",
            "url": "https://example.com/fetched/fetched",
        })
        rows = []
        for fm, entry_id in ((known_fm, "known"), (fetched_fm, "fetched")):
            write_entry(root / "library" / "coding" / entry_id / "entry.md", fm, body_for(entry_id))
            rows.append(_row(entry_id, fm["published"], fm["archived"], "coding", "manual", False))
        articles = {a["id"]: a for a in embedded_articles(build_html(root, rows, LABELS))}
        self.assertTrue(articles["fetched"]["published_unknown"])
        self.assertEqual(articles["fetched"]["published_sort"], "")
        self.assertFalse(articles["known"]["published_unknown"])
        self.assertTrue(articles["known"]["published_sort"])
        # Unknown published dates stay at the end of the exported catalog.
        self.assertEqual(
            [a["id"] for a in embedded_articles(build_html(root, rows, LABELS))],
            ["known", "fetched"],
        )

    def test_bad_date_metadata_never_aborts_the_build(self):
        # One malformed offset and one UTC-overflow value, mixed with a good
        # record: build_html must still return a catalog, not raise.
        root = self.tmpdir / "bad-metadata"
        root.mkdir()
        specs = (
            ("good", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
            ("badoffset", "2026-01-01T12:00:00+02:99", "2026-01-02T00:00:00Z"),
            ("overflow", "0001-01-01T00:00:00+23:00", "9999-12-31T23:59:59-23:00"),
        )
        rows = []
        for entry_id, published, archived in specs:
            fm = {
                "id": entry_id,
                "title": title_for(entry_id),
                "original_title": title_for(entry_id),
                "published": published,
                "archived": archived,
                "category": "coding",
                "source": "合成发布方甲",
                "source_type": "web",
                "url": f"https://example.com/bad/{entry_id}",
                "collection": "manual",
                "tags": ["排序样本"],
                "status": "archived",
            }
            write_entry(root / "library" / "coding" / entry_id / "entry.md", fm, body_for(entry_id))
            rows.append(_row(entry_id, published, archived, "coding", "manual", False))
        try:
            html_doc = build_html(root, rows, LABELS)
        except Exception as exc:  # noqa: BLE001 - the point of the test
            self.fail(f"build_html raised {type(exc).__name__}: {exc}")
        articles = {a["id"]: a for a in embedded_articles(html_doc)}
        for entry_id in ("badoffset", "overflow"):
            with self.subTest(entry=entry_id):
                self.assertTrue(articles[entry_id]["published_unknown"])
                self.assertEqual(articles[entry_id]["published_sort"], "")
        # Only the overflow record's archived value is also invalid; the
        # malformed offset is confined to the published field.
        self.assertEqual(articles["overflow"]["archived_sort"], "")
        self.assertTrue(articles["badoffset"]["archived_sort"])
        self.assertTrue(articles["good"]["published_sort"])



class SortControlMarkupTests(unittest.TestCase):
    def setUp(self):
        self.template = TEMPLATE.read_text(encoding="utf-8")

    def test_compact_accessible_control_near_filters(self):
        self.assertIn('id="sort-archived"', self.template)
        self.assertIn('id="sort-published"', self.template)
        self.assertIn('role="group"', self.template)
        self.assertIn('aria-label="排序方式"', self.template)
        self.assertIn('data-sort="archived"', self.template)
        self.assertIn('data-sort="published"', self.template)
        self.assertIn('aria-pressed="true"', self.template)
        self.assertIn("最近归档", self.template)
        self.assertIn("最近发布", self.template)
        # The control belongs to the sidebar filter block, before the list.
        self.assertLess(self.template.index('id="sort-archived"'), self.template.index('id="article-list"'))
        self.assertGreater(
            self.template.index('id="sort-archived"'), self.template.index('id="category-filter"')
        )

    def test_sort_module_is_extractable_for_node(self):
        self.assertIsNotNone(SORT_SCRIPT_RE.search(self.template), "inline sort module missing")
        source = SORT_SCRIPT_RE.search(self.template).group(1)
        self.assertIn("window.CAReaderSort", source)

    def test_sort_module_lives_in_the_single_inline_script(self):
        # The encrypted build hashes exactly one inline <script>; a second
        # inline block would be blocked by its CSP.
        self.assertEqual(self.template.count("<script>\n"), 1)
        self.assertNotIn('<script id="ca-reader-sort">', self.template)
        self.assertLess(
            self.template.index("CA-SORT-MODULE-BEGIN"),
            self.template.index("const encryptedMode"),
        )

    def test_only_the_sort_preference_is_stored(self):
        source = SORT_SCRIPT_RE.search(self.template).group(1)
        self.assertIn("ca.reader.sort", source)
        self.assertEqual(source.count("setItem("), 1)
        for forbidden in ("passphrase", "dek", "session", "body_html", "content_hash"):
            self.assertNotIn(forbidden, source.lower())


NODE_HARNESS = r"""
'use strict';
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const src = fs.readFileSync(input.script, 'utf8');

function loadApi(win) {
  const factory = new Function('window', src + '\nreturn window.CAReaderSort;');
  return factory(win);
}

const api = loadApi({});
const cases = [];
function record(name, fn) {
  try {
    const detail = fn();
    cases.push({ name: name, ok: true, detail: detail == null ? '' : String(detail) });
  } catch (err) {
    cases.push({ name: name, ok: false, detail: (err && err.stack) || String(err) });
  }
}
function assert(cond, msg) { if (!cond) throw new Error(msg); }
function ids(list) { return list.map(function (a) { return String(a.id); }); }
function eq(actual, expected, label) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) throw new Error(label + ': got ' + a + ' expected ' + e);
}
function stripSortFields(record) {
  const copy = Object.assign({}, record);
  delete copy.published_sort;
  delete copy.archived_sort;
  return copy;
}
function memStorage(initial) {
  const map = new Map(Object.entries(initial || {}));
  return {
    _map: map,
    get length() { return map.size; },
    key: function (i) { const keys = Array.from(map.keys()); return i < keys.length ? keys[i] : null; },
    getItem: function (k) { return map.has(k) ? map.get(k) : null; },
    setItem: function (k, v) { map.set(String(k), String(v)); },
    removeItem: function (k) { map.delete(k); },
    clear: function () { map.clear(); }
  };
}

const records = input.records;
const legacy = records.map(stripSortFields);

record('archived order uses normalised instants', function () {
  eq(ids(api.sortArticles(records, 'archived')), input.expectedArchived, 'archived');
});
record('archived order for legacy records', function () {
  eq(ids(api.sortArticles(legacy, 'archived')), input.expectedArchived, 'legacy archived');
});
record('published order puts known dates first', function () {
  eq(ids(api.sortArticles(records, 'published')), input.expectedPublished, 'published');
});
record('published order for legacy records', function () {
  eq(ids(api.sortArticles(legacy, 'published')), input.expectedPublished, 'legacy published');
});
record('unknown archived timestamps sort last', function () {
  const extra = records.slice(0, 2).map(function (r) { return Object.assign({}, r); });
  extra[0].id = 'zz-missing';
  extra[0].archived = '';
  extra[0].archived_sort = '';
  extra[1].id = 'aa-tied';
  extra[1].archived = '2026-01-02T02:00:00Z';
  extra[1].archived_sort = '2026-01-02T02:00:00Z';
  eq(ids(api.sortArticles(extra, 'archived')), ['aa-tied', 'zz-missing'], 'unknown archived');
});
record('date-only keeps date-only precision', function () {
  eq(api.publishedKey({ published: '2026-02-03' }), '2026-02-03', 'date-only key');
  eq(api.publishedKey({ published_sort: '2026-02-03' }), '2026-02-03', 'exported date-only');
  eq(api.archivedKey({ archived: '2026-02-03' }), '2026-02-03', 'archived date-only');
});
record('legacy offsets are converted for comparison', function () {
  eq(api.publishedKey({ published: '2026-02-03T23:30:00-05:00' }), '2026-02-04T04:30:00Z', 'offset');
  eq(api.archivedKey({ archived: '2026-02-04T09:00:00+08:00' }), '2026-02-04T01:00:00Z', 'offset archived');
});
record('unknown flag wins over a sortable date', function () {
  eq(api.publishedKey({ published: '2026-02-03', published_sort: '2026-02-03', published_unknown: true }), '', 'unknown flag');
});
record('malformed sort fields fall back to the raw value', function () {
  eq(api.publishedKey({ published: '2026-03-04', published_sort: 'not-a-date' }), '2026-03-04', 'fallback');
  eq(api.publishedKey({ published: 'not-a-date' }), '', 'invalid');
});
record('impossible calendar dates are rejected', function () {
  eq(api.publishedKey({ published: '2026-02-31' }), '', 'feb 31');
  eq(api.publishedKey({ published: '2026-13-01T00:00:00Z' }), '', 'month 13');
});
record('invalid offsets are rejected, not silently shifted', function () {
  eq(api.publishedKey({ published: '2026-01-01T12:00:00+02:99' }), '', 'minutes 99');
  eq(api.publishedKey({ published: '2026-01-01T12:00:00-02:99' }), '', 'negative minutes 99');
  eq(api.publishedKey({ published: '2026-01-01T12:00:00+24:00' }), '', 'hours 24');
  eq(api.publishedKey({ published: '2026-01-01T12:00:00-24:00' }), '', 'negative hours 24');
});
record('utc conversion overflow is rejected without throwing', function () {
  eq(api.publishedKey({ published: '0001-01-01T00:00:00+23:00' }), '', 'underflow');
  eq(api.publishedKey({ published: '9999-12-31T23:59:59-23:00' }), '', 'overflow');
});
record('small years keep their written identity', function () {
  eq(api.publishedKey({ published: '0099-01-01T00:00:00Z' }), '0099-01-01T00:00:00Z', 'year 99');
});
record('subsecond fractions keep their order', function () {
  eq(api.publishedKey({ published: '2026-01-01T00:00:00Z' }), '2026-01-01T00:00:00Z', 'whole');
  eq(api.publishedKey({ published: '2026-01-01T00:00:00.100Z' }),
     '2026-01-01T00:00:00.100000000Z', '100ms');
  eq(api.publishedKey({ published: '2026-01-01T00:00:00.900Z' }),
     '2026-01-01T00:00:00.900000000Z', '900ms');
  var fractional = [
    { id: 'whole', published_sort: '2026-01-01T00:00:00Z' },
    { id: 'late', published_sort: '2026-01-01T00:00:00.900000000Z' },
    { id: 'early', published_sort: '2026-01-01T00:00:00.100000000Z' }
  ];
  eq(ids(api.sortArticles(fractional, 'published')), ['late', 'early', 'whole'], 'fraction order');
});
record('year zero is invalid before timezone conversion', function () {
  eq(api.normalizeTimestamp('0000-01-01'), '', 'date-only year zero');
  eq(api.normalizeTimestamp('0000-12-31T23:00:00-02:00'), '', 'year zero crossing into year one');
  eq(api.normalizeTimestamp('0001-01-01'), '0001-01-01', 'year one remains valid');
});
record('ties break by id ascending', function () {
  const tied = [
    { id: 'bbb', archived_sort: '2026-01-10T04:00:00Z', published_sort: '' },
    { id: 'aaa', archived_sort: '2026-01-10T04:00:00Z', published_sort: '' }
  ];
  eq(ids(api.sortArticles(tied, 'archived')), ['aaa', 'bbb'], 'id tie');
});
record('sorting does not mutate the input', function () {
  const original = ids(records);
  api.sortArticles(records, 'published');
  eq(ids(records), original, 'input order');
});
record('unknown mode falls back to archived', function () {
  eq(ids(api.sortArticles(records, 'bogus')), input.expectedArchived, 'bogus mode');
});

record('default mode is archived without storage', function () {
  eq(api.readMode(), 'archived', 'missing storage');
  eq(api.DEFAULT_MODE, 'archived', 'default constant');
});
record('valid preference is read back', function () {
  const win = { localStorage: memStorage({ 'ca.reader.sort': 'published' }) };
  const localApi = loadApi(win);
  eq(localApi.readMode(), 'published', 'stored preference');
});
record('malformed preferences fall back safely', function () {
  const values = ['PUBLISHED', ' published', 'published ', '{"sort":"published"}', '', 'archivedx',
                  'ca.reader.sort', 'published\n', 'x'.repeat(200000), 'null'];
  values.forEach(function (value) {
    const win = { localStorage: memStorage({ 'ca.reader.sort': value }) };
    eq(loadApi(win).readMode(), 'archived', 'malformed ' + JSON.stringify(value.slice(0, 20)));
  });
});
record('only a validated mode is written', function () {
  const store = memStorage({});
  const localApi = loadApi({ localStorage: store });
  assert(localApi.writeMode('published') === true, 'valid write rejected');
  eq(store.getItem('ca.reader.sort'), 'published', 'stored value');
  eq(Array.from(store._map.keys()), ['ca.reader.sort'], 'only one key');
  assert(localApi.writeMode('bogus') === false, 'invalid write accepted');
  assert(localApi.writeMode(null) === false, 'null write accepted');
  eq(store.getItem('ca.reader.sort'), 'published', 'invalid write changed storage');
});
record('disabled or throwing storage never breaks the reader', function () {
  const getterWin = {};
  Object.defineProperty(getterWin, 'localStorage', {
    configurable: true,
    get: function () { throw new Error('storage disabled'); }
  });
  const denied = loadApi(getterWin);
  eq(denied.readMode(), 'archived', 'getter read');
  assert(denied.writeMode('published') === false, 'getter write');
  eq(ids(denied.sortArticles(records, 'published')), input.expectedPublished, 'sort still works');

  const broken = loadApi({ localStorage: { getItem: function () { throw new Error('x'); },
                                          setItem: function () { throw new Error('x'); } } });
  eq(broken.readMode(), 'archived', 'throwing read');
  assert(broken.writeMode('published') === false, 'throwing write');

  const quota = loadApi({ localStorage: { getItem: function () { return null; },
                                          setItem: function () { throw new Error('quota'); } } });
  eq(quota.readMode(), 'archived', 'quota read');
  assert(quota.writeMode('published') === false, 'quota write');
});

const failed = cases.filter(function (c) { return !c.ok; }).length;
console.log(JSON.stringify({ ok: failed === 0, total: cases.length, failed: failed, cases: cases }));
"""


class NodeSortBehaviourTests(unittest.TestCase):
    def test_real_javascript_sort_and_storage_behaviour(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available")
        template = TEMPLATE.read_text(encoding="utf-8")
        match = SORT_SCRIPT_RE.search(template)
        self.assertIsNotNone(match, "inline CA-SORT-MODULE markers missing")
        work = Path(tempfile.mkdtemp(dir=str(SCRATCH), prefix="test_reader_sort_node_"))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        script_path = work / "sort_module.js"
        script_path.write_text(match.group(1), encoding="utf-8")

        records = []
        for entry_id, published, archived, category, collection, unknown in FIXTURES:
            records.append({
                "id": entry_id,
                "title": title_for(entry_id),
                "published": EXPECTED_DISPLAY[entry_id],
                "published_sort": EXPECTED_PUBLISHED_SORT[entry_id],
                "published_unknown": unknown or entry_id == "cc",
                "archived": archived,
                "archived_sort": EXPECTED_ARCHIVED_SORT[entry_id],
                "category": category,
                "collection": collection,
            })
        input_path = work / "input.json"
        input_path.write_text(json.dumps({
            "script": str(script_path),
            "records": records,
            "expectedArchived": EXPECTED_ARCHIVED,
            "expectedPublished": EXPECTED_PUBLISHED,
        }, ensure_ascii=False), encoding="utf-8")
        harness = work / "harness.js"
        harness.write_text(NODE_HARNESS, encoding="utf-8")

        proc = subprocess.run([node, str(harness), str(input_path)],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(report["ok"], json.dumps(report, ensure_ascii=False, indent=2))
        self.assertGreaterEqual(report["total"], 15)
        names = {case["name"] for case in report["cases"]}
        for expected in (
            "archived order uses normalised instants",
            "published order puts known dates first",
            "unknown archived timestamps sort last",
            "date-only keeps date-only precision",
            "ties break by id ascending",
            "malformed preferences fall back safely",
            "disabled or throwing storage never breaks the reader",
        ):
            self.assertIn(expected, names)


class EncryptedIndexMetadataTests(SyntheticArchiveCase):
    def test_encrypted_index_carries_sort_fields_without_plaintext(self):
        from encrypted_site import build_site, initialize_key
        from test_encrypted_site import decrypt_file, derive_dek, object_oid_from_rel

        write_index_csv(self.archive, self.rows)
        (self.archive / "taxonomy.yaml").write_text(
            "version: 1\ncategories:\n  - slug: coding\n    label: 编程与开发\n"
            "  - slug: tech\n    label: 科技与 AI\n",
            encoding="utf-8",
        )
        key = self.tmpdir / "keys" / "site-key.json"
        initialize_key(key, PASS)
        out = self.tmpdir / "site-out"
        build_site(self.archive, out, key)

        site = json.loads((out / "site.json").read_text(encoding="utf-8"))
        dek = derive_dek(site, PASS)
        index_oid = object_oid_from_rel(site["index"]["file"])
        plain = decrypt_file(out / site["index"]["file"], dek, site["archive_id"], "index", index_oid)
        index = json.loads(plain.decode("utf-8"))
        by_title = {a["title"]: a for a in index["articles"]}
        for entry_id in EXPECTED_PUBLISHED_SORT:
            article = by_title[title_for(entry_id)]
            with self.subTest(entry=entry_id):
                self.assertEqual(article["published_sort"], EXPECTED_PUBLISHED_SORT[entry_id])
                self.assertEqual(article["archived_sort"], EXPECTED_ARCHIVED_SORT[entry_id])

        shell = (out / "index.html").read_text(encoding="utf-8")
        # The shell may ship the sorting programme, but never article data.
        self.assertNotIn("排序文章", shell)
        self.assertNotIn("2026-01-10T04:00:00Z", shell)
        data = re.search(
            r'<script id="archive-data" type="application/json">\s*(.*?)\s*</script>',
            shell,
            re.S,
        )
        self.assertIsNotNone(data)
        self.assertEqual(json.loads(data.group(1)), [])
        self.assertIn('id="sort-published"', shell)


class BrowserSortAcceptance(SyntheticArchiveCase):
    """Opt-in real-Chromium run (set CA_RUN_BROWSER_TESTS=1)."""

    @unittest.skipUnless(
        os.environ.get("CA_RUN_BROWSER_TESTS") == "1",
        "set CA_RUN_BROWSER_TESTS=1",
    )
    def test_real_chromium_sort_controls_offline_and_encrypted(self):
        python = os.environ.get("CA_PLAYWRIGHT_PYTHON") or sys.executable
        if not Path(python).is_file():
            self.skipTest("playwright python not available: set CA_PLAYWRIGHT_PYTHON")
        probe = subprocess.run([python, "-c", "import playwright.sync_api"],
                               capture_output=True, text=True)
        if probe.returncode != 0:
            self.skipTest(f"playwright is not installed for {python}: {probe.stderr.strip()}")

        offline_dir = self.tmpdir / "offline-site"
        offline_dir.mkdir()
        html_doc = build_html(self.archive, self.rows, LABELS)
        (offline_dir / "index.html").write_text(html_doc, encoding="utf-8")
        # A legacy catalog has no published_sort/archived_sort fields; the
        # reader must fall back to the values an older export carried.
        legacy = [
            {key: value for key, value in article.items()
             if key not in ("published_sort", "archived_sort")}
            for article in embedded_articles(html_doc)
        ]
        (offline_dir / "legacy.html").write_text(
            re.sub(
                r'(<script id="archive-data" type="application/json">\s*).*?(\s*</script>)',
                lambda m: m.group(1) + json.dumps(legacy, ensure_ascii=False) + m.group(2),
                html_doc,
                count=1,
                flags=re.S,
            ),
            encoding="utf-8",
        )
        # The offline page references media relative to the archive root.
        shutil.copytree(self.archive / "library", offline_dir / "library")

        from encrypted_site import build_site, initialize_key

        write_index_csv(self.archive, self.rows)
        (self.archive / "taxonomy.yaml").write_text(
            "version: 1\ncategories:\n  - slug: coding\n    label: 编程与开发\n"
            "  - slug: tech\n    label: 科技与 AI\n",
            encoding="utf-8",
        )
        key = self.tmpdir / "keys" / "site-key.json"
        initialize_key(key, PASS)
        encrypted_dir = self.tmpdir / "site-out"
        build_site(self.archive, encrypted_dir, key)

        fixture = {
            "archivedTitles": [title_for(i) for i in EXPECTED_ARCHIVED],
            "publishedTitles": [title_for(i) for i in EXPECTED_PUBLISHED],
            "selectTitle": title_for("gg"),
            "mediaTitle": title_for("hh"),
            "searchNeedle": SEARCH_NEEDLE,
            "searchTitle": title_for("hh"),
            "techCount": sum(1 for f in FIXTURES if f[3] == "tech"),
        }
        fixture_path = self.tmpdir / "browser-fixture.json"
        fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
        script = str(ROOT / "tests" / "browser_reader_sort.py")

        def run(directory: Path, mode: str) -> dict:
            cmd = [python, script, "--dir", str(directory), "--mode", mode,
                   "--fixture", str(fixture_path)]
            if mode == "encrypted":
                cmd += ["--passphrase", PASS]
            browser = os.environ.get("CA_PLAYWRIGHT_BROWSER")
            if browser:
                cmd += ["--browser", browser]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=str(ROOT))
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            report = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertTrue(report["ok"], json.dumps(report, ensure_ascii=False, indent=2))
            return report

        run(offline_dir, "offline")
        run(offline_dir, "legacy")
        run(encrypted_dir, "encrypted")


if __name__ == "__main__":
    unittest.main()
