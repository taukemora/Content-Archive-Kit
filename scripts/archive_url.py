#!/usr/bin/env python3
"""Archive one URL into a portable entry.md skeleton."""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
import trafilatura
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from markdownify import markdownify as md

from article_formatting import normalize_formatting_html, normalize_formatting_tree
from media_preservation import preserve_media
from media_markup import (
    finalize_media_markdown,
    prepare_generic_html,
    prepare_wechat_media,
    summarize_media,
)
from media_policy import (
    DEFAULT_MAX_DOWNLOAD_SECONDS,
    DEFAULT_MAX_IMAGES,
    DEFAULT_MAX_SINGLE_IMAGE_MIB,
    DEFAULT_MAX_TOTAL_IMAGE_MIB,
    MediaBudget,
    MediaConfigError,
    resolve_budget,
)
from common import (
    ROOT,
    TZ,
    assert_public_url,
    canonicalize_url,
    category_slugs,
    compute_content_hash,
    context_slugs,
    count_summary_chars,
    dedupe_key,
    is_allowed_file_path,
    iso,
    load_seen,
    load_yaml,
    normalize_body_headings,
    now,
    read_entry,
    safe_fetch,
    save_seen,
    slugify,
    state_lock,
    taxonomy,
    write_entry,
    write_text_atomic,
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
CONTENT_HASH_MIN_CHARS = 40  # too-short bodies collide by accident; don't dedupe on them

_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(\s+\"[^\"]*\")?\)")
_LOCALIZED_ALT_RE = re.compile(r"（未本地化：.*）\s*$")


class ImageDownloadTimeout(Exception):
    """The article-wide image download time budget was exhausted."""


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def fetch(url: str, referer: str | None = None) -> requests.Response:
    return safe_fetch(url, referer=referer, timeout=40, max_redirects=3)


NON_ARTICLE_CONTENT_TYPES = {
    "application/pdf",
    "application/zip",
    "application/octet-stream",
    "application/rss+xml",
    "application/atom+xml",
    "application/xml",
    "text/xml",
}
NON_ARTICLE_CONTENT_PREFIXES = ("image/", "video/", "audio/")


def check_web_content_type(response) -> None:
    """Reject non-HTML payloads (PDF/zip/binary/image/video/audio) outright."""
    headers = getattr(response, "headers", None) or {}
    try:
        raw = headers.get("Content-Type") or ""
    except Exception:
        raw = ""
    main = str(raw).split(";")[0].strip().lower()
    if not main:
        return
    if main in NON_ARTICLE_CONTENT_TYPES or main.startswith(NON_ARTICLE_CONTENT_PREFIXES):
        raise ValueError(f"不支持非网页文章媒体类型: {raw}")


def image_ext(data: bytes) -> str:
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"GIF8"):
        return ".gif"
    return ".img"


def _read_response_limited(
    response,
    limit: int,
    deadline: float | None = None,
) -> bytes:
    """Read at most ``limit`` bytes, preferring a streaming response.

    ``deadline`` is an absolute ``time.monotonic()`` value for the whole
    article's image budget; it is checked while streaming so a slow body can
    neither hang the run nor spend the next image's allowance.
    """
    headers = getattr(response, "headers", {}) or {}
    try:
        content_length = headers.get("Content-Length")
    except Exception:
        content_length = None
    if content_length and str(content_length).isdigit() and int(content_length) > limit:
        raise ValueError(f"Content-Length {content_length} exceeds {limit}")

    def check_deadline() -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise ImageDownloadTimeout("image download budget exhausted")

    check_deadline()
    chunks: list[bytes] = []
    total = 0
    try:
        iterator = response.iter_content(64 * 1024)
    except (AttributeError, TypeError):
        iterator = None

    if iterator is not None:
        try:
            for chunk in iterator:
                if not chunk:
                    continue
                total += len(chunk)
                if total > limit:
                    raise ValueError(f"stream exceeds {limit} bytes")
                check_deadline()
                chunks.append(chunk)
        except TypeError:
            chunks = []

    if chunks:
        check_deadline()
        return b"".join(chunks)

    check_deadline()
    data = response.content
    check_deadline()
    if len(data) > limit:
        raise ValueError(f"body exceeds {limit} bytes")
    return data


def _annotate_alt(alt: str, reason: str) -> str:
    """Append a visible, non-HTML reason to a failed image's alt text."""
    base = _LOCALIZED_ALT_RE.sub("", alt or "").strip()
    return f"{base}（未本地化：{reason}）" if base else f"（未本地化：{reason}）"


def localize_images(
    markdown_text: str,
    base_url: str,
    images_dir: Path,
    referer: str | None = None,
    budget: MediaBudget | None = None,
) -> tuple[str, int, int, list[str]]:
    """Download remote markdown images, rewrite to local paths.

    Returns ``(text, ok, failed, notes)``.  The optional ``budget`` carries the
    article-wide count/size/time limits; failures keep the original remote
    Markdown image (a safe external link in the reader) and gain a Chinese
    reason in both the alt text and ``notes``.
    """
    budget = budget or MediaBudget(
        max_images=DEFAULT_MAX_IMAGES,
        max_single_image_bytes=DEFAULT_MAX_SINGLE_IMAGE_MIB * 1024 * 1024,
        max_total_image_bytes=DEFAULT_MAX_TOTAL_IMAGE_MIB * 1024 * 1024,
        max_download_seconds=float(DEFAULT_MAX_DOWNLOAD_SECONDS),
    )
    images_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + float(budget.max_download_seconds)
    seen: dict[str, str] = {}
    ok = failed = 0
    counter = [0]
    total_bytes = [0]
    notes: list[str] = []
    stopped_count = [False]
    stopped_total = [False]
    stopped_time = [False]

    def add_note(message: str) -> None:
        if message not in notes:
            notes.append(message)

    def stop(reason: str, flag: list[bool], message: str) -> None:
        if not flag[0]:
            flag[0] = True
            add_note(message)

    def repl(m: re.Match) -> str:
        nonlocal ok, failed
        alt, src, title = m.group(1), m.group(2), m.group(3) or ""
        if src.startswith(("images/", "data:")):
            return m.group(0)
        absolute = urljoin(base_url, src)
        if absolute in seen:
            return f"![{alt}]({seen[absolute]})"

        def leave(reason: str, detail: str | None = None) -> str:
            nonlocal failed
            failed += 1
            if detail:
                add_note(f"{detail}：{absolute}")
            return f"![{_annotate_alt(alt, reason)}]({src}{title})"

        if stopped_time[0] or time.monotonic() >= deadline:
            stop(
                "图片下载时间预算用尽",
                stopped_time,
                f"图片下载时间预算 {budget.max_download_seconds:g} 秒用尽，后续图片未本地化",
            )
            return leave("图片下载时间预算用尽")
        if stopped_count[0] or counter[0] >= budget.max_images:
            stop(
                "达到单篇图片数量上限",
                stopped_count,
                f"达到单篇最多 {budget.max_images} 张图片上限，后续图片未本地化",
            )
            return leave(f"达到单篇最多 {budget.max_images} 张图片上限")
        if stopped_total[0] or total_bytes[0] >= budget.max_total_image_bytes:
            stop(
                "单篇图片总大小超限",
                stopped_total,
                f"单篇图片总大小超过 {budget.max_total_image_mib:g}MB 上限，后续图片未本地化",
            )
            return leave(f"单篇图片总大小超过 {budget.max_total_image_mib:g}MB 上限")

        try:
            assert_public_url(absolute)
        except Exception as e:
            log(f"image blocked by SSRF check: {absolute}: {e}")
            return leave("图片链接被 SSRF 拦截", "图片链接被 SSRF 拦截")

        try:
            remaining = max(0.05, deadline - time.monotonic())
            r = safe_fetch(
                absolute,
                referer=referer or base_url,
                timeout=min(30.0, remaining),
                max_redirects=3,
                stream=True,
            )
            try:
                try:
                    data = _read_response_limited(r, budget.max_single_image_bytes, deadline)
                finally:
                    close = getattr(r, "close", None)
                    if callable(close):
                        close()
            except ImageDownloadTimeout:
                stop(
                    "图片下载时间预算用尽",
                    stopped_time,
                    f"图片下载时间预算 {budget.max_download_seconds:g} 秒用尽，后续图片未本地化",
                )
                return leave("图片下载时间预算用尽")
            except ValueError as e:
                log(f"image size exceeded: {absolute}: {e}")
                return leave(
                    f"单图大小超限（上限 {budget.max_single_image_mib:g} MiB）",
                    "单图大小超限",
                )
            if len(data) > budget.max_single_image_bytes:
                log(f"image size exceeded: {absolute}: {len(data)} bytes")
                return leave(
                    f"单图大小超限（上限 {budget.max_single_image_mib:g} MiB）",
                    "单图大小超限",
                )

            if total_bytes[0] + len(data) > budget.max_total_image_bytes:
                stop(
                    "单篇图片总大小超限",
                    stopped_total,
                    f"单篇图片总大小超过 {budget.max_total_image_mib:g}MB 上限，后续图片未本地化",
                )
                return leave(
                    f"单篇图片总大小超过 {budget.max_total_image_mib:g}MB 上限",
                    "单篇图片总大小超过上限",
                )

            total_bytes[0] += len(data)
            counter[0] += 1
            name = f"{counter[0]:03d}{image_ext(data)}"
            (images_dir / name).write_bytes(data)
            rel = f"images/{name}"
            seen[absolute] = rel
            ok += 1
            return f"![{alt}]({rel})"
        except ImageDownloadTimeout:
            stop(
                "图片下载时间预算用尽",
                stopped_time,
                f"图片下载时间预算 {budget.max_download_seconds:g} 秒用尽，后续图片未本地化",
            )
            return leave("图片下载时间预算用尽")
        except Exception as e:
            log(f"image failed: {absolute}: {e}")
            if time.monotonic() >= deadline:
                stop(
                    "图片下载时间预算用尽",
                    stopped_time,
                    f"图片下载时间预算 {budget.max_download_seconds:g} 秒用尽，后续图片未本地化",
                )
                return leave("图片下载时间预算用尽")
            return leave("图片下载失败", "图片下载失败")

    text = _IMAGE_RE.sub(repl, markdown_text)
    return text, ok, failed, notes


def meta_from_html(html: str, url: str) -> dict:
    s = BeautifulSoup(html, "html.parser")

    def content(selector: str) -> str:
        el = s.select_one(selector)
        return (el.get("content") or "").strip() if el else ""

    title = content('meta[property="og:title"]') or content('meta[name="twitter:title"]')
    if not title and s.title:
        title = s.title.get_text(" ", strip=True)
    site = content('meta[property="og:site_name"]') or urlsplit(url).netloc
    author = content('meta[name="author"]') or content('meta[property="article:author"]')

    meta_name_pub = content('meta[name="published_time"]')
    meta_art_pub = content('meta[property="article:published_time"]') or content('meta[name="article:published_time"]')
    meta_date = content('meta[name="date"]') or content('meta[name="pubdate"]') or content('meta[name="publishdate"]')

    def is_cal_date(v: str) -> bool:
        return bool(re.match(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$", (v or "").strip()))

    def has_time(v: str) -> bool:
        return bool(re.search(r"[T\s]\d{1,2}:\d{2}", (v or "").strip()))

    # JSON-LD fallback
    json_ld_pub = ""
    for tag in s.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            title = title or (item.get("headline") or item.get("name") or "")
            json_ld_pub = json_ld_pub or (item.get("datePublished") or "")
            author_obj = item.get("author")
            if not author and isinstance(author_obj, dict):
                author = author_obj.get("name") or ""
            elif not author and isinstance(author_obj, list) and author_obj:
                author = author_obj[0].get("name", "") if isinstance(author_obj[0], dict) else ""

    published = ""
    published_original = ""
    source_display_date = ""
    date_precision = ""

    # Google blog & generic: both meta[name=published_time] and article:published_time
    candidates_pub = [p for p in (meta_name_pub, meta_art_pub, meta_date, json_ld_pub) if p]
    time_cand = next((p for p in candidates_pub if has_time(p)), None)
    date_cand = next((p for p in candidates_pub if is_cal_date(p)), None)

    if time_cand:
        published = time_cand
        published_original = time_cand
        date_precision = "datetime"
    elif date_cand:
        published = date_cand
        published_original = date_cand
        date_precision = "day"
    elif candidates_pub:
        published = candidates_pub[0]
        published_original = candidates_pub[0]
        date_precision = "datetime" if has_time(published) else "day"

    # source_display_date: explicit page calendar date only (no fabrication if absent)
    if meta_art_pub and is_cal_date(meta_art_pub):
        source_display_date = meta_art_pub
    elif date_cand:
        source_display_date = date_cand
    elif meta_art_pub and not has_time(meta_art_pub):
        parsed_d = parse_date(meta_art_pub)
        if parsed_d:
            source_display_date = parsed_d[:10]

    language = (s.html.get("lang") if s.html else "") or ""
    return {
        "title": (title or "").strip(),
        "source": (site or "").strip(),
        "author": (author or "").strip(),
        "published": (published or "").strip(),
        "published_original": (published_original or "").strip(),
        "source_display_date": (source_display_date or "").strip(),
        "date_precision": date_precision,
        "language": language,
    }


WEB_EXTRACT_TAIL_MARKERS = (
    "[page truncated",
    "page truncated",
    "full text saved to",
)


def strip_truncation_note(text: str) -> tuple[str, bool]:
    """Strip a trailing Hermes ``web_extract`` truncation footnote.

    Returns ``(text, matched)``; ``matched`` is True when the footnote was
    found and removed, which forces the entry to be marked ``partial``.
    """
    lines = (text or "").splitlines()
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    if end == 0:
        return text, False

    start = end
    while start > 0 and lines[start - 1].strip():
        start -= 1
    block = "\n".join(lines[start:end]).lower()
    if not any(marker in block for marker in WEB_EXTRACT_TAIL_MARKERS):
        return text, False

    head = lines[:start]
    while head and not head[-1].strip():
        head.pop()
    if head and head[-1].strip() in ("---", "***", "___"):
        head.pop()
    return "\n".join(head).strip(), True


def strip_web_extract_header(text: str, original_title: str = "") -> str:
    """Remove web_extract's leading title/metadata block.

    Only the leading header block is touched: ``URL:`` / ``Published:`` lines in
    the article body or inside code blocks are preserved verbatim.
    """
    lines = (text or "").splitlines()
    while lines:
        line = lines[0].strip()
        if not line:
            lines.pop(0)
            continue
        if re.match(r"^(URL|Published):", line):
            lines.pop(0)
            continue
        if line.startswith("# "):
            lines.pop(0)
            continue
        if original_title and (line == original_title or line.startswith(original_title)):
            lines.pop(0)
            continue
        if re.match(r"^[A-Z][a-z]+ \d{1,2}, \d{4}$", line):
            lines.pop(0)
            continue
        break
    return "\n".join(lines).strip()


def looks_truncated(text: str) -> bool:
    s = (text or "").strip()
    if len(s) < 300:
        return False
    while True:
        stripped = re.sub(r"!?\[[^\]]*\]\([^)]*\)\s*$", "", s).rstrip()
        if stripped == s:
            break
        s = stripped
    if len(s) < 300:
        return False
    end_chars = set("。！？.!?\"”…）")
    return s[-1] not in end_chars


def parse_date(value: str) -> str:
    if not value:
        return ""
    try:
        dt = dateparser.parse(value)
        if not dt:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ).isoformat(timespec="seconds")
    except Exception:
        return ""


def wechat_extract(html: str, url: str) -> tuple[dict, str, str]:
    s = BeautifulSoup(html, "html.parser")
    title_el = s.select_one("#activity-name") or s.select_one('meta[property="og:title"]')
    title = title_el.get_text(" ", strip=True) if title_el and title_el.name != "meta" else (title_el.get("content", "") if title_el else "")
    account = s.select_one("#js_name")
    author = s.select_one("#js_author_name")
    ct = re.search(r"(?<![\w])(?:var\s+|window\.)?ct\s*=\s*['\"]?(\d{10})['\"]?", html)
    published = ""
    if ct:
        published = datetime.fromtimestamp(int(ct.group(1)), tz=TZ).isoformat(timespec="seconds")
    else:
        meta_pub = s.select_one('meta[property="article:published_time"]')
        raw_pub = (meta_pub.get("content") or "").strip() if meta_pub else ""
        if raw_pub:
            published = parse_date(raw_pub)
    body = s.select_one("#js_content")
    if not body:
        raise ValueError("未找到 #js_content，可能是视频/图片页、已删除或触发验证")
    # Preserve video positions and recognised captions before any node is dropped.
    videos, _captions = prepare_wechat_media(body, url)
    for tag in body.select("script,style,iframe"):
        tag.decompose()
    for img in body.select("img"):
        src = img.get("data-src") or img.get("src")
        if src:
            img["src"] = src
        img.attrs.pop("data-src", None)
        for attr in ("data-type", "data-fail", "data-ratio", "data-w", "data-copy", "data-src"):
            img.attrs.pop(attr, None)
    # Normalise emphasis on the DOM: markdownify cannot see inline CSS and
    # emits literal stars when one <strong> encloses several blocks.
    normalize_formatting_tree(body)
    content = md(str(body), heading_style="ATX")
    content, _media_stats = finalize_media_markdown(content, videos)
    content = re.sub(r"\n{3,}", "\n\n", content).strip()
    meta = {
        "title": title.strip(),
        "source": (account.get_text(" ", strip=True) if account else "微信公众号"),
        "author": (author.get_text(" ", strip=True) if author else ""),
        "published": published,
        "language": "zh",
    }
    return meta, content, html


def generic_extract(html: str, url: str) -> tuple[dict, str]:
    meta = meta_from_html(html, url)
    try:
        tm = trafilatura.extract_metadata(html, default_url=url)
        if tm:
            meta["title"] = meta["title"] or (tm.title or "")
            meta["author"] = meta["author"] or (tm.author or "")
            meta["source"] = meta["source"] or (tm.sitename or "")
            if not meta["published"] and tm.date:
                meta["published"] = tm.date
                meta["published_original"] = tm.date
                meta["date_precision"] = "day" if re.match(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$", tm.date) else "datetime"
                if re.match(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$", tm.date):
                    meta["source_display_date"] = tm.date
    except Exception:
        pass
    prepared_html, videos, _captions = prepare_generic_html(html, url)
    prepared_html = normalize_formatting_html(prepared_html)
    content = trafilatura.extract(
        prepared_html,
        url=url,
        output_format="markdown",
        include_images=True,
        include_links=True,
        include_formatting=True,
        favor_recall=True,
    )
    if not content:
        s = BeautifulSoup(prepared_html, "html.parser")
        node = s.select_one("article") or s.select_one("main") or s.body
        if not node:
            raise ValueError("正文提取失败：没有 article/main/body")
        content = md(str(node), heading_style="ATX")
    content, _media_stats = finalize_media_markdown(content, videos)
    content = re.sub(r"\n{3,}", "\n\n", content).strip()
    return meta, content


_ASCII_KEYWORD_RE = re.compile(r"^[a-z0-9]{1,12}$")


def _keyword_hits(hay: str, keyword: str) -> int:
    """Count keyword occurrences; short ASCII terms use word boundaries.

    Bare ``str.count`` would let ``ai`` match ``said`` or ``git`` match
    ``digital``; CJK keywords stay plain substring counts.
    """
    if _ASCII_KEYWORD_RE.match(keyword):
        return len(re.findall(r"\b" + re.escape(keyword) + r"\b", hay))
    return hay.count(keyword)


def classify(text: str, title: str) -> tuple[str, float]:
    tx = taxonomy()
    title_lower = (title or "").lower()
    hay = (title + "\n" + text[:4000]).lower()
    best, best_score = "other", 0
    for cat in tx.get("categories", []):
        score = 0
        for kw in cat.get("keywords") or []:
            kwl = str(kw).lower().strip()
            if not kwl:
                continue
            hits = _keyword_hits(hay, kwl)
            if not hits:
                continue
            score += hits * (3 if _keyword_hits(title_lower, kwl) else 1)
        if score > best_score:
            best, best_score = cat["slug"], score
    confidence = min(0.95, 0.5 + 0.1 * best_score) if best_score else 0.0
    return best, confidence


def resolve_local_arg(value: str) -> Path:
    """Resolve a local CLI path relative to the project root first, then CWD."""
    p = Path(value).expanduser()
    if not p.is_absolute():
        candidate = ROOT / p
        if candidate.exists():
            p = candidate
    return p.resolve()


def find_entry_by_content_hash(content_hash: str, library_root: Path | None = None) -> Path | None:
    """Return the first ``entry.md`` whose ``content_hash`` matches (else None)."""
    if not content_hash:
        return None
    lib = library_root or (ROOT / "library")
    if not lib.exists():
        return None
    for p in sorted(lib.rglob("entry.md")):
        try:
            fm, _ = read_entry(p)
        except Exception:
            continue
        if (fm.get("content_hash") or "").strip() == content_hash:
            return p
    return None


def move_to_trash(entry_dir: Path, root: Path | None = None) -> Path:
    """Soft-move an entry directory into ``<root>/trash/``; never deletes."""
    base = Path(root) if root is not None else ROOT
    trash_root = base / "trash"
    trash_root.mkdir(parents=True, exist_ok=True)
    stamp = now().strftime("%Y%m%d-%H%M%S")
    counter = 1
    while True:
        suffix = "" if counter == 1 else f"-{counter}"
        target = trash_root / f"{stamp}-{entry_dir.name}{suffix}"
        counter += 1
        try:
            os.rename(entry_dir, target)
            return target
        except FileExistsError:
            continue
        except OSError:
            # rename may fail across filesystems; fall back to a unique move.
            if target.exists():
                continue
            shutil.move(str(entry_dir), str(target))
            return target


COLLECTION_VALUES = ("manual", "auto")
COLLECTION_FIELD_RE = re.compile(r"^collection\s*:")


def normalize_collection(value) -> str:
    """Return ``manual``/``auto`` for a valid value, else ``""``."""
    v = str(value or "").strip().lower()
    return v if v in COLLECTION_VALUES else ""


def entry_path_in_library(rel_or_path) -> Path | None:
    """Resolve a seen/index path to a real ``entry.md`` inside ``ROOT/library``.

    ``seen.json`` is data, not a trust boundary: absolute paths, ``..`` escapes
    and symlinks pointing outside the library are never followed or rewritten.
    """
    raw = str(rel_or_path or "").strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = ROOT / p
    try:
        resolved = p.resolve(strict=True)
        library = (ROOT / "library").resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if resolved.name != "entry.md" or not resolved.is_file():
        return None
    if resolved != library and library not in resolved.parents:
        return None
    return resolved


def set_collection_text(text: str, collection: str) -> str:
    """Set ``collection`` in the frontmatter, leaving every other byte intact.

    A full YAML round-trip would re-quote/rewrap unrelated fields, so the line
    is edited in place (or appended as the last frontmatter field).
    """
    if not text.startswith("---\n"):
        raise ValueError("entry.md has no YAML frontmatter")
    end = text.find("\n---", 4)
    if end == -1:
        raise ValueError("entry.md frontmatter is not closed")
    lines = text[4:end].split("\n")
    for i, line in enumerate(lines):
        if COLLECTION_FIELD_RE.match(line):
            lines[i] = f"collection: {collection}"
            return text[:4] + "\n".join(lines) + text[end:]
    last = len(lines) - 1
    while last >= 0 and not lines[last].strip():
        last -= 1
    lines.insert(last + 1, f"collection: {collection}")
    return text[:4] + "\n".join(lines) + text[end:]


def promote_collection(entry_path: Path, reason: str = "") -> tuple[str, bool]:
    """Upgrade an existing entry to ``manual``; returns ``(collection, changed)``.

    The caller must already hold ``state_lock`` -- this never re-locks (a nested
    ``flock`` would deadlock). The file is re-read here so a concurrent
    ``finalize`` is never clobbered, and only the ``collection`` field is
    touched: body, summary and original dates stay byte-identical.
    """
    path = entry_path_in_library(entry_path)
    if path is None:
        return "", False
    try:
        fm, _ = read_entry(path)
    except Exception as e:
        log(f"collection: cannot read {path}: {e}")
        return "", False
    current = normalize_collection(fm.get("collection"))
    if current == "manual":
        return "manual", False
    try:
        text = path.read_text(encoding="utf-8")
        updated = set_collection_text(text, "manual")
        if updated != text:
            write_text_atomic(path, updated)
            log(f"collection: promoted {path.parent.name} to manual{reason}")
            return "manual", True
    except (OSError, ValueError) as e:
        log(f"collection: could not promote {path}: {e}")
        return current, False
    return "manual", False


def reserve_entry_dir(parent: Path, entry_id: str) -> Path:
    """Atomically claim a fresh entry directory for this run.

    ``mkdir(exist_ok=False)`` makes the claim atomic: two concurrent runs can
    never receive the same directory, so one can never clean up (or trash) the
    other's half-written entry.  Name collisions fall through to ``-2``, ...
    """
    parent.mkdir(parents=True, exist_ok=True)
    candidate = parent / entry_id
    n = 2
    while True:
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            candidate = parent / f"{entry_id}-{n}"
            n += 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--title-zh", default="")
    ap.add_argument("--original-title", default="")
    ap.add_argument("--source", default="")
    ap.add_argument("--author", default="")
    ap.add_argument("--published", default="")
    ap.add_argument("--published-original", default="", help="raw publication date string from source")
    ap.add_argument("--date-precision", default="", help="precision of publication date: datetime or day")
    ap.add_argument("--source-display-date", default="", help="explicit page calendar date (YYYY-MM-DD), if proven")
    ap.add_argument("--date-source", default="", help="feed/page/fetched; defaults inferred")
    ap.add_argument("--published-unknown", action="store_true", help="published date is a fetch-date fallback")
    ap.add_argument("--language", default="")
    ap.add_argument("--category", default="")
    ap.add_argument("--subcategory", default="")
    ap.add_argument("--context", default="")
    ap.add_argument("--tags", default="")
    ap.add_argument("--source-type", default="")
    ap.add_argument("--content-file", default="")
    ap.add_argument(
        "--media-html-file",
        default="",
        help="original HTML used to append YouTube links / inline-SVG chart text when --content-file is used",
    )
    ap.add_argument("--raw-file", default="", help="path to raw snapshot file (under the scratch root, /tmp, or .state)")
    ap.add_argument("--extraction", default="", help="override extraction label, e.g. web_extract/docs")
    ap.add_argument(
        "--collection",
        choices=COLLECTION_VALUES,
        default=None,
        help="collection provenance: manual (user saved it) or auto (feed/backfill); "
             "default manual for new entries, inherited on --force",
    )
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--max-images", type=int, default=None, help="单篇最多本地化图片数（覆盖 media.yaml）")
    ap.add_argument("--max-image-mib", type=float, default=None, help="单张图片最大 MiB（覆盖 media.yaml）")
    ap.add_argument("--max-total-image-mib", type=float, default=None, help="单篇图片总大小上限 MiB（覆盖 media.yaml）")
    ap.add_argument("--image-download-seconds", type=float, default=None, help="单篇图片下载总时间预算秒（覆盖 media.yaml）")
    args = ap.parse_args()

    url = args.url.strip()
    try:
        assert_public_url(url)
    except Exception as e:
        print(json.dumps({"status": "error", "url": url, "error": f"SSRF blocked: {e}"}, ensure_ascii=False, indent=2))
        return 1

    try:
        media_budget = resolve_budget(
            {
                "max_images": args.max_images,
                "max_single_image_mib": args.max_image_mib,
                "max_total_image_mib": args.max_total_image_mib,
                "max_download_seconds": args.image_download_seconds,
            },
            root=ROOT,
        )
    except MediaConfigError as e:
        print(json.dumps({"status": "error", "url": url, "error": f"媒体预算配置无效: {e}"}, ensure_ascii=False, indent=2))
        return 1

    key = dedupe_key(url)
    try:
        seen = load_seen()
    except Exception as e:
        print(json.dumps({"status": "error", "url": url, "error": f"Failed to load seen.json: {e}"}, ensure_ascii=False, indent=2))
        return 1

    # A plain ``add`` is a deliberate user save, even without --collection.
    requested_collection = args.collection or "manual"

    # Optional subscription selection. Absent/disabled policy leaves every path
    # below byte-for-byte legacy behaviour; a present-but-invalid policy fails
    # automatic add visibly but never blocks a deliberate manual save.
    selection_module = None
    selection_policy = None
    selection_error = ""
    if (ROOT / "selection.yaml").exists():
        try:
            import subscription_selection as selection_module  # type: ignore[no-redef]

            selection_policy = selection_module.load_policy(ROOT)
        except Exception as e:
            selection_error = str(e)
            selection_module = None
            selection_policy = None
            log(
                "selection automation unavailable; manual saves are unaffected "
                f"and the ledger is left untouched: {e}"
            )

    def auto_fail(reason: str) -> None:
        """Accepted work that could not be archived stays retryable + visible."""
        if requested_collection == "auto" and selection_policy is not None:
            try:
                selection_module.mark_archive_failure(
                    ROOT, selection_policy, url=url, reason=reason
                )
            except Exception as e:
                log(f"selection automation warning: could not record archive failure: {e}")

    if key in seen.get("entries", {}) and not args.force:
        payload = {
            "status": "duplicate",
            "url": url,
            "entry": seen["entries"][key],
        }
        # Re-read under the lock, then promote only if the caller meant "manual":
        # an explicit ``--collection auto`` must never downgrade a manual entry.
        with state_lock(ROOT):
            try:
                current_seen = load_seen()
            except Exception as e:
                log(f"duplicate: could not re-read seen.json under lock: {e}")
                current_seen = seen
            record = (current_seen.get("entries") or {}).get(key) or seen["entries"][key]
            payload["entry"] = record
            entry_path = entry_path_in_library(record.get("path"))
            collection = normalize_collection(record.get("collection"))
            if entry_path is not None and not collection:
                # seen.json stores no collection; the entry.md is the source of truth.
                try:
                    current_fm, _ = read_entry(entry_path)
                    collection = normalize_collection(current_fm.get("collection"))
                except Exception as e:
                    log(f"duplicate: could not read {entry_path}: {e}")
            promoted = False
            if requested_collection == "manual" and entry_path is not None:
                collection, promoted = promote_collection(
                    entry_path, reason=" (duplicate add)"
                )
            payload["collection"] = collection
            payload["promoted"] = promoted
        if requested_collection == "auto" and selection_policy is not None:
            # A duplicate is already archived work: stop retrying the admission
            # instead of leaving a stale 'ready' record forever.
            try:
                selection_module.mark_archived(
                    ROOT,
                    selection_policy,
                    url=url,
                    entry_id=str((payload.get("entry") or {}).get("id") or ""),
                    path=str(((payload.get("entry") or {}).get("path") or "")),
                )
            except Exception as e:
                log(f"selection automation warning: could not mark duplicate archived: {e}")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    # Automatic admission: checked before any directory, media or content write.
    # An unreadable/invalid policy fails automatic add closed, never the manual path.
    if requested_collection == "auto" and selection_error:
        print(json.dumps({
            "status": "blocked",
            "url": url,
            "error": f"selection policy error: {selection_error}",
        }, ensure_ascii=False, indent=2))
        return 1
    if requested_collection == "auto" and selection_policy is not None:
        try:
            selection_module.admit(
                ROOT,
                selection_policy,
                url=url,
                content_file=args.content_file or None,
            )
        except Exception as e:
            print(json.dumps({
                "status": "blocked",
                "url": url,
                "error": f"selection admission blocked: {e}",
            }, ensure_ascii=False, indent=2))
            return 1

    # Validate raw-file if provided
    raw_file_path: Path | None = None
    if args.raw_file:
        rf_path = resolve_local_arg(args.raw_file)
        if not rf_path.exists() or not rf_path.is_file():
            auto_fail(f"raw-file not found: {args.raw_file}")
            print(json.dumps({"status": "error", "error": f"raw-file not found: {args.raw_file}"}, ensure_ascii=False, indent=2))
            return 1
        if not is_allowed_file_path(rf_path):
            auto_fail(f"raw-file path not allowed: {args.raw_file}")
            print(json.dumps({"status": "error", "error": f"raw-file path not allowed: {args.raw_file}"}, ensure_ascii=False, indent=2))
            return 1
        if rf_path.stat().st_size > 5 * 1024 * 1024:
            auto_fail(f"raw-file exceeds 5MB: {args.raw_file}")
            print(json.dumps({"status": "error", "error": f"raw-file exceeds 5MB limit: {args.raw_file}"}, ensure_ascii=False, indent=2))
            return 1
        raw_file_path = rf_path

    media_html: str | None = None
    if args.media_html_file:
        mh_path = resolve_local_arg(args.media_html_file)
        if not mh_path.exists() or not mh_path.is_file():
            print(json.dumps({"status": "error", "error": f"media-html-file not found: {args.media_html_file}"}, ensure_ascii=False, indent=2))
            return 1
        if not is_allowed_file_path(mh_path):
            print(json.dumps({"status": "error", "error": f"media-html-file path not allowed: {args.media_html_file}"}, ensure_ascii=False, indent=2))
            return 1
        if mh_path.stat().st_size > 20 * 1024 * 1024:
            print(json.dumps({"status": "error", "error": f"media-html-file exceeds 20MB limit: {args.media_html_file}"}, ensure_ascii=False, indent=2))
            return 1
        try:
            media_html = mh_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            print(json.dumps({"status": "error", "error": f"media-html-file UTF-8 decode failed: {e}"}, ensure_ascii=False, indent=2))
            return 1

    host = urlsplit(url).netloc.lower()
    raw_html: str | None = None
    extraction = "direct"
    content = ""
    status = "draft"
    content_note = ""
    meta = {"title": "", "source": "", "author": "", "published": "", "language": ""}
    raw_file: str | None = "raw.html.gz"
    media_report: dict = {}

    try:
        if args.content_file:
            cf_path = resolve_local_arg(args.content_file)
            if not cf_path.exists() or not cf_path.is_file():
                auto_fail(f"content-file not found: {args.content_file}")
                print(json.dumps({"status": "error", "error": f"content-file not found: {args.content_file}"}, ensure_ascii=False, indent=2))
                return 1
            if not is_allowed_file_path(cf_path):
                auto_fail(f"content-file path not allowed: {args.content_file}")
                print(json.dumps({"status": "error", "error": f"content-file path not allowed: {args.content_file}"}, ensure_ascii=False, indent=2))
                return 1
            if cf_path.stat().st_size > 5 * 1024 * 1024:
                auto_fail(f"content-file exceeds 5MB: {args.content_file}")
                print(json.dumps({"status": "error", "error": f"content-file exceeds 5MB limit: {args.content_file}"}, ensure_ascii=False, indent=2))
                return 1
            try:
                content = cf_path.read_text(encoding="utf-8").strip()
            except UnicodeDecodeError as e:
                auto_fail(f"content-file UTF-8 decode failed: {args.content_file}")
                print(json.dumps({"status": "error", "error": f"content-file UTF-8 decode failed: {e}"}, ensure_ascii=False, indent=2))
                return 1
            if not content:
                auto_fail(f"content-file is empty: {args.content_file}")
                print(json.dumps({"status": "error", "error": "content-file 为空"}, ensure_ascii=False, indent=2))
                return 1

            extraction = args.extraction or "web_extract"
            raw_file = None
            if extraction in ("web_extract", "exa"):
                content = strip_web_extract_header(content, args.original_title)
            content, had_truncation_note = strip_truncation_note(content)
            if had_truncation_note:
                status = "partial"
                content_note = "正文为 web_extract 截断输出（尾部截断提示已移除），全文不完整"
            elif extraction == "web_extract" and looks_truncated(content):
                status = "partial"
                content_note = f"正文在约 {len(content)} 字符处截断，末尾可能不完整"
            else:
                status = "draft"
                content_note = ""
            if media_html is not None:
                content, media_report = preserve_media(content, media_html, url)
            meta["title"] = args.original_title or ""
            meta["source"] = args.source or host
            meta["author"] = args.author
            meta["published"] = parse_date(args.published)
            meta["published_original"] = args.published_original
            meta["date_precision"] = args.date_precision
            meta["source_display_date"] = args.source_display_date
            meta["language"] = args.language or "en"
        elif "mp.weixin.qq.com" in host:
            r = fetch(url, referer="https://mp.weixin.qq.com/")
            check_web_content_type(r)
            raw_html = r.text
            meta, content, _ = wechat_extract(raw_html, url)
            content, media_report = preserve_media(content, raw_html, url)
        elif "openai.com" in host:
            print(json.dumps({
                "status": "needs_web_extract",
                "url": url,
                "reason": "openai.com 直连 403/Cloudflare；请用 web_extract 取正文后传 --content-file",
            }, ensure_ascii=False, indent=2))
            return 2
        else:
            r = fetch(url)
            check_web_content_type(r)
            raw_html = r.text
            meta, content = generic_extract(raw_html, url)
            content, media_report = preserve_media(content, raw_html, url)
    except Exception as e:
        auto_fail(str(e))
        print(json.dumps({"status": "error", "url": url, "error": str(e)}, ensure_ascii=False, indent=2))
        return 1

    media_report = dict(media_report or {})
    media_report.update(summarize_media(content))

    original_title = (args.original_title or meta.get("title") or "").strip()
    title_zh = args.title_zh.strip()
    language = args.language or meta.get("language") or ""
    if not language:
        language = "zh" if len(re.findall(r"[\u4e00-\u9fff]", content)) > 80 else "en"
    if language.lower().startswith("zh"):
        language = "zh"
    else:
        language = "en"

    display_title = title_zh or original_title or "未命名"
    title_status = "translated" if title_zh else ("original" if language == "zh" else "pending")

    published = parse_date(args.published) or parse_date(meta.get("published", "")) or now().isoformat(timespec="seconds")
    published_unknown = bool(args.published_unknown or not (args.published or meta.get("published")))
    valid_date_sources = {"feed", "list", "url", "page", "fetched"}
    if args.date_source:
        date_source = args.date_source.strip()
        if date_source not in valid_date_sources:
            print(f"warning: unknown date_source '{date_source}', recording as 'other'", file=sys.stderr)
            date_source = "other"
    elif published_unknown:
        date_source = "fetched"
    elif args.published:
        date_source = "feed"
    elif meta.get("published"):
        date_source = "page"
    else:
        date_source = "fetched"

    published_original = (args.published_original or meta.get("published_original") or "").strip()
    if not published_original and not published_unknown:
        published_original = (args.published or meta.get("published") or "").strip()

    source_display_date = (args.source_display_date or meta.get("source_display_date") or "").strip()

    date_precision = (args.date_precision or meta.get("date_precision") or "").strip()
    if not date_precision:
        if published_unknown:
            date_precision = "day"
        else:
            ref = published_original or args.published or meta.get("published", "")
            if re.search(r"[T\s]\d{1,2}:\d{2}", ref) or re.search(r"\b\d{1,2}:\d{2}:\d{2}\b", ref):
                date_precision = "datetime"
            else:
                date_precision = "day"

    if published_unknown:
        published_original = ""
        source_display_date = ""
        date_precision = "day"

    date_part = published[:10]

    # The source's own leading H1 (and any other body H1) is demoted here so the
    # entry never grows a second H1; hashing happens on this normalized text.
    content = normalize_body_headings(content, [original_title, display_title])
    content_hash = compute_content_hash(content)

    # --force: resolve the entry this run replaces (soft-moved to trash/, never deleted)
    old_entry: dict = {}
    old_dir: Path | None = None
    if args.force:
        old_record = (seen.get("entries") or {}).get(key) or {}
        # Only a real entry.md inside ROOT/library may be read or trashed.
        old_path = entry_path_in_library(old_record.get("path"))
        if old_path is not None:
            try:
                old_fm, _ = read_entry(old_path)
                old_entry = old_fm or {}
            except Exception:
                old_entry = {}
            old_dir = old_path.parent

    if not args.force:
        dup_path = find_entry_by_content_hash(content_hash) if len(content.strip()) >= CONTENT_HASH_MIN_CHARS else None
        if dup_path is not None:
            payload = {
                "status": "duplicate",
                "url": url,
                "entry": dup_path.parent.name,
                "path": str(dup_path),
                "matched_by": "content_hash",
            }
            with state_lock(ROOT):
                collection = ""
                promoted = False
                if requested_collection == "manual":
                    collection, promoted = promote_collection(
                        dup_path, reason=" (content-hash duplicate)"
                    )
                else:
                    try:
                        current_fm, _ = read_entry(dup_path)
                        collection = normalize_collection(current_fm.get("collection"))
                    except Exception as e:
                        log(f"duplicate: could not read {dup_path}: {e}")
                payload["collection"] = collection
                payload["promoted"] = promoted
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

    entry_id = f"{date_part}-{slugify(display_title)}"
    category = args.category or ""
    confidence = 1.0
    if category not in category_slugs():
        inherited_category = str(old_entry.get("category") or "")
        if inherited_category in category_slugs():
            category = inherited_category
            try:
                confidence = float(old_entry.get("confidence") or 1.0)
            except (TypeError, ValueError):
                confidence = 1.0
        else:
            category, confidence = classify(content, display_title)

    inherited_context = str(old_entry.get("context") or "")
    if args.context in context_slugs():
        context = args.context
    elif inherited_context in context_slugs():
        context = inherited_context
    else:
        context = "reference"

    # --force without --collection inherits the old value (never silently turns an
    # auto entry manual); an explicit auto never downgrades a manual entry.
    inherited_collection = normalize_collection(old_entry.get("collection"))
    if args.collection:
        collection = args.collection
        if collection == "auto" and inherited_collection == "manual":
            collection = "manual"
    else:
        collection = inherited_collection or "manual"

    if args.tags:
        tags = [t.strip() for t in re.split(r"[,，、;；]", args.tags) if t.strip()]
    else:
        tags = [str(t).strip() for t in (old_entry.get("tags") or []) if str(t).strip()]
    if args.category == "coding" and "coding" not in tags:
        tags.insert(0, "coding")

    subcategory = (args.subcategory or str(old_entry.get("subcategory") or "")).strip()

    if args.source_type:
        source_type = args.source_type
    elif extraction == "docs" or "docs.bigmodel.cn" in host:
        source_type = "docs"
    elif "mp.weixin.qq.com" in host:
        source_type = "wechat"
    elif any(d in host for d in ("openai.com", "anthropic.com", "kimi.com", "moonshot.ai", "zhipuai.cn", "bigmodel.cn")):
        source_type = "vendor_blog"
    else:
        source_type = "tech_blog"

    if old_dir is not None and old_dir.exists():
        try:
            with state_lock(ROOT):
                trash_path = move_to_trash(old_dir)
            log(f"force: previous entry moved to {trash_path}")
        except Exception as e:
            print(json.dumps({"status": "error", "url": url, "error": f"failed to move previous entry to trash: {e}"}, ensure_ascii=False, indent=2))
            return 1

    entry_dir = reserve_entry_dir(ROOT / "library" / category, entry_id)

    # Handle raw file snapshot
    if raw_file_path is not None:
        shutil.copy2(str(raw_file_path), str(entry_dir / raw_file_path.name))
        raw_file = raw_file_path.name
    elif raw_html is not None:
        with gzip.open(entry_dir / "raw.html.gz", "wb") as f:
            f.write(raw_html.encode("utf-8", "ignore"))
        raw_file = "raw.html.gz"
    else:
        raw_file = None

    # Localize images in markdown
    image_count = 0
    image_notes: list[str] = []
    if "![" in content:
        content, image_count, failed, image_notes = localize_images(
            content,
            url,
            entry_dir / "images",
            referer="https://mp.weixin.qq.com/" if "mp.weixin.qq.com" in host else None,
            budget=media_budget,
        )
        if failed:
            log(f"images: {image_count} ok, {failed} failed")

    image_note = "; ".join(image_notes) if image_notes else ""

    fm = {
        "id": entry_dir.name,
        "title": display_title,
        "original_title": original_title,
        "title_status": title_status,
        "source": args.source or meta.get("source") or host,
        "source_type": source_type,
        "author": args.author or meta.get("author") or "",
        "published": published,
        "date_source": date_source,
        "date_precision": date_precision,
        "published_unknown": published_unknown,
        "published_original": published_original,
        "source_display_date": source_display_date,
        "archived": iso(),
        "url": dedupe_key(url),
        "canonical_url": dedupe_key(url),
        "category": category,
        "subcategory": subcategory or None,
        "context": context,
        "collection": collection,
        "tags": tags,
        "summary_chars": 0,
        "status": status,
        "content_note": content_note,
        "content_hash": content_hash,
        "raw_file": raw_file,
        "images": image_count,
        "image_note": image_note,
        "extraction": extraction,
        "language": language,
        "confidence": round(confidence, 2),
        "model": "",
    }
    if old_entry.get("relevance"):
        fm["relevance"] = old_entry["relevance"]

    partial_banner = ""
    if status == "partial":
        partial_banner = f"> ⚠️ **部分抓取**：本文正文在约 {len(content)} 字符处截断，以下内容不完整。\n\n"

    body = f"""# {display_title}

> 来源：{fm['source']} · 作者：{fm['author'] or '—'} · 发布：{published[:10]} · [原文]({fm['url']})

## 摘要

（待生成）

## 全文

{partial_banner}{content}
"""

    rel_path = str((entry_dir / "entry.md").relative_to(ROOT))
    with state_lock(ROOT):
        # Re-read under the lock: image downloads above may have taken a while and
        # a concurrent run may have registered this URL in the meantime.
        seen = load_seen()
        entries = seen.setdefault("entries", {})
        existing = entries.get(key) or {}
        # Validate the recorded path: only a real entry.md inside ROOT/library is
        # ever promoted or compared against our own reserved directory.
        existing_abs = entry_path_in_library(existing.get("path"))
        if not args.force and existing_abs is not None:
            # Our copy is the loser of the race.  Windows are reserved atomically,
            # so this is our own directory; move only it to trash.  If the winner
            # reused this exact path (simulated in tests), leave it untouched.
            if existing_abs.parent.resolve() != entry_dir.resolve():
                try:
                    trashed = move_to_trash(entry_dir)
                    log(f"duplicate race: own copy moved to {trashed}")
                except Exception as e:
                    log(f"duplicate race: could not move own copy to trash: {e}")
            collection = normalize_collection(existing.get("collection"))
            if not collection:
                try:
                    existing_fm, _ = read_entry(existing_abs)
                    collection = normalize_collection(existing_fm.get("collection"))
                except Exception as e:
                    log(f"duplicate race: could not read {existing_abs}: {e}")
            promoted = False
            if requested_collection == "manual":
                collection, promoted = promote_collection(
                    existing_abs, reason=" (duplicate race)"
                )
            print(json.dumps({
                "status": "duplicate",
                "url": url,
                "entry": existing.get("id") or "",
                "path": str(existing_abs),
                "collection": collection or "",
                "promoted": promoted,
            }, ensure_ascii=False, indent=2))
            return 0

        write_entry(entry_dir / "entry.md", fm, body)
        entries[key] = {
            "id": entry_dir.name,
            "path": rel_path,
            "first_seen": existing.get("first_seen") or iso(),
        }
        save_seen(seen)

    if selection_policy is not None:
        try:
            selection_module.mark_archived(
                ROOT,
                selection_policy,
                url=url,
                entry_id=entry_dir.name,
                path=str(entry_dir / "entry.md"),
                manual=(requested_collection == "manual"),
            )
        except Exception as e:
            log(f"selection automation warning: could not record archive outcome: {e}")

    print(json.dumps({
        "status": "ok",
        "id": entry_dir.name,
        "path": str(entry_dir / "entry.md"),
        "title": display_title,
        "original_title": original_title,
        "title_status": title_status,
        "source": fm["source"],
        "author": fm["author"],
        "published": published,
        "date_source": date_source,
        "date_precision": date_precision,
        "published_unknown": published_unknown,
        "published_original": published_original,
        "source_display_date": source_display_date,
        "language": language,
        "category": category,
        "context": context,
        "collection": collection,
        "tags": tags,
        "chars": len(content),
        "images": image_count,
        "image_note": image_note,
        "media_budget": media_budget.as_dict(),
        "media": media_report,
        "extraction": extraction,
        "confidence": fm["confidence"],
        "entry_status": status,
        "content_note": content_note,
        "needs_summary": True,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
