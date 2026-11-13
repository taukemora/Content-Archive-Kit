#!/usr/bin/env python3
"""Collect candidate articles from configured vendor sources.

Emits JSON. It does not fetch full article text or summarize; the agent does that.

Phase 3 hardening (see docs/plans/phase3-fixes.md §1/§2/§3/§6):
- zero-result / unparsable sources report ``status="empty"`` and never advance
  ``initialized``; three consecutive empty/error runs set ``needs_attention``.
- candidates without a date fall back to card text -> URL -> fetch date and are
  marked ``published_unknown`` / ``date_source="fetched"`` instead of vanishing.
- the water mark is the newest *archived* entry per source (derived from
  ``library/``), so ``last_newest`` only moves when something is archived.
- quota is applied at selection time (per-source round-robin), leftovers are
  parked in ``.state/pending.json`` and counted as ``dropped_by_max``.

Wave 1B hardening (see docs/plans/phase4-all-fixes.md):
- once a source has an archived water mark, that water mark is the only gate;
  the 36h window applies *only* to sources without one, and a 30-day lookback
  bounds the sweep. A missed cron day can no longer drop items.
- fetch-date fallbacks (``published_unknown`` / ``date_source: fetched``) and
  impossible future dates never poison the water mark, and card/URL dates beyond
  tomorrow are ignored.
- the GLM adapter no longer writes ``.state/raw`` itself; snapshots are written
  by the main loop only for selected/parked sections (never in ``--dry-run``) and
  snapshots older than 30 days are pruned.
- ``fetch_text`` refuses env proxies, follows at most 3 redirects, caps a response
  at 10MB and blocks private/loopback/metadata targets (loopback only when the
  initial URL is loopback itself).
- ``.state/pending.json`` / ``.state/sources.json`` writes take ``.state/.lock``.

Batch 2 hardening (see docs/plans/2026-10-29-repair-batches.md §2):

- an archived water mark no longer *filters* candidates, it only switches the
  source to the 30-day lookback sweep: every unseen candidate inside that
  window is selected, including one published exactly at (or slightly before)
  the water mark. ``seen.json`` stays the only "already archived" gate, so a
  same-timestamp late publication or a back-dated announcement cannot be lost.
  Candidates outside the lookback are counted as ``coverage_skipped`` instead of
  being parked (a full RSS history must not flood the queue).
- window bounds are lower bounds, so a day-precision date is compared at the
  *start* of its day (the 36h window cannot stretch to ~60h); the water mark is
  an upper bound, so the same date is compared at the *end* of its day (a new
  item published on the water-mark day is never starved).
- pending records are retried from their stored url/title/date/source/extractor
  after they roll out of the upstream feed. Their 30-day life starts at the
  first enqueue (``queued_at``, legacy ``first_seen`` / ``added_at`` /
  ``parked_at`` / ``dropped_at``) and is never restarted by a re-park. Every
  exit is reported in ``pending_exits`` (url/source_id/reason =
  ``expired``/``seen``/``over_limit``) with a ``pending_expired`` count.
- a failed source keeps its queue untouched (no exits, no TTL extension), and
  pending records win over fresh items under the per-source / global caps.
- every selected item is persisted as ``awaiting_archive``, so an agent that
  dies mid-run cannot lose it; the next run drops it once it is seen.
- the commit phase re-reads ``pending.json`` / ``sources.json`` inside
  ``.state/.lock`` and merges this run's delta (network happens outside the
  lock), so a concurrent run's new records, another source's state, a newer
  ``last_run`` and a record updated by someone else are never clobbered.
- ``--dry-run`` performs the same merge arithmetic on the current files and
  reports the same numbers without writing anything.
"""
from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from markdownify import markdownify as md

from common import (
    ROOT,
    TZ,
    assert_public_url,
    canonicalize_url,
    dedupe_key,
    iso,
    load_json,
    load_seen,
    load_yaml,
    now,
    read_entry,
    slugify,
    write_json_atomic,
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
DATE_RE = re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})")
URL_DATE_RE = re.compile(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})")
EN_DATE_RE = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+20\d{2}"
)

# A date that only carries day precision is compared as end-of-day, so a new
# item published the same day as the water mark is not starved (§3).
DAY_PADDING = timedelta(hours=24) - timedelta(seconds=1)
STALE_DAYS_WARN = 14
NEEDS_ATTENTION_STREAK = 3
CANDIDATE_RETENTION_DAYS = 90
PENDING_LIMIT = 300
PENDING_MAX_AGE_DAYS = 30
# Upper bound for the sweep of an *already water-marked* source. The archived
# water mark stays the real gate (a missed cron day, a two-day vendor outage or
# a late publication must never be dropped); this only stops a month-long gap
# from dragging the whole archive back in.
MAX_WATERMARK_LOOKBACK_DAYS = 30
# Fields that record when a pending entry first entered the retry queue. The
# order matters: legacy records only carry ``dropped_at``/``parked_at`` and must
# never be aged from ``published`` (an old article re-queued today is young).
PENDING_QUEUED_KEYS = ("queued_at", "first_seen", "added_at", "parked_at", "dropped_at")
# Raw whole-page snapshots (GLM docs) are debugging aids, not history.
RAW_SNAPSHOT_RETENTION_DAYS = 30
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 3
STREAM_CHUNK = 64 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "::1"}


class SourceEmpty(Exception):
    """The adapter returned nothing usable (empty feed/list or parse failure)."""


class FetchBlocked(RuntimeError):
    """A URL (or a redirect target) points at a private/internal address."""


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _load_selection_policy():
    """Return ``(module, policy)``; ``(None, None)`` when selection is absent.

    A present-but-invalid policy fails visibly instead of silently reverting to
    the legacy path (fail closed). The import is lazy so a project copy without
    the optional module keeps working while selection is not configured.
    """
    if not (ROOT / "selection.yaml").exists():
        return None, None
    try:
        import subscription_selection as module
    except Exception as exc:  # pragma: no cover - broken installation
        raise RuntimeError(
            f"selection.yaml exists but subscription_selection.py is unavailable: {exc}"
        ) from exc
    return module, module.load_policy(ROOT)


@contextmanager
def state_lock():
    """Exclusive advisory lock on ``.state/.lock`` (parallel cron safety)."""
    lock_path = ROOT / ".state" / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _public_guard(url: str) -> None:
    try:
        assert_public_url(url)
    except ValueError as e:
        raise FetchBlocked(str(e)) from e


def _decode_body(raw: bytes, resp: requests.Response) -> str:
    """Decode a capped body without ever asking requests for ``.content``.

    Reading ``resp.apparent_encoding`` would drain the stream past our size cap,
    so the same detector (charset_normalizer) is applied to the bytes we already
    hold. A ``text/*`` response without a charset makes requests assume
    ISO-8859-1, which would mojibake Chinese pages; the declared meta charset and
    the detector therefore win over that guess.
    """
    header_enc = (resp.encoding or "").strip()
    meta_enc = ""
    m = re.search(rb"charset=[\"']?([\w.-]+)", raw[:4096], re.I)
    if m:
        meta_enc = m.group(1).decode("ascii", "ignore")

    latin_default = (header_enc or "").lower() in {"iso-8859-1", "latin-1"}
    candidates: list[str] = []
    if header_enc and not latin_default:
        candidates.append(header_enc)
    if meta_enc:
        candidates.append(meta_enc)
    if header_enc and latin_default:
        candidates.append(header_enc)
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(raw[:500_000]).best()
        if best is not None and best.encoding:
            candidates.append(best.encoding)
    except Exception:
        pass
    candidates += ["utf-8", "gb18030"]

    for enc in candidates:
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_text(url: str) -> str:
    """Fetch a page with SSRF guards, a redirect cap and a 10MB stream cap.

    ``127.0.0.1`` / ``::1`` is only reachable when the *initial* URL is loopback
    itself (local test fixtures); a public source can never be bounced into a
    loopback or cloud-metadata address. The environment proxy is ignored, which
    matches ``archive_url.py``.
    """
    try:
        allow_local = (urlsplit(url).hostname or "").lower() in LOOPBACK_HOSTS
    except ValueError:
        allow_local = False

    def guard(target: str) -> None:
        host = (urlsplit(target).hostname or "").lower()
        if allow_local and host in LOOPBACK_HOSTS:
            return
        _public_guard(target)

    session = requests.Session()
    session.trust_env = False

    def _get(start_url: str) -> str:
        current = start_url
        hops = 0
        while True:
            guard(current)
            resp = session.get(
                current, headers=HEADERS, timeout=40, allow_redirects=False, stream=True
            )
            try:
                location = resp.headers.get("Location") if resp.is_redirect else None
                if location:
                    hops += 1
                    if hops > MAX_REDIRECTS:
                        raise ValueError(f"too many redirects (>{MAX_REDIRECTS}): {url}")
                    current = urljoin(current, location)
                    continue
                resp.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_content(STREAM_CHUNK):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        raise ValueError(
                            f"response exceeded {MAX_RESPONSE_BYTES} bytes: {current}"
                        )
                    chunks.append(chunk)
                return _decode_body(b"".join(chunks), resp)
            finally:
                resp.close()

    last = None
    for attempt in range(3):
        try:
            return _get(url)
        except FetchBlocked:
            raise
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status is not None and status < 500:
                raise
            last = e
            time.sleep(1.5 * (attempt + 1))
        except ValueError as e:
            # Redirect loops and oversized bodies are deterministic: no retry.
            if "too many redirects" in str(e) or "response exceeded" in str(e):
                raise
            last = e
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]


def parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = dateparser.parse(value, fuzzy=True)
    except Exception:
        return None
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def struct_to_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime(*value[:6], tzinfo=timezone.utc).astimezone(TZ)
    except Exception:
        return None


def _future_cutoff(max_dt: datetime | None) -> datetime:
    """Dates beyond this are banner text ("2030 roadmap"), not publication dates."""
    return max_dt or (now() + timedelta(days=1))


def date_from_text(text: str, max_dt: datetime | None = None) -> datetime | None:
    """First YYYY-MM-DD / YYYY/MM/DD occurrence in free text (day precision)."""
    m = DATE_RE.search(text or "")
    if not m:
        return None
    try:
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=TZ)
    except ValueError:
        return None
    if dt > _future_cutoff(max_dt):
        return None
    return dt


def date_from_url(url: str, max_dt: datetime | None = None) -> datetime | None:
    path = urlsplit(url or "").path
    m = URL_DATE_RE.search(path)
    if not m:
        m = URL_DATE_RE.search(url or "")
    if not m:
        return None
    try:
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=TZ)
    except ValueError:
        return None
    if dt > _future_cutoff(max_dt):
        return None
    return dt


def resolve_published(
    *,
    url: str,
    dt: datetime | None,
    date_source: str,
    precision: str,
    text: str = "",
    fetched_at: datetime | None = None,
) -> tuple[str, str, str, bool]:
    """Return (published ISO, date_source, precision, published_unknown).

    Order: adapter date -> card/parent text -> URL -> fetch date (§2).
    """
    if dt is not None:
        prec = precision or ("day" if (dt.hour, dt.minute, dt.second) == (0, 0, 0) else "datetime")
        return iso(dt), date_source or "list", prec, False
    card_dt = date_from_text(text)
    if card_dt is not None:
        return iso(card_dt), "list", "day", False
    url_dt = date_from_url(url)
    if url_dt is not None:
        return iso(url_dt), "url", "day", False
    fetched = (fetched_at or now()).astimezone(TZ)
    fallback = fetched.replace(hour=0, minute=0, second=0, microsecond=0)
    return iso(fallback), "fetched", "day", True


def candidate(
    title: str,
    url: str,
    source: str,
    *,
    dt: datetime | None = None,
    date_source: str = "",
    precision: str = "",
    date_text: str = "",
    published_original: str = "",
    source_display_date: str = "",
    summary: str = "",
    fetched_at: datetime | None = None,
    **extra,
) -> dict:
    published, ds, prec, unknown = resolve_published(
        url=url,
        dt=dt,
        date_source=date_source,
        precision=precision,
        text=date_text,
        fetched_at=fetched_at,
    )
    orig = published_original or str(extra.pop("published_original", "") or "")
    disp = source_display_date or str(extra.pop("source_display_date", "") or "")
    d_text = date_text or str(extra.pop("date_text", "") or "")
    return {
        "title": (title or "").strip(),
        "url": url,
        "published": published,
        "source": source,
        "summary": (summary or "").strip()[:600],
        "date_source": ds,
        "date_precision": prec,
        "published_unknown": unknown,
        "date_text": d_text.strip(),
        "published_original": orig.strip(),
        "source_display_date": disp.strip(),
        **extra,
    }


def effective_dt(item: dict) -> datetime | None:
    """Comparison timestamp: day-precision dates count as end-of-day."""
    dt = parse_dt(str(item.get("published") or ""))
    if dt is None:
        return None
    if item.get("date_precision") == "day":
        return dt + DAY_PADDING
    return dt


def window_dt(item: dict) -> datetime | None:
    """Lower-bound timestamp: day-precision dates count as the start of the day.

    Window floors (36h / 30d) are lower bounds, so an unknown time is resolved
    pessimistically: an item only counts as inside a window when the *whole* day
    it belongs to is inside it. Comparing at end-of-day would silently stretch
    the 36h guard to ~60h and drag two-day-old items into a first run.
    """
    dt = parse_dt(str(item.get("published") or ""))
    if dt is None:
        return None
    if item.get("date_precision") == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return dt


def within_coverage(
    item: dict, wm: datetime | None, fetched_at: datetime, window_hours: int
) -> bool:
    """Is an unseen candidate inside this source's coverage window?

    With an archived water mark the sweep covers ``MAX_WATERMARK_LOOKBACK_DAYS``
    (the water mark itself is *not* a filter any more — only ``seen.json`` is).
    Without one the hard ``window_hours`` floor still applies, so a first run or
    a source that never archived cannot pull in years of feed history.

    A date that only carries day precision is compared in whole calendar days
    (``window_hours // 24`` days back, 30 days for the lookback), because there
    is no time to compare: at 09:00 a 36h floor reaches back to two days ago
    21:00, so "yesterday" is genuinely inside the window while "two days ago" is
    not. Comparing a day-precision value at end-of-day instead would stretch the
    36h guard to ~60h. A fetch-date fallback means "first seen now", so such an
    item is always inside the window.
    """
    if item.get("published_unknown") or str(item.get("date_source") or "").lower() == "fetched":
        return True
    if item.get("date_precision") == "day":
        dt = window_dt(item)
        if dt is None:
            return True
        limit = MAX_WATERMARK_LOOKBACK_DAYS if wm is not None else window_hours // 24
        return (fetched_at.date() - dt.date()).days <= limit
    dt = window_dt(item) or fetched_at
    if wm is not None:
        floor = fetched_at - timedelta(days=MAX_WATERMARK_LOOKBACK_DAYS)
    else:
        floor = fetched_at - timedelta(hours=window_hours)
    return dt >= floor


def pending_queue_dt(rec: dict) -> datetime | None:
    """When the record first entered the retry queue (never the article date)."""
    for key in PENDING_QUEUED_KEYS:
        dt = parse_dt(str(rec.get(key) or ""))
        if dt is not None:
            return dt
    return None


def pending_enqueued_at(rec: dict, fallback: datetime) -> datetime:
    return pending_queue_dt(rec) or fallback


def pending_sort_dt(rec: dict) -> datetime:
    """Newest-first ordering for the queue bound (unknowns sort last)."""
    return pending_queue_dt(rec) or datetime.min.replace(tzinfo=TZ)


def _rec_ts(rec: dict) -> datetime | None:
    for key in ("updated_at", "dropped_at", "parked_at"):
        dt = parse_dt(str(rec.get(key) or ""))
        if dt is not None:
            return dt
    return None


def _fingerprint(rec: dict) -> tuple[str, str, str]:
    """Identity of one write, used to spot a concurrent update of a record."""
    q = pending_queue_dt(rec)
    return (
        str(rec.get("updated_at") or ""),
        str(rec.get("reason") or ""),
        iso(q) if q else "",
    )


def merge_pending_record(prev: dict | None, new: dict) -> dict:
    """Same-key merge: newest payload wins, earliest queue time is kept."""
    if not prev:
        return dict(new)
    prev_ts, new_ts = _rec_ts(prev), _rec_ts(new)
    if prev_ts is not None and (new_ts is None or prev_ts > new_ts):
        merged = {**new, **prev}
    else:
        merged = {**prev, **new}
    q_prev, q_new = pending_queue_dt(prev), pending_queue_dt(new)
    if q_prev and q_new:
        merged["queued_at"] = iso(min(q_prev, q_new))
    elif q_prev or q_new:
        merged["queued_at"] = iso(q_prev or q_new)
    keys: list[str] = []
    for rec in (prev, new):
        for key in rec.get("dedupe_keys") or []:
            if key not in keys:
                keys.append(key)
    if keys:
        merged["dedupe_keys"] = keys
    return merged


def merge_pending_entries(
    *,
    disk_entries: dict,
    baseline_entries: dict,
    upserts: dict,
    remove_keys: set[str],
    limit: int = PENDING_LIMIT,
) -> tuple[dict, set[str], list[tuple[str, dict, str]]]:
    """Merge this run's delta into the *current* pending file.

    ``disk_entries`` must be a fresh read taken under the state lock.
    Returns ``(merged, applied_removals, evicted)``. A removal is only applied
    when the on-disk record still looks like the one this run based its decision
    on; if another process touched it, the record is kept (their write wins).
    ``evicted`` holds ``(key, record, reason)`` for entries dropped by the bound.
    """
    merged = {k: dict(v) for k, v in (disk_entries or {}).items()}
    applied: set[str] = set()
    for key in sorted(remove_keys or ()):
        if key not in merged:
            continue
        base = (baseline_entries or {}).get(key)
        if base is not None and base != merged[key]:
            continue  # someone else re-parked or modified it: keep their record
        del merged[key]
        applied.add(key)
    for key, rec in (upserts or {}).items():
        merged[key] = merge_pending_record(merged.get(key), rec)
    evicted: list[tuple[str, dict, str]] = []
    if limit and len(merged) > limit:
        ordered = sorted(merged.items(), key=lambda kv: pending_sort_dt(kv[1]), reverse=True)
        merged = dict(ordered[:limit])
        evicted = [(key, rec, "over_limit") for key, rec in ordered[limit:]]
    return merged, applied, evicted


def merge_source_record(prev: dict | None, delta: dict, fetched_at: datetime) -> dict:
    """Merge one source's delta into the record freshly re-read from disk.

    ``prev`` must come from the lock-held re-read, so a failure streak counts
    every run exactly once even when two runs overlap. If disk already holds a
    *newer* run for this source, its status block is left alone (an old run must
    not overwrite a newer ``last_run``/``observed_newest``).
    """
    prev = dict(prev or {})
    prev_run = parse_dt(str(prev.get("last_run") or ""))
    if prev_run is not None and prev_run > fetched_at:
        return prev
    status = str(delta.get("status") or "error")
    if delta.get("mode"):
        prev["last_mode"] = delta["mode"]
    prev["last_run"] = iso(fetched_at)
    prev["last_status"] = status
    if status == "ok":
        prev["initialized"] = True
        prev["failures"] = 0
        prev["consecutive_failures"] = 0
        prev["needs_attention"] = False
        prev.pop("last_error", None)
    else:
        failures = int(prev.get("failures") or 0) + 1
        prev["failures"] = failures
        prev["consecutive_failures"] = failures
        prev["needs_attention"] = failures >= NEEDS_ATTENTION_STREAK
        prev["last_error"] = str(delta.get("error") or "")
    if delta.get("observed_newest"):
        prev["observed_newest"] = delta["observed_newest"]
    prev["last_archived_published"] = str(delta.get("watermark") or "")
    if delta.get("watermark"):
        prev["last_newest"] = delta["watermark"]
    return prev


def is_safe_cache_name(name: str | None) -> bool:
    if not name or not isinstance(name, str):
        return False
    if "/" in name or "\\" in name or ".." in name or "\0" in name:
        return False
    if Path(name).name != name:
        return False
    return name.endswith(".md") or name.endswith(".txt")


def pending_candidate(rec: dict, key: str) -> dict:
    """Rebuild a selectable candidate from a stored pending record.

    This is what keeps an item alive after it rolls out of the upstream feed:
    url/title/published/source/extractor/date metadata are all reused as stored,
    so no network request to the list page is needed for the retry.
    """
    item = {
        k: v for k, v in rec.items()
        if not k.startswith("_") and k not in ("reason", "dropped_at", "parked_at")
    }
    item.setdefault("source", rec.get("source") or rec.get("source_id") or "")
    item["from_pending"] = True
    item["_key"] = key

    cache_name = rec.get("cache_name")
    if cache_name and is_safe_cache_name(cache_name):
        cpath = ROOT / ".state" / "candidates" / cache_name
        if cpath.is_file():
            item["content_file"] = str(cpath)
            item["cache_name"] = cache_name
        else:
            item["content_file"] = None
            item["cache_name"] = cache_name
    elif rec.get("content_file"):
        cf = Path(rec["content_file"])
        if cf.is_file():
            item["content_file"] = str(cf)
        else:
            item["content_file"] = None
    else:
        item["content_file"] = None

    return item


def select_for_source(
    *,
    sid: str,
    items: list[dict],
    pending_for_source: dict[str, dict],
    seen_keys: set[str],
    wm: datetime | None,
    fetched_at: datetime,
    window_hours: int,
    per_source_cap: int,
    backfill: int,
    suppressed_keys: set[str] | None = None,
    protected_keys: set[str] | None = None,
    selection_active: bool = False,
) -> dict:

    """Choose this source's candidates (pure: no I/O, no clock beyond ``fetched_at``).

    Ordering: pending records first (they are already owed), then unseen fresh
    items inside the coverage window, newest first. Only ``seen.json`` filters
    archived work; the water mark decides *how wide* the sweep is, never whether
    an individual unseen item is eligible.

    ``suppressed_keys`` are terminal selection identities (reject/defer/archived
    in the ledger). They are removed from fresh work and exited from pending so a
    stale queue delta can never resurrect rejected work. ``protected_keys`` are
    owed open identities (accepted admissions, judgment-budget waits): the
    undecided-retry TTL never discards them.
    """
    suppressed = suppressed_keys or set()
    protected = protected_keys or set()
    exited_this_run: set[str] = set()
    fresh = [
        it for it in items
        if not is_seen(it, seen_keys) and it.get("_key") not in suppressed
    ]
    fresh_by_key = {it.get("_key"): it for it in fresh}
    exits: list[dict] = []
    remove_keys: set[str] = set()
    pending_pool: list[dict] = []
    pending_kept: dict[str, dict] = {}

    ranked = sorted(pending_for_source.items(), key=lambda kv: pending_sort_dt(kv[1]),
                    reverse=True)
    for key, rec in ranked:
        url = str(rec.get("url") or "")
        if key in suppressed:
            exits.append({"key": key, "url": url, "source_id": sid,
                          "reason": "selection_terminal"})
            remove_keys.add(key)
            exited_this_run.add(key)
            continue
        if is_seen(rec, seen_keys):
            exits.append({"key": key, "url": url, "source_id": sid, "reason": "seen"})
            remove_keys.add(key)
            exited_this_run.add(key)
            continue
        enqueued = pending_enqueued_at(rec, fetched_at)
        if key not in protected and enqueued < fetched_at - timedelta(days=PENDING_MAX_AGE_DAYS):
            exits.append({
                "key": key,
                "url": url,
                "source_id": sid,
                "reason": "expired",
                "queued_at": iso(enqueued),
            })
            remove_keys.add(key)
            exited_this_run.add(key)
            continue
        if backfill:
            # A backfill is an explicit "newest N now" action; parked items keep
            # their slot for the next normal run instead of being re-picked here.
            pending_kept[key] = rec
            continue
        item = fresh_by_key.get(key) or pending_candidate(rec, key)
        item["_queued_at"] = iso(enqueued)
        item["from_pending"] = True
        pending_pool.append(item)

    coverage_skipped = 0
    dropped: list[tuple[dict, str]] = []
    if backfill:
        selected = fresh[:backfill]
    else:
        queued_keys = {it["_key"] for it in pending_pool}
        coverage_pool: list[dict] = []
        for it in fresh:
            if it.get("_key") in queued_keys:
                continue
            if selection_active and it.get("_key") in exited_this_run:
                # A selection-active run must not re-park an identity it just
                # exited (expired/seen/terminal); legacy mode keeps the historic
                # same-run rediscovery behaviour.
                continue
            if within_coverage(it, wm, fetched_at, window_hours):
                coverage_pool.append(it)
            else:
                coverage_skipped += 1
        pool = pending_pool + coverage_pool
        selected = pool[:per_source_cap]
        dropped = [(it, "per_source_cap") for it in pool[per_source_cap:]]

    return {
        "selected": selected,
        "dropped": dropped,
        "exits": exits,
        "remove_keys": remove_keys,
        "pending_kept": pending_kept,
        "pending_selected": len(pending_pool),
        "coverage_skipped": coverage_skipped,
        "suppressed_skipped": sum(
            1 for it in items if it.get("_key") in suppressed and not is_seen(it, seen_keys)
        ),
        "fresh": fresh,
    }


# ---------------------------------------------------------------- adapters


def openai_rss(src: dict, fetched_at: datetime) -> list[dict]:
    """Generic RSS adapter: discovery only; extractor comes from the source."""
    raw = fetch_text(src["feed"])
    feed = feedparser.parse(raw)
    entries = list(feed.entries or [])
    if not entries:
        raise SourceEmpty(
            f"RSS 解析失败或为空（bozo={bool(feed.bozo)}, entries=0）"
        )
    if feed.bozo:
        log(f"warning: RSS parse warning (bozo={feed.bozo}), entries={len(entries)}; continuing")
    out = []
    for e in entries:
        link = (e.get("link") or "").strip()
        if not link:
            continue
        raw_pub = (
            e.get("published")
            or e.get("pubDate")
            or e.get("updated")
            or ""
        ).strip()
        dt = struct_to_dt(e.get("published_parsed") or e.get("updated_parsed"))
        out.append(candidate(
            e.get("title", ""),
            link,
            src["label"],
            dt=dt,
            date_source="feed",
            precision="datetime",
            date_text=raw_pub or e.get("summary", ""),
            published_original=raw_pub,
            source_display_date="",
            summary=e.get("summary", ""),
            fetched_at=fetched_at,
            extractor=src.get("extractor") or "web_extract",
            language=src.get("language") or "en",
            source_type=src.get("source_type") or "vendor_blog",
        ))
    return out


rss_feed = openai_rss


def anthropic_html(src: dict, fetched_at: datetime) -> list[dict]:
    html = fetch_text(src["list_url"])
    s = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in s.select("li a[href^='/news/']"):
        classes = " ".join(a.get("class") or [])
        if "title-link" not in classes:
            continue
        href = a.get("href", "")
        if href in seen:
            continue
        seen.add(href)
        title = a.get_text(" ", strip=True)
        li = a.find_parent("li")
        text = li.get_text(" ", strip=True) if li else title
        m = EN_DATE_RE.search(text)
        raw_date = m.group(0) if m else ""
        dt = parse_dt(raw_date) if m else None
        cal_date = dt.strftime("%Y-%m-%d") if dt else ""
        out.append(candidate(
            title,
            urljoin("https://www.anthropic.com", href),
            src["label"],
            dt=dt,
            date_source="list",
            precision="day",
            date_text=raw_date or text,
            published_original=raw_date,
            source_display_date=cal_date,
            summary=text,
            fetched_at=fetched_at,
            extractor="direct",
            language="en",
            source_type="vendor_blog",
        ))
    if not out:
        raise SourceEmpty("列表页未解析到任何文章条目")
    return out


def kimi_html(src: dict, fetched_at: datetime) -> list[dict]:
    html = fetch_text(src["list_url"])
    s = BeautifulSoup(html, "html.parser")
    main = s.find("main") or s
    out, seen = [], set()
    for a in main.select("a[href^='/en/blog/'], a[href^='/blog/']"):
        href = a.get("href", "")
        if href in seen:
            continue
        seen.add(href)
        card = a.find_parent("div", class_=re.compile("menu-card")) or a.parent
        text = card.get_text("\n", strip=True) if card else a.get_text(" ", strip=True)
        title = a.get("aria-label") or ""
        if not title and card:
            h = card.select_one("h4, .card-title")
            title = h.get_text(" ", strip=True) if h else ""
        title = title or a.get_text(" ", strip=True)
        m = DATE_RE.search(text)
        raw_date = "-".join(m.groups()) if m else ""
        dt = parse_dt(raw_date) if m else None
        cal_date = dt.strftime("%Y-%m-%d") if dt else ""
        out.append(candidate(
            title,
            urljoin("https://www.kimi.com", href),
            src["label"],
            dt=dt,
            date_source="list",
            precision="day",
            date_text=raw_date or text,
            published_original=raw_date,
            source_display_date=cal_date,
            summary=text,
            fetched_at=fetched_at,
            extractor="direct",
            language="en",
            source_type="vendor_blog",
        ))
    if not out:
        raise SourceEmpty("列表页未解析到任何文章条目")
    return out


def glm_section_title(sec) -> str:
    node = sec.select_one('[data-component-part="update-description"]')
    if node:
        text = node.get_text(" ", strip=True)
        if text:
            return text
    for tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        node = sec.select_one(tag)
        if node:
            text = node.get_text(" ", strip=True)
            if text:
                return text
    return ""


class CandidateList(list):
    """List of candidates that also carries explicit rejections metadata."""

    def __init__(self, items=(), rejections=None):
        super().__init__(items)
        self.rejections = list(rejections or [])


def glm_docs(src: dict, fetched_at: datetime) -> list[dict]:
    """GLM release notes.

    Sections are real anchors on the page, so the URL uses ``#<section id>``.
    Sections lacking a real id are rejected explicitly (no anchor fabricated).
    Sections with a real id but no date enter candidates with fetched fallback
    (published_unknown=True, date_precision=day).
    The adapter writes nothing to disk (§6, P2 #10): it hands the whole-page HTML
    back through ``_raw_html`` and the main loop persists a single snapshot only
    when a section of this page is actually selected or parked.
    """
    list_url = src["list_url"]
    html = fetch_text(list_url)
    s = BeautifulSoup(html, "html.parser")
    sections = s.select("div.update")
    if not sections:
        raise SourceEmpty("文档更新页未解析到任何 div.update section")

    out: list[dict] = []
    rejections: list[dict] = []
    seen_cache_names: set[str] = set()
    raw_rel = f".state/raw/{fetched_at.strftime('%Y-%m-%d')}/glm.html.gz"
    for sec in sections:
        sec_id = (sec.get("id") or "").strip().lstrip("#")
        title = glm_section_title(sec).strip()
        body = sec.select_one('[data-component-part="update-content"]') or sec
        content_md = md(str(body), heading_style="ATX")
        content_md = re.sub(r"\n{3,}", "\n\n", content_md).strip()

        # Date extraction: read update-label tag or explicit date in title ONLY.
        # Do NOT scan the whole body text to guess publication date.
        date_node = sec.select_one('[data-component-part="update-label"]')
        date_text = date_node.get_text(" ", strip=True) if date_node else ""
        dt = date_from_text(date_text)
        raw_date_str = date_text
        if dt is None:
            dt = date_from_text(title)
            if dt is not None:
                m = DATE_RE.search(title)
                raw_date_str = m.group(0) if m else ""

        # Section without real id: do not fabricate anchor!
        if not sec_id:
            rejections.append({
                "title": title or "未命名章节",
                "reason": "missing_section_id",
                "message": "section 缺少真实 id 属性，不编造锚点",
                "date": raw_date_str,
            })
            continue

        anchor = sec_id
        candidate_title = title or f"GLM 更新 {anchor}"
        url = f"{list_url}#{anchor}"

        if dt is not None:
            date_str = dt.strftime("%Y-%m-%d")
            slug = slugify(candidate_title, 40)
            base_cache_name = f"glm-{date_str}-{slugify(anchor, 24)}.md"
            cache_name = base_cache_name
            counter = 1
            while cache_name in seen_cache_names:
                counter += 1
                stem, ext = os.path.splitext(base_cache_name)
                cache_name = f"{stem}-{counter}{ext}"
            seen_cache_names.add(cache_name)

            out.append(candidate(
                candidate_title,
                url,
                src.get("label", "GLM"),
                dt=dt,
                date_source="list",
                precision="day",
                date_text=raw_date_str,
                published_original=raw_date_str,
                source_display_date=date_str,
                summary=sec.get_text("\n", strip=True),
                fetched_at=fetched_at,
                extractor="direct",
                language="zh",
                source_type="docs",
                raw_file=raw_rel,
                _content_md=content_md,
                _cache_name=cache_name,
                _raw_html=html,
                dedupe_keys=[
                    f"{list_url}#{date_str}-{slug}",
                    f"{list_url}#{anchor}-{slug}",
                    f"{list_url}#{date_str}",
                    f"{list_url}#{anchor}",
                ],
            ))
        else:
            # Undated section with real id:
            # fetched fallback, published_unknown=True, date_precision=day
            fallback_dt = fetched_at.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            date_str = fetched_at.strftime("%Y-%m-%d")
            base_cache_name = f"glm-{date_str}-{slugify(anchor, 24)}.md"
            cache_name = base_cache_name
            counter = 1
            while cache_name in seen_cache_names:
                counter += 1
                stem, ext = os.path.splitext(base_cache_name)
                cache_name = f"{stem}-{counter}{ext}"
            seen_cache_names.add(cache_name)

            out.append(candidate(
                candidate_title,
                url,
                src.get("label", "GLM"),
                dt=fallback_dt,
                date_source="fetched",
                precision="day",
                date_text="",
                published_original="",
                source_display_date="",
                summary=sec.get_text("\n", strip=True),
                fetched_at=fetched_at,
                extractor="direct",
                language="zh",
                source_type="docs",
                raw_file=raw_rel,
                published_unknown=True,
                _content_md=content_md,
                _cache_name=cache_name,
                _raw_html=html,
                dedupe_keys=[
                    f"{list_url}#{anchor}",
                ],
            ))

    if not out and not rejections:
        raise SourceEmpty("文档更新页未解析到任何章节")
    elif not out and rejections:
        raise SourceEmpty(f"文档更新页未解析到有效章节（{len(rejections)} 个章节因无 id 锚点被拒绝）")
    return CandidateList(out, rejections=rejections)


ADAPTERS = {
    "openai_rss": openai_rss,
    "rss_feed": rss_feed,
    "anthropic_html": anthropic_html,
    "kimi_html": kimi_html,
    "glm_docs": glm_docs,
}


# ---------------------------------------------------------------- helpers


def source_hosts(src: dict) -> set[str]:
    hosts = set()
    for key in ("feed", "list_url", "homepage"):
        url = src.get(key)
        if not url:
            continue
        host = urlsplit(str(url)).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host:
            hosts.add(host)
    return hosts


def load_archived_watermarks(sources: list[dict]) -> dict[str, datetime]:
    """Newest archived ``published`` per source, derived from ``library/``.

    The water mark is only produced by archived entries, so it cannot advance
    during ``fetch`` (§3). Entries whose date is a fetch-date fallback
    (``published_unknown`` / ``date_source: fetched``) and entries carrying an
    impossible future date are ignored, otherwise one poisoned entry would push
    the water mark into the future and starve the source forever (P2 #5).
    """
    lib = ROOT / "library"
    result: dict[str, datetime] = {}
    if not lib.exists():
        return result
    future_cutoff = now() + timedelta(days=1)
    hosts = {s["id"]: source_hosts(s) for s in sources}
    labels = {
        s["id"]: (s.get("label") or "").split(" / ")[0].strip().lower() for s in sources
    }
    for path in lib.rglob("entry.md"):
        try:
            fm, _ = read_entry(path)
        except Exception:
            continue
        if fm.get("published_unknown") is True:
            continue
        if str(fm.get("date_source") or "").strip().lower() == "fetched":
            continue
        url = str(fm.get("canonical_url") or fm.get("url") or "")
        dt = parse_dt(str(fm.get("published") or ""))
        if not url or dt is None or dt > future_cutoff:
            continue
        host = urlsplit(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        entry_source = str(fm.get("source") or "").strip().lower()
        entry_st = str(fm.get("source_type") or "").strip().lower()
        for sid in hosts:
            matched = any(
                host == h or host.endswith("." + h) or h.endswith("." + host)
                for h in hosts[sid]
            )
            if (
                not matched
                and labels.get(sid)
                and host != "mp.weixin.qq.com"
                and entry_st not in ("wechat", "tech_blog", "other")
            ):
                lbl = labels[sid]
                matched = (
                    entry_source == lbl
                    or entry_source.startswith(lbl + " / ")
                    or entry_source.startswith(lbl + " ")
                )
            if matched and (sid not in result or dt > result[sid]):
                result[sid] = dt
    return result


def candidate_keys(item: dict) -> list[str]:
    keys = [dedupe_key(item.get("url", ""))]
    for extra in item.get("dedupe_keys") or []:
        k = dedupe_key(extra)
        if k and k not in keys:
            keys.append(k)
    return keys


def internal_key(url: str) -> str:
    """Identity inside one source.

    Anchored documents (docs sections) are distinct entries even when the host
    is not in ``dedupe_key``'s fragment allow-list, so the fragment is kept.
    Seen/dup-archive matching still uses ``dedupe_key`` so it stays in sync with
    ``archive_url.py``.
    """
    if urlsplit(url or "").fragment:
        return canonicalize_url(url, keep_fragment=True)
    return dedupe_key(url)


def is_seen(item: dict, seen_keys: set[str]) -> bool:
    return any(k in seen_keys for k in candidate_keys(item))


def build_pending_record(
    item: dict, sid: str, reason: str, *, queued_at: datetime | None = None
) -> dict:
    """Snapshot a candidate into a pending record.

    ``queued_at`` is the *first* enqueue time and is therefore passed in by the
    caller when the item was already parked: re-parking must refresh the payload
    without restarting the 30-day life of the record.
    """
    rec = dict(item)
    rec.pop("_content_md", None)
    rec.pop("_raw_html", None)
    rec.pop("_key", None)
    # Keep the cache file name so cleanup can protect a parked item.
    cache_name = item.get("_cache_name") or item.get("cache_name") or ""
    rec["cache_name"] = cache_name
    rec.pop("_cache_name", None)
    rec.pop("_queued_at", None)
    rec["source_id"] = sid
    rec["reason"] = reason
    rec["dropped_at"] = iso()
    rec["updated_at"] = rec["dropped_at"]
    rec["queued_at"] = iso(queued_at or now())
    rec["dedupe_keys"] = [
        k for k in candidate_keys(rec) if k != dedupe_key(rec.get("url", ""))
    ]
    return rec


def soft_delete_file(path: Path) -> bool:
    """Soft-delete a file or directory into <ROOT>/trash/<stamp>-<name>; never physically deletes."""
    try:
        trash_dir = ROOT / "trash"
        trash_dir.mkdir(parents=True, exist_ok=True)
        stamp = now().strftime("%Y%m%d-%H%M%S")
        target = trash_dir / f"{stamp}-{path.name}"
        counter = 1
        while target.exists():
            counter += 1
            target = trash_dir / f"{stamp}-{counter}-{path.name}"
        shutil.move(str(path), str(target))
        return True
    except OSError:
        return False


def ensure_candidate_cache(
    cache_dir: Path,
    base_name: str,
    content_md: str,
    *,
    dry_run: bool = False,
) -> tuple[str, Path]:
    """Store content_md in cache_dir under a safe, collision-free name.

    If base_name exists with identical content, reuse it without overwriting.
    If base_name exists with different content, append content hash to avoid
    overwriting existing or referenced bytes.
    """
    if not is_safe_cache_name(base_name):
        base_name = f"doc-{slugify(base_name, 24)}.md"
        if not is_safe_cache_name(base_name):
            base_name = "doc-content.md"

    stem, ext = os.path.splitext(base_name)
    cache_path = cache_dir / base_name

    if cache_path.exists():
        existing = None
        try:
            existing = cache_path.read_text(encoding="utf-8")
        except Exception:
            existing = None
        if existing != content_md:
            chash = hashlib.sha256(content_md.encode("utf-8")).hexdigest()[:8]
            base_name = f"{stem}-{chash}{ext}"
            cache_path = cache_dir / base_name

    if not dry_run:
        if not cache_path.exists():
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(content_md, encoding="utf-8")
        else:
            try:
                if cache_path.read_text(encoding="utf-8") != content_md:
                    cache_path.write_text(content_md, encoding="utf-8")
            except Exception:
                pass

    return base_name, cache_path


def ensure_raw_snapshot(
    root: Path,
    proposed_rel: str,
    html: str,
    *,
    dry_run: bool = False,
) -> tuple[str, Path]:
    """Store raw html snapshot under a safe, non-overwriting path.

    If proposed_rel exists with identical content, reuse it.
    If proposed_rel exists with different content on the same day,
    use a hashed filename to preserve the existing snapshot.
    """
    raw_path = root / proposed_rel
    if raw_path.exists():
        same = False
        try:
            with gzip.open(raw_path, "rt", encoding="utf-8") as f:
                same = (f.read() == html)
        except Exception:
            same = False
        if not same:
            chash = hashlib.sha256(html.encode("utf-8")).hexdigest()[:8]
            p = Path(proposed_rel)
            name_parts = p.name.split(".", 1)
            new_name = f"{name_parts[0]}-{chash}.{name_parts[1]}"
            proposed_rel = str(p.parent / new_name)
            raw_path = root / proposed_rel

    if not dry_run:
        if not raw_path.exists():
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(raw_path, "wt", encoding="utf-8") as f:
                f.write(html)

    return proposed_rel, raw_path


def cleanup_candidate_cache(keep: set[str]) -> list[str]:
    """Drop stale GLM section caches (§6).

    ``keep`` holds every cache name that must be retained (live page + all
    current pending entries). Stale/orphan files are soft-moved to trash.
    """
    cache_dir = ROOT / ".state" / "candidates"
    if not cache_dir.exists():
        return []
    removed = []
    cutoff = now() - timedelta(days=CANDIDATE_RETENTION_DAYS)
    for path in cache_dir.iterdir():
        if not path.is_file() or path.suffix not in (".md", ".txt"):
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=TZ)
        except OSError:
            continue
        if path.name in keep:
            continue
        if path.name.startswith("glm-"):
            pass
        else:
            if mtime >= cutoff:
                continue
        if soft_delete_file(path):
            removed.append(path.name)
    return removed


def cleanup_raw_snapshots(
    days: int = RAW_SNAPSHOT_RETENTION_DAYS,
    protected: set[str] | None = None,
) -> list[str]:
    """Soft-delete .state/raw/<date>/ snapshots older than days (P2 #10).

    Snapshots referenced in ``protected`` are NEVER pruned.
    Pruned files are soft-moved to trash.
    """
    raw_root = ROOT / ".state" / "raw"
    if not raw_root.exists():
        return []
    protected = protected or set()
    cutoff = now() - timedelta(days=days)
    removed: list[str] = []
    for day_dir in sorted(raw_root.iterdir()):
        if not day_dir.is_dir():
            continue
        day_dt = None
        try:
            day_dt = datetime.strptime(day_dir.name, "%Y-%m-%d").replace(tzinfo=TZ)
        except ValueError:
            try:
                day_dt = datetime.fromtimestamp(day_dir.stat().st_mtime, tz=TZ)
            except OSError:
                continue
        if day_dt >= cutoff:
            continue
        for path in sorted(day_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_file() or path.is_symlink():
                rel = str(path.relative_to(ROOT))
                if rel in protected or path.name in protected:
                    continue
                if soft_delete_file(path):
                    removed.append(rel)
        try:
            if not any(day_dir.iterdir()):
                day_dir.rmdir()
                removed.append(str(day_dir.relative_to(ROOT)) + "/")
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------- main


def commit_states(
    *,
    fetched_at: datetime,
    pending_baseline: dict,
    upserts: dict,
    remove_keys: set[str],
    deltas: dict[str, dict],
    dry_run: bool,
    extra_keep_caches: set[str] | None = None,
    extra_keep_raw: set[str] | None = None,
    selection=None,
    next_rotation: int | None = None,
) -> dict:
    """Merge this run's delta into ``pending.json`` / ``sources.json``.

    The files are re-read *inside* ``.state/.lock`` at commit time (the network
    phase never holds the lock), so a concurrent run's additions, another
    source's state and a newer ``last_run`` survive. In ``--dry-run`` the same
    arithmetic runs against the current files without lock or write, which is
    what makes the dry-run report truthful.
    """
    pending_path = ROOT / ".state" / "pending.json"
    sources_path = ROOT / ".state" / "sources.json"

    def _merge(disk_pending: dict, disk_sources: dict) -> dict:
        merged, removals, evicted = merge_pending_entries(
            disk_entries=disk_pending.get("entries") or {},
            baseline_entries=pending_baseline,
            upserts=upserts,
            remove_keys=remove_keys,
        )
        selection_stats: dict = {}
        if selection is not None:
            # Re-read the ledger while this process holds .state/.lock, so a
            # rejection written by another run cannot be resurrected by a stale
            # pending delta, and crash-lost queue work is re-derived.
            ledger = selection["module"].load_ledger(ROOT)
            seen_under_lock = set(
                (load_json(ROOT / ".state" / "seen.json", {"entries": {}})
                 .get("entries") or {}).keys()
            )
            selection_stats = selection["module"].apply_commit(
                ledger,
                selection["policy"],
                run_gen=selection["run_gen"],
                candidates=selection["candidates"],
                observed=selection.get("observed"),
                merged=merged,
                evicted=evicted,
                dry_run=dry_run,
                seen_keys=seen_under_lock,
                expired_keys=set(selection.get("expired_keys") or ()),
            )
            if not dry_run:
                selection["module"].save_ledger(ROOT, ledger)
        counts: dict[str, int] = {}
        for rec in merged.values():
            sid = str(rec.get("source_id") or "")
            counts[sid] = counts.get(sid, 0) + 1

        state = dict(disk_sources or {})
        if next_rotation is not None:
            state["rotation"] = int(next_rotation)
        records = dict(state.get("sources") or {})
        stamp = iso(fetched_at)
        for sid, delta in deltas.items():
            merged_rec = merge_source_record(records.get(sid), delta, fetched_at)
            if merged_rec.get("last_run") == stamp:
                # Our status block landed: the queue size belongs to this run.
                merged_rec["pending"] = counts.get(sid, 0)
            records[sid] = merged_rec
        state["sources"] = records
        state["updated_at"] = iso()
        pending_out = {"updated_at": iso(), "entries": merged, "schema": 2}
        return {
            "pending": merged,
            "removals": removals,
            "evicted": evicted,
            "sources": state,
            "pending_out": pending_out,
            "selection": selection_stats,
        }

    if dry_run:
        result = _merge(
            load_json(pending_path, {"entries": {}}),
            load_json(sources_path, {"sources": {}}),
        )
        return result

    with state_lock():
        result = _merge(
            load_json(pending_path, {"entries": {}}),
            load_json(sources_path, {"sources": {}}),
        )
        merged = result["pending"]
        keep_caches = set(extra_keep_caches or ())
        keep_raw = set(extra_keep_raw or ())
        for rec in merged.values():
            if rec.get("cache_name"):
                keep_caches.add(str(rec["cache_name"]))
            if rec.get("content_file"):
                keep_caches.add(Path(rec["content_file"]).name)
            if rec.get("raw_file"):
                keep_raw.add(str(rec["raw_file"]))
        for rec in pending_baseline.values():
            if rec.get("cache_name"):
                keep_caches.add(str(rec["cache_name"]))
            if rec.get("content_file"):
                keep_caches.add(Path(rec["content_file"]).name)
            if rec.get("raw_file"):
                keep_raw.add(str(rec["raw_file"]))

        cleanup_candidate_cache(keep_caches)
        cleanup_raw_snapshots(RAW_SNAPSHOT_RETENTION_DAYS, protected=keep_raw)

        write_json_atomic(pending_path, result["pending_out"])
        write_json_atomic(sources_path, result["sources"])
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="", help="only one source id")
    ap.add_argument("--window-hours", type=int, default=0)
    ap.add_argument("--backfill", type=int, default=0, help="override backfill count")
    ap.add_argument("--max", type=int, default=0, help="global cap (round-robin)")
    ap.add_argument("--dry-run", action="store_true", help="do not update source state")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    try:
        selection_module, selection_policy = _load_selection_policy()
    except Exception as e:
        print(f"[fetch] selection policy error: {e}", file=sys.stderr)
        return 2
    suppressed_keys: set[str] = set()
    protected_keys: set[str] = set()
    if selection_policy is not None and selection_module is not None:
        try:
            ledger_snapshot = selection_module.load_ledger(ROOT)
            suppressed_keys = selection_module.suppressed_keys(ledger_snapshot)
            protected_keys = selection_module.owed_keys(ledger_snapshot)
        except Exception as e:
            print(f"[fetch] selection ledger error: {e}", file=sys.stderr)
            return 3


    fetched_at = now()
    cfg = load_yaml(ROOT / "sources.yaml")
    defaults = cfg.get("defaults", {})
    window_hours = args.window_hours or int(defaults.get("window_hours", 36))
    per_source_cap = int(defaults.get("max_items_per_source", 8))
    if selection_policy is not None:
        # The policy budget replaces the generic per-source default; an explicit
        # --max below still tightens the global cap.
        per_source_cap = int(selection_policy.budgets["per_source"])
    backfill_default = int(defaults.get("backfill_items", 2))

    # Baseline snapshot, read once for the whole (unlocked) network phase. The
    # commit phase re-reads both files because another run may have written in
    # the meantime.
    state = load_json(ROOT / ".state" / "sources.json", {"sources": {}})
    state.setdefault("sources", {})
    pending_state = load_json(ROOT / ".state" / "pending.json", {"entries": {}})
    pending_baseline: dict[str, dict] = dict(pending_state.get("entries") or {})
    seen = load_seen()
    seen_keys = set((seen.get("entries") or {}).keys())

    all_sources = [s for s in cfg.get("sources", []) if s.get("enabled", True)]
    if args.source:
        known = {s.get("id") for s in cfg.get("sources", [])}
        if args.source not in known:
            print(f"[fetch] 未知的源 id：{args.source}（可选：{', '.join(sorted(known))}）",
                  file=sys.stderr)
            return 2
        if args.source not in {s.get("id") for s in all_sources}:
            print(f"[fetch] 源 {args.source} 已禁用（enabled: false）", file=sys.stderr)
            return 2
    watermarks = load_archived_watermarks(all_sources)

    report: list[dict] = []
    per_source_selected: dict[str, list[dict]] = {}
    per_source_report: dict[str, dict] = {}
    per_source_dropped: dict[str, list[tuple[dict, str]]] = {}
    upserts: dict[str, dict] = {}
    planned_exits: list[dict] = []
    remove_keys: set[str] = set()
    deltas: dict[str, dict] = {}
    processed: list[str] = []
    glm_section_names: set[str] = set()
    observed_candidates: list[dict] = []

    for src in cfg.get("sources", []):
        if not src.get("enabled", True):
            continue
        sid = src.get("id", "")
        if args.source and sid != args.source:
            continue
        processed.append(sid)
        st = state["sources"].get(sid) or {}
        initialized = bool(st.get("initialized"))
        wm = watermarks.get(sid)
        wm_iso = iso(wm) if wm else ""

        # Every parked record of this source, straight from the baseline read.
        pending_for_source: dict[str, dict] = {
            k: v for k, v in pending_baseline.items() if v.get("source_id") == sid
        }

        backfill = args.backfill if args.backfill else (0 if initialized else backfill_default)
        mode = f"backfill:{backfill}" if backfill else f"window:{window_hours}h"
        coverage_window = (
            "backfill" if backfill else ("watermark:30d" if wm else f"window:{window_hours}h")
        )
        status = "ok"
        error = ""
        empty_reason = ""
        found = 0
        found_raw = 0
        skipped = 0
        no_date = 0
        uniq: list[dict] = []
        selection = {
            "selected": [],
            "dropped": [],
            "exits": [],
            "remove_keys": set(),
            "pending_selected": 0,
            "coverage_skipped": 0,
        }

        items: list[dict] = []
        rejections: list[dict] = []
        try:
            adapter = ADAPTERS.get(src.get("adapter", ""))
            if adapter is None:
                raise ValueError(f"unknown adapter: {src.get('adapter')}")
            items = adapter(src, fetched_at)
            rejections = getattr(items, "rejections", [])
            found_raw = len(items)

            uniq, keys = [], set()
            for it in items:
                if not it.get("url") or not it.get("title"):
                    continue
                k = internal_key(it["url"])
                if k in keys:
                    continue
                keys.add(k)
                it["_key"] = k
                it["source_id"] = sid
                uniq.append(it)
            if not uniq:
                raise SourceEmpty("解析到的条目全部缺少标题或链接")
            uniq.sort(key=lambda x: effective_dt(x) or fetched_at, reverse=True)
            for it in uniq:
                if it.get("_cache_name"):
                    glm_section_names.add(it["_cache_name"])
            observed_candidates.extend(uniq)
            found = len(uniq)
            no_date = sum(1 for it in uniq if it.get("published_unknown"))
            skipped = sum(1 for it in uniq if is_seen(it, seen_keys))
            selection = select_for_source(
                sid=sid,
                items=uniq,
                pending_for_source=pending_for_source,
                seen_keys=seen_keys,
                wm=wm,
                fetched_at=fetched_at,
                window_hours=window_hours,
                per_source_cap=per_source_cap,
                backfill=backfill,
                suppressed_keys=suppressed_keys,
                protected_keys=protected_keys,
                selection_active=selection_policy is not None,
            )
        except SourceEmpty as e:
            status = "empty"
            empty_reason = str(e)
            error = empty_reason
        except Exception as e:
            status = "error"
            error = str(e)

        selected: list[dict] = selection["selected"]
        dropped: list[tuple[dict, str]] = selection["dropped"]
        per_source_selected[sid] = selected
        per_source_dropped[sid] = dropped
        no_date_selected = sum(1 for it in selected if it.get("published_unknown"))
        per_source_report[sid] = {
            "id": sid,
            "label": src.get("label", sid),
            "status": status,
            "mode": mode,
            "coverage_window": coverage_window,
            "found": found,
            "found_raw": found_raw,
            "skipped": skipped,
            "selected": 0,          # filled after the global round-robin
            "dropped_by_max": len(dropped),
            "no_date": no_date,
            "no_date_selected": no_date_selected,
            "coverage_skipped": selection["coverage_skipped"],
            "pending": len(pending_for_source),
            "pending_selected": selection["pending_selected"],
            "suppressed_skipped": selection.get("suppressed_skipped", 0),
            "pending_expired": 0,
            "pending_exits": [],
            "stale_days": None,
            "needs_attention": False,
            "streak": 0,
            "newest": uniq[0].get("published") if uniq else "",
            "oldest": uniq[-1].get("published") if uniq else "",
            "watermark": wm_iso,
            "empty_reason": empty_reason,
            "rejected": len(rejections),
            "rejections": rejections,
        }
        if status == "error":
            per_source_report[sid]["error"] = error
        report.append(per_source_report[sid])

        # Diagnostics only (never used for selection).
        observed = uniq[0].get("published") if status == "ok" and uniq else ""
        observed_dt = parse_dt(str(observed or "")) or parse_dt(str(st.get("observed_newest") or ""))
        stale_days = (fetched_at.date() - observed_dt.date()).days if observed_dt else None
        per_source_report[sid]["stale_days"] = stale_days

        deltas[sid] = {
            "status": status,
            "mode": mode,
            "error": error,
            "watermark": wm_iso,
            "observed_newest": observed,
        }

        if status != "ok":
            # A failed/empty source keeps its queue untouched: no exits, no TTL
            # refresh, nothing newly parked.
            continue
        planned_exits.extend(selection["exits"])
        remove_keys |= selection["remove_keys"]

    # ---- global quota: round-robin over sources
    order = [s["id"] for s in report]
    rotation = int(state.get("rotation") or 0)
    quota_order = order
    if selection_policy is not None and order:
        # A rotating starting source keeps a scarce budget from always favouring
        # the first configured source. Disabled mode keeps the legacy order.
        start = rotation % len(order)
        quota_order = order[start:] + order[:start]
    if args.max and args.max > 0:
        remaining = args.max
    elif selection_policy is not None:
        remaining = int(selection_policy.budgets["discovered"])
    else:
        remaining = None
    chosen: dict[str, list[dict]] = {sid: [] for sid in order}
    cut: dict[str, list[dict]] = {sid: [] for sid in order}
    if remaining is None:
        for sid in order:
            chosen[sid] = per_source_selected.get(sid, [])
    else:
        queues = {sid: list(per_source_selected.get(sid, [])) for sid in order}
        while remaining > 0 and any(queues[sid] for sid in quota_order):
            for sid in quota_order:
                if remaining <= 0:
                    break
                if not queues[sid]:
                    continue
                chosen[sid].append(queues[sid].pop(0))
                remaining -= 1
        for sid in order:
            cut[sid] = queues[sid]
            if cut[sid]:
                per_source_report[sid]["dropped_by_max"] += len(cut[sid])

    all_candidates: list[dict] = []
    for sid in order:
        items = chosen[sid]
        per_source_report[sid]["selected"] = len(items)
        all_candidates.extend(items)

    # ---- persist candidate content and raw snapshots for ALL selected or parked items
    items_to_persist: list[dict] = []
    seen_persist_keys: set[str] = set()
    for sid in order:
        for it in chosen.get(sid, []):
            k = it.get("_key") or it.get("url")
            if k not in seen_persist_keys:
                seen_persist_keys.add(k)
                items_to_persist.append(it)
        for it in cut.get(sid, []):
            k = it.get("_key") or it.get("url")
            if k not in seen_persist_keys:
                seen_persist_keys.add(k)
                items_to_persist.append(it)
        for it, _ in per_source_dropped.get(sid, []):
            k = it.get("_key") or it.get("url")
            if k not in seen_persist_keys:
                seen_persist_keys.add(k)
                items_to_persist.append(it)

    cache_dir = ROOT / ".state" / "candidates"
    for it in items_to_persist:
        content_md = it.get("_content_md")
        name = it.get("_cache_name") or it.get("cache_name")
        if content_md and name:
            actual_name, actual_path = ensure_candidate_cache(
                cache_dir, name, content_md, dry_run=args.dry_run
            )
            it["cache_name"] = actual_name
            it["_cache_name"] = actual_name
            it["content_file"] = str(actual_path)
        elif not it.get("content_file") and it.get("cache_name"):
            cname = it.get("cache_name")
            if is_safe_cache_name(cname):
                cpath = cache_dir / cname
                if cpath.is_file():
                    it["content_file"] = str(cpath)
                else:
                    it["content_file"] = None
            else:
                it["content_file"] = None

        raw_html = it.get("_raw_html")
        raw_rel = it.get("raw_file")
        if raw_html and raw_rel:
            actual_rel, _ = ensure_raw_snapshot(
                ROOT, raw_rel, raw_html, dry_run=args.dry_run
            )
            it["raw_file"] = actual_rel

    def _queue_time(item: dict) -> datetime:
        """First enqueue time: preserved for a re-picked pending item."""
        if item.get("from_pending"):
            dt = parse_dt(str(item.get("_queued_at") or item.get("queued_at") or ""))
            if dt is not None:
                return dt
        return fetched_at

    # ---- pending delta: everything selected is parked until it is seen, so an
    # agent dying mid-run cannot lose a candidate; leftovers keep their reason.
    for sid in order:
        for it in chosen[sid]:
            upserts[it["_key"]] = build_pending_record(
                it, sid, "awaiting_archive", queued_at=_queue_time(it)
            )
        for it in cut[sid]:
            upserts[it["_key"]] = build_pending_record(
                it, sid, "global_max", queued_at=_queue_time(it)
            )
        for it, reason in per_source_dropped.get(sid, []):
            upserts[it["_key"]] = build_pending_record(
                it, sid, reason, queued_at=_queue_time(it)
            )

    extra_keep_caches: set[str] = set(glm_section_names)
    extra_keep_raw: set[str] = set()
    for it in all_candidates:
        if it.get("cache_name"):
            extra_keep_caches.add(str(it["cache_name"]))
        if it.get("content_file"):
            extra_keep_caches.add(Path(it["content_file"]).name)
        if it.get("raw_file"):
            extra_keep_raw.add(str(it["raw_file"]))

    # ---- commit: re-read + merge under the lock
    try:
        result = commit_states(
            fetched_at=fetched_at,
            pending_baseline=pending_baseline,
            upserts={k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                     for k, v in upserts.items()},
            remove_keys=remove_keys,
            deltas=deltas,
            dry_run=args.dry_run,
            extra_keep_caches=extra_keep_caches,
            extra_keep_raw=extra_keep_raw,
            selection=(
                {
                    "module": selection_module,
                    "policy": selection_policy,
                    "run_gen": iso(fetched_at),
                    "candidates": [
                        {k: v for k, v in it.items() if not k.startswith("_")}
                        for it in all_candidates
                    ],
                    "observed": [
                        {k: v for k, v in it.items() if not k.startswith("_")}
                        for it in observed_candidates
                    ],
                    "expired_keys": {
                        e.get("key") for e in planned_exits if e.get("reason") == "expired"
                    },
                }
                if selection_policy is not None
                else None
            ),
            next_rotation=(rotation + 1) if selection_policy is not None else None,
        )
    except Exception as e:
        if selection_module is not None and isinstance(e, selection_module.SelectionError):
            print(f"[fetch] selection commit failed: {e}", file=sys.stderr)
            return e.exit_code
        raise
    merged_pending: dict[str, dict] = result["pending"]
    sources_after = result["sources"].get("sources") or {}

    final_exits = [dict(e) for e in planned_exits if e.get("key") in result["removals"]]
    for key, rec, reason in result["evicted"]:
        final_exits.append({
            "key": key,
            "url": str(rec.get("url") or ""),
            "source_id": str(rec.get("source_id") or ""),
            "reason": reason,
            "queued_at": iso(pending_queue_dt(rec)) if pending_queue_dt(rec) else "",
        })
    pending_expired = sum(1 for e in final_exits if e.get("reason") == "expired")

    exits_by_source: dict[str, list[dict]] = {}
    for exit_rec in final_exits:
        exits_by_source.setdefault(str(exit_rec.get("source_id") or ""), []).append(exit_rec)
    pending_by_source: dict[str, int] = {}
    for rec in merged_pending.values():
        sid = str(rec.get("source_id") or "")
        pending_by_source[sid] = pending_by_source.get(sid, 0) + 1

    for sid in order:
        rec = per_source_report[sid]
        rec["pending"] = pending_by_source.get(sid, 0)
        rec["pending_exits"] = exits_by_source.get(sid, [])
        rec["pending_expired"] = sum(1 for e in rec["pending_exits"]
                                     if e.get("reason") == "expired")
        # Streak/attention come from the merged record so an overlapping run is
        # counted once, not once per baseline snapshot.
        after = sources_after.get(sid) or {}
        rec["streak"] = int(after.get("failures") or 0)
        rec["needs_attention"] = bool(after.get("needs_attention"))

    skipped_total = sum(s.get("skipped", 0) for s in report)
    no_date_total = sum(s.get("no_date", 0) for s in report)
    no_date_selected_total = sum(s.get("no_date_selected", 0) for s in report)
    global_dropped = sum(s.get("dropped_by_max", 0) for s in report)
    coverage_skipped = sum(s.get("coverage_skipped", 0) for s in report)
    rejections_total = sum(s.get("rejected", 0) for s in report)

    out = {
        "generated_at": iso(fetched_at),
        "window_hours": window_hours,
        "lookback_days": MAX_WATERMARK_LOOKBACK_DAYS,
        "skipped_total": skipped_total,
        "no_date_total": no_date_total,
        "no_date_selected_total": no_date_selected_total,
        "global_dropped": global_dropped,
        "coverage_skipped": coverage_skipped,
        "rejections_total": rejections_total,
        "pending_total": len(merged_pending),
        "pending_expired": pending_expired,
        "pending_exits": final_exits,
        "max": args.max,
        "dry_run": bool(args.dry_run),
        "candidates": [],
        "sources": report,
    }
    if selection_policy is not None:
        stats = dict(result.get("selection") or {})
        # Suppressed work that was skipped before it could enter the queue is
        # still reported: zero pending resurrection is not "nothing to report".
        stats["suppressed"] = int(stats.get("suppressed") or 0) + sum(
            int(s.get("suppressed_skipped") or 0) for s in report
        )
        out["selection"] = stats
    for it in all_candidates:
        clean = {k: v for k, v in it.items() if not k.startswith("_")}
        out["candidates"].append(clean)

    if not args.dry_run:
        runs_dir = ROOT / ".state" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        run_path = runs_dir / f"{fetched_at.strftime('%Y-%m-%dT%H%M%S')}-fetch.json"
        if run_path.exists():
            # Two runs in the same second must not overwrite each other.
            run_path = runs_dir / (
                f"{fetched_at.strftime('%Y-%m-%dT%H%M%S')}-{os.getpid()}-fetch.json"
            )
        write_json_atomic(run_path, out)
    if args.json_out:
        write_json_atomic(args.json_out, out)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
