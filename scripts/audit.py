#!/usr/bin/env python3
"""Audit archive entries for summary/title/structure/index consistency issues."""
from __future__ import annotations

import csv
import json
import re
from urllib.parse import urlsplit

from common import (
    ROOT,
    SHORT_ARTICLE_CHARS,
    SHORT_MAX,
    SHORT_MIN,
    SUMMARY_MAX,
    SUMMARY_MIN,
    atx_h1_lines,
    category_slugs,
    context_slugs,
    count_summary_chars,
    dedupe_key,
    load_seen,
    read_entry,
    seen_path,
    summary_section,
)

VALID_SOURCE_TYPES = {"wechat", "vendor_blog", "tech_blog", "report", "docs", "other"}
VALID_STATUSES = {"archived", "partial", "needs_review"}

# Vendor primary domains: their entries must be first-party material, never a
# third-party wechat repost or a personal tech_blog.
VENDOR_DOMAINS = ("openai.com", "anthropic.com", "kimi.com", "moonshot.ai", "zhipuai.cn")
VENDOR_SOURCE_TYPES = {"vendor_blog", "report", "docs"}

PARTIAL_SUMMARY_BANNER = "摘要基于部分抓取"
PARTIAL_TEXT_MARK = "部分抓取"
PARTIAL_BANNER_WINDOW = 300

SUBCATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
FULL_SECTION_RE = re.compile(r"^[ \t]*##[ \t]*全文[ \t]*$", re.M)
BANNER_LINE_RE = re.compile(r"^[ \t]*>[^\n]*部分抓取", re.M)


def split_full_section(body: str) -> tuple[str, str]:
    """Split body into (everything before ``## 全文``, everything after it)."""
    m = FULL_SECTION_RE.search(body)
    if not m:
        return body, ""
    return body[: m.start()], body[m.end():]


def strip_leading_banner(text: str) -> str:
    """Drop leading blank lines plus a leading ``> ⚠️ **部分抓取**`` banner line.

    The banner is bookkeeping, not article content: counting it would let a
    truncated article cross the short-article threshold and then demand a
    200-250 char summary that a 120-char short-article summary can never meet.
    """
    lines = text.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].lstrip().startswith(">") and PARTIAL_TEXT_MARK in lines[i]:
        i += 1
    return "\n".join(lines[i:]).strip()


def head_has_partial_banner(full_text: str, limit: int = PARTIAL_BANNER_WINDOW) -> bool:
    """True when a ``部分抓取`` blockquote banner sits near the top of ``## 全文``."""
    return bool(BANNER_LINE_RE.search(full_text[:limit]))


def normalize_host(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def main() -> int:
    issues: list[tuple[str, str, str]] = []
    pending_titles: list[dict[str, str]] = []
    entries = sorted((ROOT / "library").rglob("entry.md"))

    key_to_paths: dict[str, list[str]] = {}

    for p in entries:
        fm, body = read_entry(p)
        rel = str(p.relative_to(ROOT))
        title = fm.get("title", "")
        summary = summary_section(body)
        n = count_summary_chars(summary)

        pre_full, full_text = split_full_section(body)
        chars = len(strip_leading_banner(full_text))
        short = chars < SHORT_ARTICLE_CHARS
        lo, hi = (SHORT_MIN, SHORT_MAX) if short else (SUMMARY_MIN, SUMMARY_MAX)

        if not re.search(r"[\u4e00-\u9fff]", title):
            issues.append((rel, "title_not_chinese", title))
        if not summary:
            issues.append((rel, "summary_empty", ""))
        elif not (lo <= n <= hi):
            issues.append((rel, "summary_out_of_range", f"{n} not in {lo}-{hi}"))
        if fm.get("summary_chars") != n:
            issues.append((rel, "summary_chars_mismatch", f"{fm.get('summary_chars')} != {n}"))

        status = fm.get("status")
        if status == "partial":
            content_note = (fm.get("content_note") or "").strip()
            summary_banner = PARTIAL_SUMMARY_BANNER in pre_full
            body_banner = head_has_partial_banner(full_text)
            missing: list[str] = []
            if not content_note:
                missing.append("content_note empty")
            if not summary_banner:
                missing.append(f"'{PARTIAL_SUMMARY_BANNER}' missing before '## 全文'")
            if not body_banner:
                missing.append(f"'{PARTIAL_TEXT_MARK}' banner missing in first {PARTIAL_BANNER_WINDOW} chars of '## 全文'")
            if missing:
                issues.append((rel, "partial_without_banner", "; ".join(missing)))
        elif status == "archived":
            content_note = (fm.get("content_note") or "").strip()
            residuals: list[str] = []
            if content_note:
                residuals.append(f"content_note='{content_note}'")
            if PARTIAL_SUMMARY_BANNER in pre_full:
                residuals.append(f"'{PARTIAL_SUMMARY_BANNER}' banner before '## 全文'")
            if head_has_partial_banner(full_text):
                residuals.append(f"'{PARTIAL_TEXT_MARK}' banner in '## 全文'")
            if residuals:
                issues.append((rel, "archived_has_partial_artifact", "; ".join(residuals)))
        elif status not in VALID_STATUSES:
            issues.append((rel, "bad_status", f"{status!r} not in {sorted(VALID_STATUSES)}"))

        # H1 check: must have exactly one H1 outside fenced code blocks, matching frontmatter title
        h1s = [text for _, text in atx_h1_lines(body)]
        if not h1s:
            issues.append((rel, "heading_mismatch", f"no H1 found in body != {title.strip()}"))
        else:
            if h1s[0].strip() != title.strip():
                issues.append((rel, "heading_mismatch", f"{h1s[0].strip()} != {title.strip()}"))
            if len(h1s) > 1:
                issues.append((rel, "duplicate_h1", f"found {len(h1s)} H1 headings: {h1s}"))

        if fm.get("category") not in category_slugs():
            issues.append((rel, "bad_category", fm.get("category")))
        if fm.get("context") not in context_slugs():
            issues.append((rel, "bad_context", fm.get("context")))

        subcategory = (fm.get("subcategory") or "").strip()
        if subcategory and not SUBCATEGORY_RE.fullmatch(subcategory):
            issues.append((rel, "bad_subcategory", f"{subcategory!r} does not match ^[a-z0-9][a-z0-9_-]*$"))

        if fm.get("raw_file") and not (p.parent / fm["raw_file"]).exists():
            issues.append((rel, "missing_raw", fm["raw_file"]))
        for img in re.findall(r"!\[[^\]]*\]\((images/[^)\s]+)\)", body):
            if not (p.parent / img).exists():
                issues.append((rel, "missing_image", img))

        # Check pending title status
        if fm.get("title_status") == "pending":
            pending_titles.append({
                "path": rel,
                "title": title,
                "original_title": fm.get("original_title", ""),
            })

        url = fm.get("url", "")
        source_type = fm.get("source_type", "")

        if source_type not in VALID_SOURCE_TYPES:
            issues.append((rel, "bad_source_type", f"{source_type!r} not in {sorted(VALID_SOURCE_TYPES)}"))

        # Domain-specific source_type rules
        if "docs.bigmodel.cn" in url and source_type != "docs":
            issues.append((rel, "source_type_mismatch", f"docs.bigmodel.cn must have source_type 'docs', got '{source_type}'"))
        if "mp.weixin.qq.com" in url and source_type != "wechat":
            issues.append((rel, "source_type_mismatch", f"mp.weixin.qq.com must have source_type 'wechat', got '{source_type}'"))

        # Vendor primary domains may host blog / report / docs material under one
        # official site, but never a wechat repost or a third-party tech_blog.
        host = normalize_host(url)
        for domain in VENDOR_DOMAINS:
            if host_matches(host, domain):
                if source_type not in VENDOR_SOURCE_TYPES:
                    issues.append((
                        rel,
                        "source_type_mismatch",
                        f"vendor domain {host} must map to one of {sorted(VENDOR_SOURCE_TYPES)}, got '{source_type}'",
                    ))
                break

        # In-library duplicate canonical URL accounting
        raw_key_url = fm.get("canonical_url") or url or ""
        key = dedupe_key(raw_key_url) if raw_key_url else ""
        if key:
            key_to_paths.setdefault(key, []).append(rel)

    for key, paths in sorted(key_to_paths.items()):
        if len(paths) < 2:
            continue
        for rel in paths:
            others = [x for x in paths if x != rel]
            issues.append((rel, "duplicate_canonical_url", f"{key} also used by {', '.join(others)}"))

    # index.csv alignment
    index_path = ROOT / "index.csv"
    if not index_path.exists():
        issues.append(("index.csv", "index_missing", "index.csv does not exist"))
    else:
        try:
            with open(index_path, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            if len(rows) != len(entries):
                issues.append(("index.csv", "index_row_count_mismatch", f"index.csv has {len(rows)} rows, but library has {len(entries)} entries"))
            for r in rows:
                r_id = r.get("id", "")
                r_path = r.get("path", "")
                if not r_path:
                    issues.append(("index.csv", "index_path_missing", f"entry id '{r_id}' has empty path"))
                    continue
                p = ROOT / r_path
                if not p.exists():
                    issues.append(("index.csv", "index_path_missing", f"file does not exist: {r_path}"))
                    continue
                entry_fm, _ = read_entry(p)
                if entry_fm.get("id") != r_id:
                    issues.append((r_path, "index_id_mismatch", f"index.csv id '{r_id}' != frontmatter id '{entry_fm.get('id')}'"))
        except Exception as e:
            issues.append(("index.csv", "index_read_error", str(e)))

    # seen.json alignment
    sp = seen_path()
    if not sp.exists():
        if entries:
            issues.append(("seen.json", "seen_missing", "seen.json does not exist"))
    else:
        try:
            seen_data = load_seen(sp)
            seen_entries = seen_data.get("entries", {})
            # 1. Check all library entries are in seen.json with correct path
            for p in entries:
                rel = str(p.relative_to(ROOT))
                fm, _ = read_entry(p)
                raw_url = fm.get("canonical_url") or fm.get("url") or ""
                key = dedupe_key(raw_url)
                if not key or key not in seen_entries:
                    issues.append((rel, "seen_missing_key", f"entry '{fm.get('id')}' ({key or raw_url}) missing from seen.json"))
                else:
                    item = seen_entries[key]
                    s_path = item.get("path", "")
                    if s_path != rel:
                        issues.append((rel, "seen_path_mismatch", f"seen path '{s_path}' != actual '{rel}'"))

            # 2. Check all seen.json entries point to existing library entries with matching id
            for key, val in seen_entries.items():
                s_path = val.get("path", "")
                if not s_path:
                    issues.append(("seen.json", "seen_empty_path", f"seen key '{key}' has empty path"))
                    continue
                target_p = ROOT / s_path
                if not target_p.exists():
                    issues.append(("seen.json", "seen_orphan_entry", f"seen key '{key}' points to non-existent path: {s_path}"))
                else:
                    try:
                        e_fm, _ = read_entry(target_p)
                        if val.get("id") and e_fm.get("id") and val.get("id") != e_fm.get("id"):
                            issues.append((s_path, "seen_id_mismatch", f"seen id '{val.get('id')}' != entry id '{e_fm.get('id')}'"))
                    except Exception as e:
                        issues.append((s_path, "seen_entry_read_error", str(e)))
        except Exception as e:
            issues.append(("seen.json", "seen_corrupt", str(e)))

    print(json.dumps({
        "entries": len(entries),
        "pending_titles": pending_titles,
        "issues": [{"path": a, "type": b, "detail": c} for a, b, c in issues],
        "ok": not issues,
    }, ensure_ascii=False, indent=2))
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
