#!/usr/bin/env python3
"""Finalize an entry: Chinese title, summary, category, context, tags."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from common import (
    ROOT,
    SHORT_ARTICLE_CHARS,
    SHORT_MAX,
    SHORT_MIN,
    SUMMARY_MAX,
    SUMMARY_MIN,
    atx_h1_lines,
    category_slugs,
    compute_content_hash,
    context_slugs,
    count_summary_chars,
    dedupe_key,
    entry_char_count,
    find_entry,
    is_allowed_file_path,
    iso,
    load_seen,
    read_entry,
    replace_summary,
    save_seen,
    scratch_root,
    state_lock,
    summary_section,
    write_entry,
)

SCRATCH_ROOT = scratch_root()


def resolve_local_path(value: str) -> Path:
    """Resolve a CLI path relative to the project root first, then CWD."""
    p = Path(value).expanduser()
    if not p.is_absolute():
        candidate = ROOT / p
        if candidate.exists():
            p = candidate
    return p.resolve()


def ensure_entry_path(path: Path) -> Path:
    rp = path.resolve()
    allowed = [ROOT / "library", SCRATCH_ROOT]
    for root in allowed:
        try:
            rp.relative_to(root.resolve())
            return rp
        except ValueError:
            continue
    raise ValueError(f"entry path not allowed (must be under library/ or scratch): {path}")


def get_library_root(entry_path: Path) -> Path:
    try:
        parts = entry_path.resolve().parts
        if "library" in parts:
            idx = parts.index("library")
            return Path(*parts[:idx + 1])
    except Exception:
        pass
    return ROOT / "library"


def set_body_banner(body: str, banner: str | None) -> str:
    body = re.sub(r"(##\s*全文\s*\n\n)(?:>[^\n]*(?:⚠️|部分抓取)[^\n]*\n\n)?", r"\1", body, count=1)
    if banner:
        body = re.sub(r"(##\s*全文\s*\n\n)", r"\1" + banner.strip() + "\n\n", body, count=1)
    return body


def set_body_h1(body: str, title: str) -> str:
    """Replace the first H1 *outside fenced code blocks*, or prepend one."""
    headings = atx_h1_lines(body)
    lines = body.splitlines(keepends=True)
    if headings:
        index, _ = headings[0]
        original = lines[index]
        newline = "\n" if original.endswith("\n") else ""
        lines[index] = f"# {title}{newline}"
        return "".join(lines)
    return f"# {title}\n\n" + body.lstrip("\n")


def set_summary_banner(body: str, banner: str | None) -> str:
    body = re.sub(r"^> ⚠️ (?:\*\*)?摘要基于部分抓取[^\n]*\n\n", "", body, flags=re.M)
    if banner:
        body = re.sub(r"\n(##\s*摘要[^\n]*)", "\n" + banner.strip() + "\n\n\\1", body, count=1)
    return body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pos_id", nargs="?", default="", help="optional positional entry id")
    ap.add_argument("--id", default="")
    ap.add_argument("--path", default="")
    ap.add_argument("--title-zh", default="")
    ap.add_argument("--summary", default="")
    ap.add_argument("--summary-file", default="")
    ap.add_argument("--category", default="")
    ap.add_argument("--subcategory", default="")
    ap.add_argument("--context", default="")
    ap.add_argument("--tags", default="")
    ap.add_argument("--source-type", default="")
    ap.add_argument("--relevance", default=None)
    ap.add_argument("--confidence", type=float, default=None)
    ap.add_argument("--status", default="")
    ap.add_argument("--content-note", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    target_id = args.id or args.pos_id
    path = Path(args.path) if args.path else (find_entry(target_id) if target_id else None)
    if not path or not path.exists():
        print(json.dumps({"status": "error", "error": "entry not found"}, ensure_ascii=False))
        return 1

    try:
        path = ensure_entry_path(resolve_local_path(str(path)))
    except ValueError as e:
        print(json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False))
        return 1

    lib_root = get_library_root(path)
    base_root = lib_root.parent
    # Only touch .state/ when the project holding this entry actually has one;
    # a sandbox copy without .state must never read or write production seen.json.
    state_dir = base_root / ".state"
    has_state = state_dir.exists()

    if has_state:
        try:
            from reconcile_seen import reconcile_seen
            reconcile_seen(base_root, quiet=True)
        except Exception:
            pass

    fm, body = read_entry(path)
    old_category = fm.get("category", "other")
    new_category = args.category or old_category
    if new_category not in category_slugs():
        print(json.dumps({"status": "error", "error": f"unknown category: {new_category}"}, ensure_ascii=False))
        return 1

    if args.title_zh:
        fm["title"] = args.title_zh.strip()
        fm["title_status"] = "original" if fm.get("language") == "zh" else "translated"
        body = set_body_h1(body, fm["title"])
    if args.subcategory:
        sub = args.subcategory.strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", sub):
            print(json.dumps({"status": "error", "error": f"invalid subcategory slug: {sub}"}, ensure_ascii=False))
            return 1
        fm["subcategory"] = sub
    if args.context:
        if args.context not in context_slugs():
            print(json.dumps({"status": "error", "error": f"unknown context: {args.context}"}, ensure_ascii=False))
            return 1
        fm["context"] = args.context
    if args.tags:
        fm["tags"] = [t.strip() for t in re.split(r"[,，、;；]", args.tags) if t.strip()]
    if args.source_type:
        fm["source_type"] = args.source_type.strip()
    if args.relevance is not None:
        fm["relevance"] = args.relevance.strip()
    if args.confidence is not None:
        fm["confidence"] = args.confidence
    if args.model:
        fm["model"] = args.model

    summary = args.summary
    if args.summary_file:
        summary_path = resolve_local_path(args.summary_file)
        if not summary_path.exists() or not summary_path.is_file():
            print(json.dumps({"status": "error", "error": f"summary-file not found: {args.summary_file}"}, ensure_ascii=False))
            return 1
        if not is_allowed_file_path(summary_path, ROOT):
            print(json.dumps({"status": "error", "error": f"summary-file path not allowed: {args.summary_file}"}, ensure_ascii=False))
            return 1
        if summary_path.stat().st_size > 5 * 1024 * 1024:
            print(json.dumps({"status": "error", "error": f"summary-file exceeds 5MB: {args.summary_file}"}, ensure_ascii=False))
            return 1
        try:
            summary = summary_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            print(json.dumps({"status": "error", "error": f"summary-file UTF-8 decode failed: {e}"}, ensure_ascii=False))
            return 1
    summary = (summary or "").strip()
    if summary:
        n = count_summary_chars(summary)
        short = entry_char_count(fm, body) < SHORT_ARTICLE_CHARS
        lo, hi = (SHORT_MIN, SHORT_MAX) if short else (SUMMARY_MIN, SUMMARY_MAX)
        if not (lo <= n <= hi) and not args.force:
            print(json.dumps({
                "status": "error",
                "error": f"summary {n} chars out of range {lo}-{hi}",
                "chars": n,
                "short_article": short,
            }, ensure_ascii=False))
            return 1
        body = replace_summary(body, summary)
        fm["summary_chars"] = n
    else:
        cur_summary = summary_section(body)
        if cur_summary:
            fm["summary_chars"] = count_summary_chars(cur_summary)
        else:
            fm.setdefault("summary_chars", 0)

    status = args.status
    if not status:
        if fm.get("status") == "partial":
            status = "partial"
        else:
            status = "needs_review" if new_category == "other" or float(fm.get("confidence") or 1) < 0.7 else "archived"

    if status == "partial":
        content_note = args.content_note.strip() if args.content_note else (fm.get("content_note") or "").strip()
        if not content_note:
            print(json.dumps({"status": "error", "error": "--status partial requires non-empty content_note"}, ensure_ascii=False))
            return 1
        fm["content_note"] = content_note
        m = re.search(r"约\s*(\d+)\s*字符", content_note)
        if m:
            banner_text = f"> ⚠️ **部分抓取**：本文正文在约 {m.group(1)} 字符处截断，以下内容不完整。"
        elif content_note.startswith("本文"):
            banner_text = f"> ⚠️ **部分抓取**：{content_note}"
        else:
            banner_text = f"> ⚠️ **部分抓取**：本文{content_note}"
        body = set_body_banner(body, banner_text)
        body = set_summary_banner(body, "> ⚠️ **摘要基于部分抓取正文**，以下摘要可能不完整。")
    elif status == "archived":
        if args.content_note and args.content_note.strip():
            print(json.dumps({"status": "error", "error": "--status archived cannot have content_note"}, ensure_ascii=False))
            return 1
        body = set_body_banner(body, None)
        body = set_summary_banner(body, None)
        fm["content_note"] = ""
    else:
        if args.content_note:
            fm["content_note"] = args.content_note.strip()

    fm.setdefault("content_note", "")
    fm["status"] = status
    fm["category"] = new_category
    fm["updated"] = iso()
    fm["content_hash"] = compute_content_hash(body)

    target_dir = path.parent
    if new_category != old_category:
        target_dir = lib_root / new_category / path.parent.name

    try:
        target_dir.resolve().relative_to((lib_root / new_category).resolve())
    except ValueError:
        print(json.dumps({"status": "error", "error": f"target outside library category: {target_dir}"}, ensure_ascii=False))
        return 1

    if target_dir.resolve() != path.parent.resolve() and target_dir.exists():
        print(json.dumps({"status": "error", "error": f"target exists: {target_dir}"}, ensure_ascii=False))
        return 1

    try:
        old_rel_path = str(path.relative_to(base_root))
    except ValueError:
        old_rel_path = str(path)

    # 1. Write entry.md in current directory with atomic tmp + os.replace
    write_entry(path, fm, body)

    # 2. Rename directory if destination is different
    if target_dir.resolve() != path.parent.resolve():
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(str(path.parent), str(target_dir))
        except OSError as e:
            print(json.dumps({"status": "error", "error": f"failed to rename {path.parent} to {target_dir}: {e}"}, ensure_ascii=False))
            return 1
        path = target_dir / "entry.md"
        fm["id"] = target_dir.name

    # 3. Update seen path — only for the project that owns this entry.
    if has_state:
        seen_file = state_dir / "seen.json"
        try:
            rel_path = str(path.relative_to(base_root))
        except ValueError:
            rel_path = str(path)

        try:
            with state_lock(base_root):
                seen = load_seen(seen_file)
                entries = seen.setdefault("entries", {})

                matched = False
                for key, val in entries.items():
                    if (
                        val.get("id") == fm.get("id")
                        or val.get("path") in (old_rel_path, rel_path)
                        or key == fm.get("canonical_url")
                        or key == fm.get("url")
                    ):
                        val["id"] = fm.get("id")
                        val["path"] = rel_path
                        entries[key] = val
                        matched = True
                        break

                if not matched and (fm.get("canonical_url") or fm.get("url")):
                    new_key = dedupe_key(fm.get("canonical_url") or fm.get("url"))
                    entries[new_key] = {
                        "id": fm.get("id"),
                        "path": rel_path,
                        "first_seen": fm.get("archived") or iso(),
                    }

                save_seen(seen, seen_file)
        except Exception as e:
            print(json.dumps({"status": "error", "error": f"failed to update seen: {e}"}, ensure_ascii=False))
            return 1

    print(json.dumps({
        "status": "ok",
        "id": fm.get("id"),
        "path": str(path),
        "title": fm.get("title"),
        "category": fm.get("category"),
        "subcategory": fm.get("subcategory"),
        "context": fm.get("context"),
        "tags": fm.get("tags"),
        "source_type": fm.get("source_type"),
        "relevance": fm.get("relevance"),
        "summary_chars": fm.get("summary_chars"),
        "status_field": fm.get("status"),
        "content_note": fm.get("content_note"),
        "summary": summary_section(body),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
