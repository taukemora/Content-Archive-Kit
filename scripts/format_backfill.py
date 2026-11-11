#!/usr/bin/env python3
"""Bounded, read-only format backfill planner and candidate preparer.

``plan`` compares each archived WeChat entry with two reconstructions of its raw
snapshot: a *legacy* Markdown conversion (raw DOM only, no emphasis
normalisation) and the current ``archive_url.wechat_extract`` output.  Only
emphasis markers are allowed to differ between them; every difference must be
grounded in an exactly matched text span, so narrative, links, images, video
cards, code and captions cannot change.

``prepare`` never trusts candidate text stored in a plan: it re-runs the same
deterministic comparison against the source, verifies entry/raw/candidate
hashes, and only then writes a brand-new destination tree with byte-identical
raw/media copies.  Nothing is written to the source archive, ``.state`` or any
index.

Deliberate refusals (reported per entry, never "fixed"): missing or incomplete
raw snapshots, extraction drift beyond formatting, edited narrative, duplicate
or ambiguous spans, unchecked heading promotions, oversized/invalid raw paths.

Usage::

    python scripts/format_backfill.py plan --source ARCHIVE --out PLAN.json \\
        [--selections selections.json]
    python scripts/format_backfill.py prepare --source ARCHIVE --plan PLAN.json \\
        --dest NEW_DIR

``selections.json`` maps a relative ``entry.md`` path to exact standalone
heading texts from the raw snapshot.  A text is promoted only when the raw
snapshot has exactly one standalone block that is a native heading or one bold
run and the entry has exactly one matching Markdown block; the promoted level
is ``##``.  A short bold paragraph is never treated as a heading otherwise.
"""
from __future__ import annotations

import argparse
import difflib
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import zlib
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString
from markdownify import markdownify as md

import archive_url
from common import compute_content_hash, split_frontmatter
from media_markup import (
    CAPTION_PREFIX,
    finalize_media_markdown,
    prepare_wechat_media,
)

# Public checkout boundary: derived from this file, never hard-coded.  Outputs
# must never land inside it (it may be the shipped library/docs tree).
SCRIPT_ROOT = Path(__file__).resolve().parents[1]
TOOL = "format_backfill"
PLAN_VERSION = 1
STATUSES = ("changed", "unchanged", "needs_review", "unsupported")
MAX_RAW_BYTES = 5 * 1024 * 1024  # same ceiling the intake command applies
TZ = timezone(timedelta(hours=8))
HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
BLOCK_TAGS = frozenset({"p", "li", "blockquote", "div", "section", "h1", "h2", "h3", "h4", "h5", "h6"})
_ATX_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_IMAGE_TARGET_RE = re.compile(r"!\[[^\]\n]*\]\(([^)\n]*)\)")
_WEIGHT_RE = re.compile(r"(?:^|;)\s*font-weight\s*:\s*([^;}]+)", re.I)
_WEIGHT_KEYWORDS = {"bold": True, "bolder": True, "normal": False, "lighter": False}


class BackfillError(Exception):
    """A refusal that must stop the command (never silently degrade)."""


class UnsupportedSource(BackfillError):
    """The entry cannot be backfilled at all (missing/inapplicable source)."""


class NeedsReview(BackfillError):
    """The entry is ambiguous or diverged from its snapshot; refuse to guess."""



def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Text plumbing: chunks, masking, marker edits
# ---------------------------------------------------------------------------


def split_chunks(text: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` ranges of blank-line separated chunks.

    Blank lines inside fenced code blocks never split a chunk, so literal
    Markdown in a snippet stays one unit.  Ranges exclude surrounding newlines.
    """
    if not text:
        return []
    ranges: list[tuple[int, int]] = []
    offset = 0
    start: int | None = None
    end = 0
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        marker = _FENCE_RE.match(body)
        if marker:
            fence_len = len(marker.group(1))
            if fence is None:
                fence = marker.group(1)[0] * fence_len
            elif marker.group(1)[0] == fence[0] and fence_len >= len(fence):
                fence = None
        blank = not body.strip() and fence is None
        if blank:
            if start is not None:
                ranges.append((start, end))
                start = None
        else:
            if start is None:
                start = offset
            end = offset + len(line) - (len(line) - len(body))
        offset += len(line)
    if start is not None:
        ranges.append((start, end))
    return ranges


def chunk_texts(text: str) -> list[str]:
    return [text[s:e] for s, e in split_chunks(text)]


def mask_image_targets(text: str) -> str:
    """Replace image link targets with NULs, preserving length and offsets.

    Local archives keep ``images/x.png`` while a fresh extraction produces the
    remote URL; masking lets spans align without ever writing a remote target.
    """
    out = []
    last = 0
    for match in _IMAGE_TARGET_RE.finditer(text):
        inner = match.group(1)
        out.append(text[last:match.start(1)])
        out.append("\x00" * len(inner))
        last = match.end(1)
    out.append(text[last:])
    return "".join(out)


def image_key(text: str) -> str:
    """Comparison key with image targets neutralised (alt text is preserved)."""
    return re.sub(r"!\[([^\]\n]*)\]\([^)\n]*\)", r"![\1](IMAGE)", text)


def _mask_spans(text: str) -> list[tuple[int, int]]:
    return [m.span(1) for m in _IMAGE_TARGET_RE.finditer(text)]



def strip_emphasis(text: str) -> str:
    """Remove unescaped ``*`` runs outside inline code spans.

    Escaped stars (``\\*``) and code spans are literal content and survive.
    """
    out: list[str] = []
    i = 0
    fence_len = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == "\\" and i + 1 < n:
            out.append(text[i : i + 2])
            i += 2
            continue
        if char == "`":
            run = 1
            while i + run < n and text[i + run] == "`":
                run += 1
            if fence_len == 0:
                fence_len = run
            elif run == fence_len:
                fence_len = 0
            out.append("`" * run)
            i += run
            continue
        if char == "*" and fence_len == 0:
            i += 1
            continue
        out.append(char)
        i += 1
    return "".join(out)


def heading_split(chunk: str) -> tuple[int, str]:
    """Return ``(level, text)`` for an ATX heading chunk, else ``(0, chunk)``."""
    match = _ATX_RE.match(chunk)
    if not match:
        return 0, chunk
    return len(match.group(1)), match.group(2)


def _edit_touches_image(i1: int, i2: int, spans: list[tuple[int, int]]) -> bool:
    for start, end in spans:
        if i1 == i2:
            if start < i1 < end:
                return True
        elif i1 < end and i2 > start:
            return True
    return False


def _strict_marker_edits(
    old_masked: str, new_masked: str, spans: list[tuple[int, int]]
) -> list[tuple[int, int, str]] | None:
    edits: list[tuple[int, int, str]] = []
    matcher = difflib.SequenceMatcher(None, old_masked, new_masked, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed, added = old_masked[i1:i2], new_masked[j1:j2]
        if removed and set(removed) != {"*"}:
            return None
        if added and set(added) != {"*"}:
            return None
        if not removed and not added:
            continue
        for start, end in spans:
            if start < i1 < end or (i1 == i2 and start < i1 < end):
                return None
        edits.append((i1, i2, added))
    return edits


def _literal_star_spans(text: str) -> list[tuple[int, int]]:
    """Spans of escaped stars and inline code where ``*`` is literal content."""
    spans: list[tuple[int, int]] = []
    index = 0
    fence_len = 0
    start = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            if text[index + 1] == "*":
                spans.append((index, index + 2))
            index += 2
            continue
        if char == "`":
            run = 1
            while index + run < len(text) and text[index + run] == "`":
                run += 1
            if fence_len == 0:
                fence_len, start = run, index
            elif run == fence_len:
                spans.append((start, index + run))
                fence_len = 0
            index += run
            continue
        index += 1
    return spans


def _edit_touches_literal(i1: int, i2: int, spans: list[tuple[int, int]]) -> bool:
    for start, end in spans:
        if i1 == i2:
            if start < i1 < end:
                return True
        elif i1 < end and i2 > start:
            return True
    return False


def _relocated_marker_edits(
    old_masked: str, new_masked: str, spans: list[tuple[int, int]]
) -> list[tuple[int, int, str]] | None:
    """Marker-only edits when punctuation moved across an emphasis boundary.

    ``difflib`` matches the shared ``*`` runs first, so ``**文｜**合成作者`` to
    ``**文**｜合成作者`` looks like a punctuation move and is refused.  Treating
    ``*`` as junk aligns the unchanged narrative instead, exposing only star
    insert/delete operations.  Every removed/added span must still be stars,
    image targets stay protected, and the edits must reconstruct the new text
    exactly; anything else is refused.
    """
    matcher = difflib.SequenceMatcher(
        lambda ch: ch == "*", old_masked, new_masked, autojunk=False
    )
    literal_spans = _literal_star_spans(old_masked)
    edits: list[tuple[int, int, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed, added = old_masked[i1:i2], new_masked[j1:j2]
        if removed and set(removed) != {"*"}:
            return None
        if added and set(added) != {"*"}:
            return None
        if not removed and not added:
            continue
        if _edit_touches_image(i1, i2, spans):
            return None
        if _edit_touches_literal(i1, i2, literal_spans):
            return None
        edits.append((i1, i2, added))
    if apply_edits(old_masked, edits) != new_masked:
        return None
    return edits


def marker_edits(old: str, new: str) -> list[tuple[int, int, str]] | None:
    """Return offset edits turning ``old`` into ``new``, or None when unsafe.

    Only insertions/deletions made purely of ``*`` are accepted, and no edit may
    start inside a masked image target.  The bounded fallback is used only when
    the default alignment refuses a punctuation-relocated emphasis boundary.
    """
    old_masked = mask_image_targets(old)
    new_masked = mask_image_targets(new)
    spans = _mask_spans(old_masked)
    edits = _strict_marker_edits(old_masked, new_masked, spans)
    if edits is None:
        return _relocated_marker_edits(old_masked, new_masked, spans)
    return edits


def apply_edits(text: str, edits: list[tuple[int, int, str]]) -> str:
    out = text
    for start, end, added in sorted(edits, key=lambda e: e[0], reverse=True):
        out = out[:start] + added + out[end:]
    return out


def surgical_chunk(body_chunk: str, new_chunk: str) -> str | None:
    """Return ``body_chunk`` with only the new emphasis markers, or None.

    Chunks without images are patched at exact offsets.  A chunk that embeds an
    image has a different target length in the archive (local file) than in the
    fresh extraction (remote URL), so offsets after that image would shift;
    there the new text is taken and every image token is replaced by the
    archive's own token at the same position.  The caller verifies the result.
    """
    if not _IMAGE_TARGET_RE.search(body_chunk) and not _IMAGE_TARGET_RE.search(new_chunk):
        return apply_edits(body_chunk, marker_edits(body_chunk, new_chunk) or [])
    body_tokens = [m.group(0) for m in _IMAGE_TARGET_RE.finditer(body_chunk)]
    new_tokens = list(_IMAGE_TARGET_RE.finditer(new_chunk))
    if len(body_tokens) != len(new_tokens):
        return None
    out: list[str] = []
    last = 0
    for token, match in zip(body_tokens, new_tokens):
        body_alt = re.match(r"!\[([^\]\n]*)\]", token)
        new_alt = re.match(r"!\[([^\]\n]*)\]", match.group(0))
        if body_alt is None or new_alt is None or body_alt.group(1) != new_alt.group(1):
            return None
        out.append(new_chunk[last:match.start()])
        out.append(token)
        last = match.end()
    out.append(new_chunk[last:])
    return "".join(out)


# ---------------------------------------------------------------------------
# Source reconstruction and entry analysis
# ---------------------------------------------------------------------------


def legacy_wechat_markdown(html: str, url: str) -> str:
    """Conservative pre-normalisation WeChat conversion (raw DOM semantics).

    Mirrors the historical intake pipeline: media/caption handling and
    ``markdownify`` exactly as before, but without emphasis normalisation.
    """
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("#js_content")
    if not body:
        raise BackfillError("raw snapshot has no #js_content")
    videos, _captions = prepare_wechat_media(body, url)
    for tag in body.select("script,style,iframe"):
        tag.decompose()
    for img in body.select("img"):
        src = img.get("data-src") or img.get("src")
        if src:
            img["src"] = src
        img.attrs.pop("data-src", None)
        for attr in ("data-type", "data-fail", "data-ratio", "data-w", "data-copy"):
            img.attrs.pop(attr, None)
    content = md(str(body), heading_style="ATX")
    content, _stats = finalize_media_markdown(content, videos)
    return re.sub(r"\n{3,}", "\n\n", content).strip()


def _has_adjacent_image(node: Tag) -> bool:
    """True when the block immediately before ``node`` contains the image."""
    sibling = node.previous_sibling
    while isinstance(sibling, NavigableString) and not str(sibling).strip():
        sibling = sibling.previous_sibling
    if not isinstance(sibling, Tag):
        return False
    return (sibling.name or "").lower() == "img" or sibling.find("img") is not None


def generated_caption_marks(html: str, url: str) -> set[str]:
    """Markdown caption lines the raw DOM actually supports.

    Two authoritative sources, both operated by ``media_markup``: paragraphs
    that ``prepare_wechat_media`` marked (explicit ``figcaption`` and the
    image-adjacent small-grey heuristic), and literal ``图注：`` paragraphs
    that ``finalize_media_markdown`` promotes only next to an image.  Ordinary
    adjacent prose is never included.
    """
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("#js_content")
    if body is None:
        return set()
    prepare_wechat_media(body, url)
    marks = {
        md(str(paragraph)).strip()
        for paragraph in body.select("p[data-ca-caption]")
    }
    for paragraph in body.select("p"):
        text = re.sub(r"\s+", " ", paragraph.get_text(" ", strip=True)).strip()
        if text.startswith(CAPTION_PREFIX) and _has_adjacent_image(paragraph):
            marks.add(md(str(paragraph)).strip())
    return marks


def caption_only_patch(
    body_chunk: str, legacy_chunk: str, marks: set[str]
) -> str | None:
    """Return the annotated caption chunk, or None when evidence is missing.

    The archived copy may have lost only the generated blockquote marker
    (``> 图注：…``) or, for a CSS caption, the generated ``图注：`` prefix.
    Everything else must match the raw-derived caption line byte for byte, so
    words, links and any manual edits are never "recovered".
    """
    if not legacy_chunk.startswith("> "):
        return None
    annotated = legacy_chunk[2:]
    if annotated not in marks:
        return None
    variants = {annotated}
    if annotated.startswith(CAPTION_PREFIX):
        variants.add(annotated[len(CAPTION_PREFIX):])
    if body_chunk not in variants:
        return None
    return legacy_chunk


def read_raw_snapshot(entry_dir: Path, raw_name: str) -> tuple[bytes, str]:
    """Read a bounded, entry-confined raw snapshot; refuse traversal/symlinks."""
    name = (raw_name or "").strip()
    if not name or name in (".", ".."):
        raise BackfillError("raw_file name is empty")
    if "/" in name or "\\" in name or name.startswith("."):
        raise BackfillError(f"raw_file is not a plain entry-local name: {raw_name!r}")
    path = entry_dir / name
    if path.parent != entry_dir:
        raise BackfillError("raw_file escapes the entry directory")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise BackfillError(f"raw snapshot missing: {name}")
    if stat.S_ISLNK(info.st_mode):
        raise BackfillError(f"raw snapshot is a symlink: {name}")
    if not stat.S_ISREG(info.st_mode):
        raise BackfillError(f"raw snapshot is not a regular file: {name}")
    if name.endswith(".gz"):
        try:
            with gzip.open(path, "rb") as handle:
                data = handle.read(MAX_RAW_BYTES + 1)
        except (OSError, EOFError, zlib.error) as error:
            # A truncated stream raises EOFError, not OSError; both are
            # per-entry refusals and must never abort the whole plan.
            raise BackfillError(
                f"raw snapshot unreadable: {type(error).__name__}: {error}"
            )
    else:
        if info.st_size > MAX_RAW_BYTES:
            raise BackfillError(f"raw snapshot exceeds {MAX_RAW_BYTES} bytes")
        data = path.read_bytes()
    if len(data) > MAX_RAW_BYTES:
        raise BackfillError(f"raw snapshot exceeds {MAX_RAW_BYTES} decompressed bytes")
    return path.read_bytes(), data.decode("utf-8", "replace")


def fulltext_region(body: str) -> tuple[int, str, str] | None:
    """Return ``(content_start, content, trailing)`` for the ``## 全文`` section."""
    match = re.search(r"(?m)^##[ \t]*全文[ \t]*$", body)
    if not match:
        return None
    tail = body[match.end():]
    lead = len(tail) - len(tail.lstrip("\n"))
    core_end = len(tail.rstrip("\n"))
    return match.end() + lead, tail[lead:core_end], tail[core_end:]


def _style_weight(node) -> bool | None:
    style = node.get("style") if hasattr(node, "get") else None
    if not style:
        return None
    match = _WEIGHT_RE.search(style)
    if not match:
        return None
    value = match.group(1).strip().lower()
    if value in _WEIGHT_KEYWORDS:
        return _WEIGHT_KEYWORDS[value]
    try:
        return float(value) >= 600
    except ValueError:
        return None


def _single_bold_run(element) -> bool:
    """True when every visible text node sits inside one bold run."""
    if element.find(["a", "img", "video", "code", "pre", "table"]):
        return False
    texts = [node for node in element.find_all(string=True) if node.strip()]
    if not texts:
        return False
    for node in texts:
        parent = node.parent
        bold = False
        while parent is not None and parent is not element.parent:
            if parent.name in ("strong", "b"):
                bold = True
                break
            weight = _style_weight(parent)
            if weight is True:
                bold = True
                break
            if weight is False:
                break
            parent = parent.parent
        if not bold:
            return False
    return True


def raw_blocks(html: str) -> list[dict]:
    """Standalone raw-DOM blocks usable for reviewed heading promotion."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("#js_content") or soup.body
    if root is None:
        return []
    blocks: list[dict] = []
    for element in root.find_all(sorted(BLOCK_TAGS)):
        if element.find(sorted(BLOCK_TAGS)) is not None:
            continue
        text = re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()
        if not text:
            continue
        blocks.append({
            "text": text,
            "native_heading": element.name in HEADING_TAGS,
            "bold": _single_bold_run(element),
        })
    return blocks


def validate_selection(
    html: str, chunks: list[str], texts: list[str]
) -> tuple[list[tuple[int, str]], list[str]]:
    """Validate heading promotions; return ([(chunk index, raw text)], reasons)."""
    blocks = raw_blocks(html)
    reasons: list[str] = []
    promotions: list[tuple[int, str]] = []
    for text in texts:
        wanted = re.sub(r"\s+", " ", str(text or "")).strip()
        if not wanted:
            reasons.append("selection contains an empty heading")
            continue
        matches = [b for b in blocks if b["text"] == wanted]
        if not matches:
            reasons.append(f"selection not found as a standalone raw block: {wanted!r}")
            continue
        if len(matches) > 1:
            reasons.append(f"selection matches repeated raw blocks: {wanted!r}")
            continue
        block = matches[0]
        if not (block["native_heading"] or block["bold"]):
            reasons.append(f"selection is ordinary prose, not a bold/heading block: {wanted!r}")
            continue
        candidates = [
            i for i, chunk in enumerate(chunks)
            if heading_split(chunk)[1] == wanted or strip_emphasis(heading_split(chunk)[1]) == wanted
        ]
        if len(candidates) > 1:
            reasons.append(f"selection matches repeated Markdown blocks: {wanted!r}")
            continue
        if not candidates:
            reasons.append(f"selection has no matching Markdown block: {wanted!r}")
            continue
        index = candidates[0]
        if heading_split(chunks[index])[0]:
            continue  # already a heading; idempotent no-op
        promotions.append((index, wanted))
    return promotions, reasons



def _unique_heading_block(html: str, wanted: str) -> bool:
    """True when the raw DOM has exactly one bold/native standalone block."""
    matches = [block for block in raw_blocks(html) if block["text"] == wanted]
    return len(matches) == 1 and (matches[0]["native_heading"] or matches[0]["bold"])


def already_promoted_heading(
    body_chunk: str,
    legacy_chunk: str,
    body_chunks: list[str],
    legacy_chunks: list[str],
    selected: list[str],
    html: str,
) -> bool:
    """True for a reviewed heading that a previous run already promoted.

    The chunk must be exactly ``## <heading>``, the source block must be the
    same unique bold/native block a fresh promotion would accept, and neither
    the raw conversion nor the entry may contain a second copy.  Anything else
    (an unselected or drifted heading) falls through to ``needs_review``.
    """
    level, heading = heading_split(body_chunk)
    if level != 2 or body_chunk != "## " + heading:
        return False
    if heading not in selected:
        return False
    legacy_level, legacy_heading = heading_split(legacy_chunk)
    if legacy_level or strip_emphasis(legacy_heading) != heading:
        return False
    if sum(
        1 for chunk in legacy_chunks if strip_emphasis(heading_split(chunk)[1]) == heading
    ) != 1:
        return False
    if sum(1 for chunk in body_chunks if heading_split(chunk)[1] == heading) != 1:
        return False
    return _unique_heading_block(html, heading)


# ---------------------------------------------------------------------------
# Entry planning
# ---------------------------------------------------------------------------


def _is_wechat_entry(fm: dict) -> bool:
    if (fm.get("extraction") or "").strip() == "wechat":
        return True
    if (fm.get("source_type") or "").strip() == "wechat":
        return True
    return "mp.weixin.qq.com" in str(fm.get("url") or "")


def _row(path: str) -> dict:
    return {
        "path": path,
        "status": "unsupported",
        "reasons": [],
        "source_entry_sha256": None,
        "raw_sha256": None,
        "candidate_sha256": None,
        "content_hash_before": None,
        "content_hash_after": None,
        "chunks_changed": 0,
        "diff": "",
        "selections": [],
    }


def analyse_entry(
    source_root: Path, entry_path: Path, selections: dict | None = None
) -> tuple[dict, str | None]:
    """Compute one deterministic plan row plus its candidate text (never writes)."""
    rel = entry_path.relative_to(source_root).as_posix()
    row = _row(rel)
    try:
        if entry_path.is_symlink():
            raise UnsupportedSource("entry.md is a symlink")
        try:
            raw_entry = entry_path.read_bytes()
        except OSError as error:
            # A single unreadable entry must not abort the whole plan, and an
            # unread file has no hash we can honestly report.
            raise UnsupportedSource(
                f"entry.md unreadable: {type(error).__name__}: {error}"
            )
        row["source_entry_sha256"] = sha256_bytes(raw_entry)
        if b"\r" in raw_entry:
            # Reject CRLF before decoding: universal-newline reads would hide
            # it and silently rewrite the file's line endings.
            raise UnsupportedSource("entry.md uses CRLF line endings")
        try:
            text = raw_entry.decode("utf-8")
        except UnicodeDecodeError as error:
            raise UnsupportedSource(f"entry.md is not valid UTF-8: {error}")
        fm, body = split_frontmatter(text)
        if not fm:
            raise UnsupportedSource("entry.md has no frontmatter")
        if not _is_wechat_entry(fm):
            raise UnsupportedSource("not a WeChat markdown entry")
        if (fm.get("status") or "").strip() == "partial":
            raise UnsupportedSource("entry is marked partial; snapshot is incomplete")
        if not (fm.get("content_hash") or "").strip():
            raise UnsupportedSource("entry has no content_hash to recompute")
        url = str(fm.get("url") or fm.get("canonical_url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise UnsupportedSource("entry has no usable source URL")
        region = fulltext_region(body)
        if region is None:
            raise UnsupportedSource("entry has no '## 全文' section")
        content_start, content, _trailing = region

        raw_name = str(fm.get("raw_file") or "").strip()
        if not raw_name:
            raise UnsupportedSource("entry has no raw_file snapshot")
        try:
            raw_bytes, html = read_raw_snapshot(entry_path.parent, raw_name)
        except BackfillError:
            raise
        except OSError as error:  # corrupt gzip, permission, I/O
            raise UnsupportedSource(f"raw snapshot unreadable: {type(error).__name__}: {error}")
        row["raw_sha256"] = sha256_bytes(raw_bytes)
        entry_dir_symlinks = [
            str(p.relative_to(entry_path.parent)) for p in entry_path.parent.rglob("*") if p.is_symlink()
        ]
        if entry_dir_symlinks:
            raise NeedsReview(f"entry directory contains symlinks: {entry_dir_symlinks[:3]}")

        try:
            legacy = legacy_wechat_markdown(html, url)
            _meta, current, _raw = archive_url.wechat_extract(html, url)
        except BackfillError:
            raise
        except Exception as error:  # noqa: BLE001 - a dependency failure is per-entry
            raise NeedsReview(f"extraction failed: {type(error).__name__}: {error}")
        legacy_chunks = chunk_texts(legacy)
        current_chunks = chunk_texts(current)
        body_chunks = chunk_texts(content)
        if not legacy_chunks:
            raise UnsupportedSource("raw snapshot produced no text")
        if len(legacy_chunks) != len(current_chunks):
            raise NeedsReview(
                f"extraction structure changed ({len(legacy_chunks)} legacy vs "
                f"{len(current_chunks)} current chunks)"
            )
        if not body_chunks:
            raise UnsupportedSource("entry full text is empty")

        edits_by_chunk: dict[int, list[tuple[int, int, str]]] = {}
        for index, (old_chunk, new_chunk) in enumerate(zip(legacy_chunks, current_chunks)):
            edits = marker_edits(old_chunk, new_chunk)
            if edits is None:
                raise NeedsReview(f"current extraction changes non-format content in chunk {index + 1}")
            if heading_split(old_chunk)[0] != heading_split(new_chunk)[0]:
                raise NeedsReview(f"current extraction changes heading level in chunk {index + 1}")
            if edits:
                edits_by_chunk[index] = edits

        if len(body_chunks) < len(legacy_chunks):
            raise NeedsReview(
                f"entry full text is shorter than the raw snapshot ({len(body_chunks)} vs "
                f"{len(legacy_chunks)} chunks)"
            )
        candidate_chunks: list[str] = []
        selected = list((selections or {}).get(rel, []))
        caption_marks = generated_caption_marks(html, url)
        for index in range(len(legacy_chunks)):
            body_chunk = body_chunks[index]
            body_key = image_key(body_chunk)
            if body_key == image_key(current_chunks[index]):
                candidate_chunks.append(body_chunk)
                continue
            if already_promoted_heading(
                body_chunk,
                legacy_chunks[index],
                body_chunks,
                legacy_chunks,
                selected,
                html,
            ):
                # A previous run already applied this reviewed selection.
                candidate_chunks.append(body_chunk)
                continue
            if body_key != image_key(legacy_chunks[index]):
                caption = caption_only_patch(
                    body_chunk, legacy_chunks[index], caption_marks
                )
                if caption is not None:
                    candidate_chunks.append(caption)
                    continue
                raise NeedsReview(
                    f"entry chunk {index + 1} does not match the raw snapshot "
                    "(edited narrative or media target)"
                )
            edits = edits_by_chunk.get(index, [])
            if not edits:
                candidate_chunks.append(body_chunk)
                continue
            candidate = surgical_chunk(body_chunk, current_chunks[index])
            if candidate is None or image_key(candidate) != image_key(current_chunks[index]):
                raise NeedsReview(f"formatting patch for chunk {index + 1} could not be verified")
            candidate_chunks.append(candidate)

        appendix = body_chunks[len(legacy_chunks):]
        if appendix:
            row["reasons"].append(f"preserved {len(appendix)} appended block(s)")

        # Reviewed heading promotions apply on top of the formatting patch.
        if selected:
            promotions, refusals = validate_selection(html, candidate_chunks, selected)
            if refusals:
                raise NeedsReview("; ".join(refusals))
            for index, heading in promotions:
                candidate_chunks[index] = "## " + heading
            if promotions:
                row["selections"] = list(selected)


        ranges = split_chunks(content)
        new_content = content
        for index in range(len(candidate_chunks) - 1, -1, -1):
            start, end = ranges[index]
            new_content = new_content[:start] + candidate_chunks[index] + new_content[end:]
        new_body = body[:content_start] + new_content + body[content_start + len(content):]

        new_hash = compute_content_hash(new_body)
        row["content_hash_before"] = (fm.get("content_hash") or "").strip()
        row["content_hash_after"] = new_hash

        prefix = text[: len(text) - len(body)]
        new_text = _replace_content_hash(prefix + new_body, new_hash)

        row["candidate_sha256"] = sha256_text(new_text)
        if new_text == text:
            row["status"] = "unchanged"
            row["reasons"].insert(0, "formatting already current")
            return row, new_text

        row["status"] = "changed"
        row["chunks_changed"] = sum(
            1 for i, chunk in enumerate(candidate_chunks) if chunk != body_chunks[i]
        )
        if not row["chunks_changed"]:
            raise NeedsReview(
                "stored content_hash disagrees with the entry body and no formatting change applies"
            )
        row["diff"] = _unified_diff(text, new_text, rel)
        return row, new_text
    except NeedsReview as error:
        row["status"] = "needs_review"
        row["reasons"] = [str(error)]
        row["candidate_sha256"] = None
        row["diff"] = ""
        return row, None
    except BackfillError as error:
        row["status"] = "unsupported"
        row["reasons"] = [str(error)]
        row["candidate_sha256"] = None
        row["diff"] = ""
        return row, None


def plan_entry(source_root: Path, entry_path: Path, selections: dict | None = None) -> dict:
    """Return only the plan row (used by callers that do not need the text)."""
    return analyse_entry(source_root, entry_path, selections)[0]



def _replace_content_hash(text: str, new_hash: str) -> str:
    """Replace only the frontmatter ``content_hash`` value; keep every other byte."""
    lines = text.splitlines(keepends=True)
    closed = 0
    for index, line in enumerate(lines):
        if index == 0 and line.strip() == "---":
            continue
        if line.strip() == "---":
            closed = index
            break
    if not closed:
        raise BackfillError("entry frontmatter is not terminated")
    for index in range(1, closed):
        if lines[index].startswith("content_hash:"):
            newline = "\n" if lines[index].endswith("\n") else ""
            lines[index] = f"content_hash: {new_hash}{newline}"
            return "".join(lines)
    raise BackfillError("entry frontmatter has no content_hash line")


def _unified_diff(old: str, new: str, rel: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=rel,
            tofile=f"{rel} (candidate)",
            lineterm="",
        )
    )


def _entry_files(source_root: Path) -> list[Path]:
    library = source_root / "library"
    if not library.is_dir() or library.is_symlink():
        raise BackfillError(f"source has no usable library directory: {library}")
    found: list[Path] = []
    for base, dirs, files in os.walk(library, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not Path(base, d).is_symlink())
        if "entry.md" in files:
            path = Path(base) / "entry.md"
            if not path.is_symlink():
                found.append(path)
    return sorted(found)


def plan_digest(plan: dict) -> str:
    payload = {key: value for key, value in plan.items() if key != "plan_digest"}
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text(blob)


def build_plan(source_root: Path, selections: dict | None = None) -> dict:
    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise BackfillError(f"source archive root is not a directory: {source_root}")
    entries = _entry_files(source_root)
    if not entries:
        raise BackfillError("source archive has no library/**/entry.md entries")
    rows = []
    for entry in entries:
        rows.append(plan_entry(source_root, entry, selections))

    known = {row["path"] for row in rows}
    unknown = sorted(set(selections or {}) - known)
    if unknown:
        raise BackfillError(f"selections reference unknown entries: {unknown[:3]}")
    counts = Counter(row["status"] for row in rows)
    plan = {
        "tool": TOOL,
        "version": PLAN_VERSION,
        "generated_at": _now(),
        "source_root": str(source_root),
        "selections": selections or {},
        "rows": rows,
        "counts": {status: counts.get(status, 0) for status in STATUSES},
    }
    if sum(plan["counts"].values()) != len(rows):
        raise BackfillError("internal count mismatch")
    plan["plan_digest"] = plan_digest(plan)
    return plan


# ---------------------------------------------------------------------------
# Destination safety and preparation
# ---------------------------------------------------------------------------


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_destination(source_root: Path, dest: Path) -> Path:
    """Refuse existing/symlinked/overlapping destinations; return resolved path."""
    source = source_root.resolve()
    raw = dest.expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    if os.path.lexists(raw):
        raise BackfillError(f"destination already exists: {raw}")
    if str(Path(os.path.realpath(raw))) != str(raw):
        raise BackfillError(f"destination path traverses a symlink: {raw}")
    resolved = raw
    if _is_within(resolved, source) or _is_within(source, resolved):
        raise BackfillError("destination overlaps the source archive")
    if _is_within(resolved, SCRIPT_ROOT):
        raise BackfillError("destination must not be inside the public checkout")
    if resolved.parent == resolved:
        raise BackfillError("destination must not be a filesystem root")
    return resolved


def validate_plan_file(source_root: Path, plan_path: Path) -> Path:
    plan_path = plan_path.expanduser()
    if not plan_path.is_absolute():
        plan_path = Path.cwd() / plan_path
    if plan_path.is_symlink():
        raise BackfillError(f"plan path is a symlink: {plan_path}")
    if plan_path.exists() and not plan_path.is_file():
        raise BackfillError(f"plan path is not a regular file: {plan_path}")
    resolved = plan_path.resolve()
    if _is_within(resolved, source_root.resolve()):
        raise BackfillError("plan output must not be inside the source archive")
    if _is_within(resolved, SCRIPT_ROOT):
        raise BackfillError("plan output must not be inside the public checkout")
    return resolved



def copy_entry_tree(entry_dir: Path, target_dir: Path) -> list[str]:
    """Copy regular files only, byte for byte; refuse symlinks and specials."""
    copied: list[str] = []
    for base, dirs, files in os.walk(entry_dir, followlinks=False):
        rel_base = Path(base).relative_to(entry_dir)
        for name in sorted(dirs):
            if Path(base, name).is_symlink():
                raise BackfillError(f"entry contains a symlinked directory: {rel_base / name}")
        dirs.sort()
        target_base = target_dir / rel_base
        target_base.mkdir(parents=True, exist_ok=True)
        for name in sorted(files):
            source_file = Path(base, name)
            if name == "entry.md" and rel_base == Path("."):
                continue
            if source_file.is_symlink():
                raise BackfillError(f"entry contains a symlinked file: {rel_base / name}")
            if not source_file.is_file():
                raise BackfillError(f"entry contains a special file: {rel_base / name}")
            target_file = target_base / name
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_file)
            copied.append((rel_base / name).as_posix())
    return copied


def prepare(source_root: Path, plan_path: Path, dest: Path) -> dict:
    """Recompute and write a new candidate tree; never touch the source."""
    source = source_root.resolve()
    plan_file = validate_plan_file(source, plan_path)
    try:
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BackfillError(f"plan is not readable JSON: {error}")
    if not isinstance(plan, dict) or plan.get("tool") != TOOL:
        raise BackfillError("not a format_backfill plan")
    if plan.get("version") != PLAN_VERSION:
        raise BackfillError(f"unsupported plan version: {plan.get('version')!r}")
    if plan.get("plan_digest") != plan_digest(plan):
        raise BackfillError("plan digest mismatch: plan was altered after review")
    if plan.get("source_root") != str(source):
        raise BackfillError(
            f"plan was built for a different source root: {plan.get('source_root')!r}"
        )
    rows = plan.get("rows")
    if not isinstance(rows, list):
        raise BackfillError("plan has no rows")
    counted = Counter(row.get("status") for row in rows)
    declared = plan.get("counts") or {}
    if {status: counted.get(status, 0) for status in STATUSES} != declared:
        raise BackfillError("plan counts do not match its rows")

    selections = plan.get("selections") or {}
    recomputed: list[tuple[dict, str]] = []
    stale: list[str] = []
    for row in rows:
        rel = row.get("path")
        entry = source / rel if isinstance(rel, str) else None
        if entry is None or not _is_within(entry.resolve(), source):
            raise BackfillError(f"plan row escapes the source root: {rel!r}")
        if row.get("status") != "changed":
            continue
        fresh, candidate = analyse_entry(source, entry, selections)
        if (
            fresh["status"] != "changed"
            or candidate is None
            or fresh["source_entry_sha256"] != row.get("source_entry_sha256")
            or fresh["raw_sha256"] != row.get("raw_sha256")
            or fresh["candidate_sha256"] != row.get("candidate_sha256")
            or fresh["content_hash_after"] != row.get("content_hash_after")
            or sha256_text(candidate) != row.get("candidate_sha256")
        ):
            stale.append(rel)
            continue
        recomputed.append((row, candidate))
    if stale:
        raise BackfillError(f"plan is stale for {len(stale)} entrie(s): {stale[:3]}")

    target = validate_destination(source, dest)
    target.mkdir(parents=True)
    written = []
    for row, candidate_text in recomputed:
        entry = source / row["path"]
        entry_dir = entry.parent
        relative = Path(row["path"]).relative_to("library")
        if relative.is_absolute() or ".." in relative.parts:
            raise BackfillError(f"plan row path escapes the library tree: {row['path']!r}")
        target_dir = target / "library" / relative.parent
        copied = copy_entry_tree(entry_dir, target_dir)
        target_entry = target_dir / "entry.md"
        target_entry.write_text(candidate_text, encoding="utf-8")

        written.append({
            "path": row["path"],
            "entry_sha256": row["candidate_sha256"],
            "source_entry_sha256": row["source_entry_sha256"],
            "raw_sha256": row["raw_sha256"],
            "content_hash_before": row["content_hash_before"],
            "content_hash_after": row["content_hash_after"],
            "chunks_changed": row["chunks_changed"],
            "copied_files": copied,
        })

    skipped = [
        {"path": row["path"], "status": row.get("status"), "reasons": row.get("reasons") or []}
        for row in rows
        if row.get("status") != "changed"
    ]
    receipt = {
        "tool": TOOL,
        "version": PLAN_VERSION,
        "generated_at": _now(),
        "plan_digest": plan["plan_digest"],
        "source_root": str(source),
        "destination": str(target),
        "written": written,
        "skipped": skipped,
        "requires_index_rebuild": bool(written),
        "source_modified": False,
        "network_used": False,
        "note": (
            "Candidates only: the source archive, its .state and indexes were not modified. "
            "Copy library/ back only after review and a separate index rebuild."
        ),
    }
    (target / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return receipt


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="analyse entries and write a review plan (read-only)")
    plan.add_argument("--source", required=True, help="archive root containing library/")
    plan.add_argument("--out", required=True, help="new plan JSON path (outside the source)")
    plan.add_argument("--selections", help="JSON map of relative entry.md path to heading texts")
    prepare_cmd = sub.add_parser("prepare", help="verify a plan and write a new candidate tree")
    prepare_cmd.add_argument("--source", required=True, help="same archive root the plan used")
    prepare_cmd.add_argument("--plan", required=True, help="reviewed plan JSON")
    prepare_cmd.add_argument("--dest", required=True, help="new, non-existent destination directory")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        source = Path(args.source).expanduser()
        if args.command == "plan":
            selections: dict = {}
            if args.selections:
                selections = json.loads(Path(args.selections).read_text(encoding="utf-8"))
                if not isinstance(selections, dict):
                    raise BackfillError("selections must be a JSON object")
            plan = build_plan(source, selections)
            out = validate_plan_file(source, Path(args.out))
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({
                "status": "ok",
                "command": "plan",
                "plan": str(out),
                "plan_digest": plan["plan_digest"],
                "counts": plan["counts"],
                "rows": len(plan["rows"]),
            }, ensure_ascii=False, indent=2))
            return 0
        receipt = prepare(source, Path(args.plan), Path(args.dest))
        print(json.dumps({
            "status": "ok",
            "command": "prepare",
            "destination": receipt["destination"],
            "plan_digest": receipt["plan_digest"],
            "written": len(receipt["written"]),
            "skipped": len(receipt["skipped"]),
            "requires_index_rebuild": receipt["requires_index_rebuild"],
        }, ensure_ascii=False, indent=2))
        return 0
    except BackfillError as error:
        print(json.dumps({"status": "error", "command": args.command, "error": str(error)}, ensure_ascii=False, indent=2))
        return 2
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "error", "command": args.command, "error": f"{type(error).__name__}: {error}"}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())
