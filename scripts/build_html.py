#!/usr/bin/env python3
"""Standalone offline HTML index generator for ContentArchive.

Generates a self-contained, reader-focused Explore HTML snapshot
without external dependencies (no CDN, no fetch, no external fonts).
"""
from __future__ import annotations

import html
import json
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Ensure scripts directory is on sys.path for common imports
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from common import TZ, iso, normalize_body_headings, now, read_entry, summary_section
from markdown_it import MarkdownIt
from media_markup import install_media_renderer

TEMPLATE_PATH = SCRIPTS_DIR / "archive_page.html"

ALLOWED_IMAGE_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".svg",
    ".avif",
    ".bmp",
    ".ico",
}

SVG_ALLOWED_TAGS = {
    "svg",
    "g",
    "defs",
    "path",
    "rect",
    "circle",
    "line",
    "polyline",
    "polygon",
    "ellipse",
    "text",
    "tspan",
    "textpath",
    "pattern",
    "clippath",
    "mask",
    "marker",
    "lineargradient",
    "radialgradient",
    "stop",
    "desc",
    "title",
    "metadata",
}


def _clean_tag_name(tag: str) -> str:
    if "}" in tag:
        tag = tag.split("}", 1)[1]
    if ":" in tag:
        tag = tag.split(":", 1)[1]
    return tag.lower()


def _is_safe_svg_file(path: Path) -> bool:
    """Validate that the SVG file contains only static XML graphics without scripts/events/external refs."""
    try:
        data = path.read_bytes()
    except Exception:
        return False

    if not data or len(data) > 20_000_000:
        return False

    lower_data = data.lower()
    # Reject DOCTYPE, entity declarations, XML stylesheets, processing instructions
    if (
        re.search(rb"<!\s*doctype", lower_data)
        or re.search(rb"<!\s*entity", lower_data)
        or re.search(rb"<\?\s*xml-stylesheet", lower_data)
    ):
        return False

    try:
        root = ET.fromstring(data)
    except Exception:
        return False

    if _clean_tag_name(root.tag) != "svg":
        return False

    for elem in root.iter():
        tag = _clean_tag_name(elem.tag)
        if tag not in SVG_ALLOWED_TAGS:
            return False

        for attr, val in elem.attrib.items():
            attr_name = _clean_tag_name(attr)
            if attr_name.startswith("on"):
                return False

            if attr_name in ("href", "src") or attr_name.endswith(":href"):
                if not val.startswith("#"):
                    return False

            val_lower = val.lower()
            if any(
                scheme in val_lower
                for scheme in (
                    "javascript:",
                    "data:",
                    "http:",
                    "https:",
                    "file:",
                    "vbscript:",
                    "blob:",
                )
            ):
                return False

            if "//" in val_lower:
                return False

            if "expression" in val_lower or "@import" in val_lower:
                return False

            if "url(" in val_lower:
                urls = re.findall(r"url\s*\((.*?)\)", val_lower)
                for u in urls:
                    u_clean = u.strip("\"' \t\r\n")
                    if not u_clean.startswith("#") or not re.match(r"^#[a-zA-Z0-9_\-]+$", u_clean):
                        return False

    return True


def _sanitize_original_url(url: Any) -> str:
    """Return validated http(s) URL with hostname and without control characters."""
    if not url or not isinstance(url, str):
        return ""
    clean = url.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in clean):
        return ""
    try:
        parsed = urllib.parse.urlsplit(clean)
    except Exception:
        return ""
    if parsed.scheme.lower() not in ("http", "https"):
        return ""
    if not parsed.hostname or not parsed.netloc:
        return ""
    return clean


def _resolve_safe_local_image(rel_or_path: str, entry_dir: Path | None, root: Path | None) -> Path | None:
    """Resolve and validate a relative image inside the entry directory."""
    if not entry_dir or not root or not rel_or_path:
        return None
    parsed = urllib.parse.urlsplit(rel_or_path)
    if parsed.scheme:
        return None
    unquoted = urllib.parse.unquote(parsed.path)
    if unquoted.startswith(("/", "\\")) or (len(unquoted) > 1 and unquoted[1] == ":"):
        return None
    try:
        target = (entry_dir / unquoted).resolve()
    except Exception:
        return None
    if not target.is_relative_to(entry_dir.resolve()):
        return None
    if not target.is_file():
        return None
    suffix = target.suffix.lower()
    if suffix not in ALLOWED_IMAGE_EXTS:
        return None
    if suffix == ".svg":
        if not _is_safe_svg_file(target):
            return None
    return target


def _clean_plain_text(md_text: str) -> str:
    """Extract plain text for substring searching from markdown content."""
    # Strip code fences and inline code
    text = re.sub(r"```[\s\S]*?```", " ", md_text)
    text = re.sub(r"`[^`]+`", " ", text)
    # Strip image tokens, keep alt text
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r" \1 ", text)
    # Strip markdown link syntax, keep link text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r" \1 ", text)
    # Strip markdown symbols
    text = re.sub(r"[#*~_>|`]", " ", text)
    # Collapse multiple whitespace
    return re.sub(r"\s+", " ", text).strip()


def _safe_json_embed(obj: Any) -> str:
    """Serialize object to JSON safely for inlining inside a <script> tag.

    Replaces '<' with '\\u003c', '>' with '\\u003e', and '&' with '\\u0026'
    to prevent closing the script tag or injecting HTML.
    """
    s = json.dumps(obj, ensure_ascii=False)
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


_DATE_ONLY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DATETIME_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?"
    r"\s*(Z|[+-]\d{2}:?\d{2})?$"
)
_SORT_KEY_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?Z$"
)
_EPOCH_DATE = date(1970, 1, 1)


def _parse_date_fields(raw: Any) -> tuple[str, str, bool]:
    """Return ``(display_date, sort_key, valid)`` for a stored date value.

    ``display_date`` is the calendar date as written by the source, so a
    timestamp such as ``2026-01-09T23:30:00-05:00`` stays on 2026-01-09 and no
    time of day is invented. ``sort_key`` is a UTC-normalised string so
    catalogs written in different offsets compare correctly; a date-only value
    keeps date-only precision (``2026-01-05``) instead of becoming midnight.
    Naive timestamps are interpreted in the archive timezone (the same ``TZ``
    that ``common.iso`` writes), and anything unparseable is reported invalid.

    Malformed metadata must never abort a build: offsets outside ``00:00`` ..
    ``23:59`` and UTC conversions that leave the representable year range are
    rejected as invalid. Sub-second digits are kept (canonically padded to
    nine) so ordering never collapses two values onto the same whole second.
    """
    text = str(raw or "").strip()
    if not text:
        return ("", "", False)
    if _DATE_ONLY_RE.match(text):
        try:
            day = datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            return ("", "", False)
        return (day.isoformat(), day.isoformat(), True)
    match = _DATETIME_RE.match(text)
    if not match:
        return ("", "", False)
    fraction = match.group(7) or ""
    nanos = int(fraction.ljust(9, "0")) if fraction else 0
    offset = match.group(8)
    if offset in (None, ""):
        tzinfo = TZ
    elif offset == "Z":
        tzinfo = timezone.utc
    else:
        sign = -1 if offset[0] == "-" else 1
        digits = offset[1:].replace(":", "")
        hours = int(digits[:2])
        minutes = int(digits[2:4])
        # ``+02:99`` is not 3h39m; only real UTC offsets are accepted.
        if hours > 23 or minutes > 59:
            return ("", "", False)
        try:
            tzinfo = timezone(sign * timedelta(hours=hours, minutes=minutes))
        except ValueError:
            return ("", "", False)
    try:
        moment = datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
            int(match.group(5)),
            int(match.group(6) or 0),
            tzinfo=tzinfo,
        )
        utc_moment = moment.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        # Includes years whose UTC conversion falls outside 0001..9999.
        return ("", "", False)
    if not 1 <= utc_moment.year <= 9999:
        return ("", "", False)
    sort_key = (
        f"{utc_moment.year:04d}-{utc_moment.month:02d}-{utc_moment.day:02d}"
        f"T{utc_moment.hour:02d}:{utc_moment.minute:02d}:{utc_moment.second:02d}"
    )
    if nanos:
        sort_key += f".{nanos:09d}"
    return (moment.date().isoformat(), sort_key + "Z", True)


def _sort_compare_value(value: str) -> tuple[int, int, int, int]:
    """Map an exported sort key to a comparable ``(known, seconds, nanos, rank)``.

    The exported string keeps its compact whole-second shape, but comparisons
    must not be plain string comparisons: ``"…:00Z"`` sorts after ``"…:00.1Z"``
    byte-wise even though the whole second is earlier. Date-only values keep
    lower rank than an equal-day timestamp, matching their historical
    precision-first ordering.
    """
    if not value:
        return (0, 0, 0, 0)
    match = _SORT_KEY_RE.match(value)
    if match:
        try:
            day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return (0, 0, 0, 0)
        seconds = (
            (day - _EPOCH_DATE).days * 86400
            + int(match.group(4)) * 3600
            + int(match.group(5)) * 60
            + int(match.group(6))
        )
        fraction = match.group(7) or ""
        nanos = int(fraction.ljust(9, "0")) if fraction else 0
        return (1, seconds, nanos, 1)
    if _DATE_ONLY_RE.match(value):
        try:
            day = date.fromisoformat(value)
        except ValueError:
            return (0, 0, 0, 0)
        return (1, (day - _EPOCH_DATE).days * 86400, 0, 0)
    return (0, 0, 0, 0)


def _create_markdown_renderer() -> MarkdownIt:
    """Initialize MarkdownIt with table, strikethrough, and secure media rules."""
    md = (
        MarkdownIt(options_update={"html": False})
        .enable("table")
        .enable("strikethrough")
    )

    def _render_image(tokens, idx, options, env):
        token = tokens[idx]
        src = token.attrs.get("src", "").strip()
        alt = token.content or ""
        title = token.attrs.get("title")
        title_attr = f' title="{html.escape(title, quote=True)}"' if title else ""
        root: Path | None = env.get("root")
        entry_dir: Path | None = env.get("entry_dir")

        if not src:
            return f'<span class="img-missing">[图片缺失: {html.escape(alt)}]</span>'

        # Check URL scheme
        parsed = urllib.parse.urlsplit(src)
        if parsed.scheme.lower() in ("http", "https"):
            # Remote images become safe text external links with target="_blank" and rel="noopener noreferrer"
            escaped_url = html.escape(src, quote=True)
            link_text = html.escape(alt if alt.strip() else "查看外部原图")
            return (
                f'<a href="{escaped_url}" class="ext-img-link" '
                f'target="_blank" rel="noopener noreferrer"{title_attr}>'
                f'🖼️ [图片: {link_text}]</a>'
            )

        # Any other protocol (data:, javascript:, file:, ftp:, etc.) is strictly forbidden
        if parsed.scheme:
            return '<span class="img-forbidden">[禁止的图片链接]</span>'

        # Absolute paths (leading slash or Windows drive like C:) are forbidden
        unquoted_path = urllib.parse.unquote(parsed.path)
        if unquoted_path.startswith(("/", "\\")) or (len(unquoted_path) > 1 and unquoted_path[1] == ":"):
            return '<span class="img-forbidden">[禁止的绝对路径图片]</span>'

        # Local image inside article directory check
        if not entry_dir or not root:
            return '<span class="img-forbidden">[无效的环境路径]</span>'

        try:
            target = (entry_dir / unquoted_path).resolve()
        except Exception:
            return '<span class="img-forbidden">[无效图片路径]</span>'

        # Must reside strictly within entry_dir (path traversal protection)
        if not target.is_relative_to(entry_dir.resolve()):
            return '<span class="img-forbidden">[禁止的越界图片路径]</span>'

        # Must be a real regular file on disk
        if not target.is_file():
            return f'<span class="img-missing">[图片缺失: {html.escape(alt)}]</span>'

        suffix = target.suffix.lower()
        if suffix not in ALLOWED_IMAGE_EXTS:
            return '<span class="img-forbidden">[禁止的图片类型]</span>'

        # SVG security check
        if suffix == ".svg":
            if not _is_safe_svg_file(target):
                return '<span class="img-unsafe">[不安全的SVG图片]</span>'

        # Convert to POSIX path relative to repository root (portable, forward slashes)
        try:
            rel = target.relative_to(root.resolve())
        except ValueError:
            return '<span class="img-forbidden">[禁止的越界图片路径]</span>'

        quoted_rel = urllib.parse.quote(rel.as_posix(), safe="/")
        if parsed.query:
            quoted_rel += "?" + parsed.query
        if parsed.fragment:
            quoted_rel += "#" + parsed.fragment

        return (
            f'<img src="{html.escape(quoted_rel, quote=True)}" '
            f'alt="{html.escape(alt, quote=True)}"{title_attr} loading="lazy" />'
        )

    def _render_link_open(tokens, idx, options, env):
        token = tokens[idx]
        href = token.attrs.get("href", "").strip()
        root: Path | None = env.get("root")
        entry_dir: Path | None = env.get("entry_dir")

        if href.startswith("#"):
            return f'<a href="{html.escape(href, quote=True)}">'

        parsed = urllib.parse.urlsplit(href)
        if parsed.scheme.lower() in ("http", "https"):
            safe_href = _sanitize_original_url(href)
            if safe_href:
                return f'<a href="{html.escape(safe_href, quote=True)}" target="_blank" rel="noopener noreferrer">'
            token.meta = {"blocked": True}
            return '<span class="disabled-link" title="链接不可用">'

        # Block dangerous schemes and absolute links
        if parsed.scheme or href.startswith(("/", "\\")) or (len(href) > 1 and href[1] == ":"):
            token.meta = {"blocked": True}
            return '<span class="disabled-link" title="链接不可用">'

        # Relative link must only point to a safe local image within the same entry dir
        target = _resolve_safe_local_image(href, entry_dir, root)
        if target is not None:
            try:
                rel = target.relative_to(root.resolve())
            except ValueError:
                token.meta = {"blocked": True}
                return '<span class="disabled-link" title="链接不可用">'

            quoted_rel = urllib.parse.quote(rel.as_posix(), safe="/")
            if parsed.query:
                quoted_rel += "?" + parsed.query
            if parsed.fragment:
                quoted_rel += "#" + parsed.fragment
            return f'<a href="{html.escape(quoted_rel, quote=True)}">'

        token.meta = {"blocked": True}
        return '<span class="disabled-link" title="链接不可用">'

    def _render_link_close(tokens, idx, options, env):
        depth = 0
        for i in range(idx - 1, -1, -1):
            if tokens[i].type == "link_close":
                depth += 1
            elif tokens[i].type == "link_open":
                if depth == 0:
                    if tokens[i].meta and tokens[i].meta.get("blocked"):
                        return "</span>"
                    return "</a>"
                depth -= 1
        return "</a>"

    md.renderer.rules["image"] = _render_image
    md.renderer.rules["link_open"] = _render_link_open
    md.renderer.rules["link_close"] = _render_link_close
    install_media_renderer(md)

    return md


def build_html(root: Path, rows: list[dict], labels: dict[str, str]) -> str:
    """Build a standalone offline Explore HTML document for the archive.

    Parameters:
        root: Repository root Path.
        rows: List of article index row dicts with summary, collection, etc.
        labels: Dict mapping category slugs to Chinese labels.

    Returns:
        Complete self-contained HTML page as string.
    """
    root_path = Path(root).resolve()
    library_dir = (root_path / "library").resolve()
    md_renderer = _create_markdown_renderer()

    articles_data: list[dict[str, Any]] = []

    for row in rows:
        row_id = str(row.get("id") or "")
        row_path = row.get("path")
        if not row_path:
            raise ValueError(f"Entry path missing for row id {row_id!r}")

        entry_file = (root_path / row_path).resolve()
        if not entry_file.is_relative_to(library_dir):
            raise ValueError(f"Entry path {row_path!r} resolves outside library directory: {entry_file}")
        if entry_file.name != "entry.md":
            raise ValueError(f"Entry file must be entry.md, got {entry_file.name!r} for row id {row_id!r}")
        if not entry_file.is_file():
            raise ValueError(f"Entry file does not exist: {entry_file}")

        try:
            fm, body_raw = read_entry(entry_file)
        except Exception as exc:
            raise ValueError(f"Failed to read/parse entry {entry_file}: {exc}") from exc

        entry_dir = entry_file.parent

        title = str(row.get("title") or fm.get("title") or "")
        orig_title = str(row.get("original_title") or fm.get("original_title") or "")

        # Heading normalization: strip leading H1 if it duplicates title
        norm_body = normalize_body_headings(body_raw, [title, orig_title])

        # Separate summary and full text if standard structure is found
        summary = str(row.get("summary") or summary_section(norm_body) or "")
        cleaned_body = norm_body

        m_summary = re.search(r"(##\s*摘要\s*\n.*?\n)(##\s*全文\s*\n?)", norm_body, re.S)
        if m_summary:
            if not summary:
                summary = m_summary.group(1).replace("## 摘要", "").strip()
            # Strip the ## 摘要 block and ## 全文 header so summary callout is not duplicated
            cleaned_body = norm_body[: m_summary.start()] + norm_body[m_summary.end() :]

        # Render markdown to HTML safely
        env = {"root": root_path, "entry_dir": entry_dir}
        body_html = md_renderer.render(cleaned_body, env)

        # Tags extraction
        raw_tags = row.get("tags") or fm.get("tags") or []
        if isinstance(raw_tags, list):
            tags_list = [str(t).strip() for t in raw_tags if str(t).strip()]
        elif isinstance(raw_tags, str):
            tags_list = [t.strip() for t in raw_tags.split(",") if t.strip()]
        else:
            tags_list = []
        tags_str = " ".join(tags_list)

        # Collection resolution: manual | auto | unlabeled (never fake manual)
        raw_collection = row.get("collection") or fm.get("collection")
        if raw_collection in ("manual", "auto"):
            collection = raw_collection
        else:
            collection = ""  # unlabeled

        # Dates: the frontmatter keeps the full timestamp, while index.csv only
        # carries a truncated date. Prefer the frontmatter, show the source's
        # calendar date, and keep a separate UTC sort key.
        published_raw = str(fm.get("published") or row.get("published") or "").strip()
        published_date, published_sort, published_valid = _parse_date_fields(published_raw)
        archived_raw = str(row.get("archived") or fm.get("archived") or "")
        _, archived_sort, _ = _parse_date_fields(archived_raw)
        date_source = fm.get("date_source")
        pub_unknown = bool(
            row.get("published_unknown")
            or fm.get("published_unknown")
            or (date_source == "unknown")
            # ``fetched`` marks a crawl-date fallback; older records may not
            # carry ``published_unknown`` yet, so the source alone is enough.
            or (date_source == "fetched")
            or not published_valid
        )
        # An unknown date has no trustworthy position: keep it out of the
        # published ordering instead of sorting the placeholder value.
        published_sort_key = "" if pub_unknown else published_sort

        status = str(row.get("status") or fm.get("status") or "archived")
        content_note = str(row.get("content_note") or fm.get("content_note") or "")
        image_note = str(row.get("image_note") or fm.get("image_note") or "")

        # Plain body text for search
        # Keep full code and text without mangling identifiers
        search_text = f"{title} {orig_title} {summary} {tags_str} {norm_body}".lower()

        cat_slug = str(row.get("category") or fm.get("category") or "other")
        cat_label = labels.get(cat_slug, cat_slug)

        raw_url = str(row.get("url") or fm.get("url") or "")
        safe_url = _sanitize_original_url(raw_url)

        articles_data.append(
            {
                "id": row_id,
                "title": title,
                "original_title": orig_title,
                "published": published_date,
                "published_unknown": pub_unknown,
                "published_sort": published_sort_key,
                "archived": archived_raw,
                "archived_sort": archived_sort,
                "source": str(row.get("source") or fm.get("source") or "其他"),
                "source_type": str(row.get("source_type") or fm.get("source_type") or ""),
                "category": cat_slug,
                "category_label": cat_label,
                "subcategory": str(row.get("subcategory") or fm.get("subcategory") or ""),
                "context": str(row.get("context") or fm.get("context") or ""),
                "tags": tags_list,
                "url": safe_url,
                "status": status,
                "content_note": content_note,
                "image_note": image_note,
                "collection": collection,
                "summary": summary,
                "body_html": body_html,
                "search_text": search_text,
            }
        )

    # Default exported order: archived instant descending, then published,
    # then id ascending for deterministic ties. The reader re-sorts in the
    # browser, but the embedded catalog stays stable for non-JS consumers.
    articles_data.sort(key=lambda article: article["id"])
    articles_data.sort(
        key=lambda article: (
            _sort_compare_value(article["archived_sort"]),
            _sort_compare_value(article["published_sort"]),
        ),
        reverse=True,
    )

    # Load HTML template
    template_text = TEMPLATE_PATH.read_text(encoding="utf-8")

    # Embed JSON safely
    archive_json = _safe_json_embed(articles_data)
    labels_json = _safe_json_embed(labels)
    build_time_str = iso(now())

    replacements = {
        "{{ARCHIVE_DATA}}": archive_json,
        "{{LABELS_DATA}}": labels_json,
        "{{BUILD_TIME}}": html.escape(build_time_str),
    }
    # Substitute only original template matches, never tokens inside article data.
    return re.sub(
        r"\{\{(?:ARCHIVE_DATA|LABELS_DATA|BUILD_TIME)\}\}",
        lambda match: replacements[match.group(0)],
        template_text,
    )
