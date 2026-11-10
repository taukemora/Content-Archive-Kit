#!/usr/bin/env python3
"""Search the archive index."""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

from common import ROOT, read_entry

TAG_SPLIT_RE = re.compile(r"[,，、;；]")


def extract_snippet(text: str, query: str, window: int = 60) -> str:
    """Extract snippet of text around the first match of query, roughly ±window chars."""
    idx = text.lower().find(query.lower())
    if idx == -1:
        return ""
    start = max(0, idx - window)
    end = min(len(text), idx + len(query) + window)
    snippet = text[start:end].replace("\n", " ").strip()
    return snippet


def split_tags(value: str) -> set[str]:
    """Split a tag string (``AI, 大模型``) into a lowercase exact-match set."""
    return {t.strip().lower() for t in TAG_SPLIT_RE.split(value or "") if t.strip()}


def row_tags(row: dict) -> set[str]:
    return split_tags(row.get("tags", ""))


def main() -> int:
    ap = argparse.ArgumentParser(description="Search the content archive")
    ap.add_argument("query", nargs="?", default="")
    ap.add_argument("--category", default="")
    ap.add_argument("--subcategory", default="")
    ap.add_argument("--context", default="")
    ap.add_argument("--status", default="")
    ap.add_argument("--collection", choices=("manual", "auto"), default="",
                    help="collection provenance: manual or auto")
    ap.add_argument("--tag", action="append", default=None,
                    help="Exact tag match; repeatable and/or comma separated")
    ap.add_argument("--source", default="")
    ap.add_argument("--from", dest="date_from", default="")
    ap.add_argument("--to", dest="date_to", default="")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--full", action="store_true", help="Search full text in entry.md")
    args = ap.parse_args()

    wanted_tags: set[str] = set()
    for value in args.tag or []:
        wanted_tags |= split_tags(value)

    path = ROOT / "index.csv"
    if not path.exists():
        print("index.csv not found; run bin/ca index first")
        return 1
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    if args.collection and "collection" not in fieldnames:
        print("index.csv has no collection column; run bin/ca index to rebuild it")
        return 1

    q = args.query.lower().strip()
    out = []
    for r in rows:
        if args.category and r.get("category") != args.category:
            continue
        if args.subcategory and r.get("subcategory") != args.subcategory:
            continue
        if args.context and r.get("context") != args.context:
            continue
        if args.status and (r.get("status") or "") != args.status:
            continue
        if args.collection and (r.get("collection") or "").strip() != args.collection:
            continue
        if args.source and args.source.lower() not in (r.get("source", "")).lower():
            continue
        # Exact tag matching: every requested tag must be one of the row's tags.
        # Substring matching would make --tag AI hit "OpenAI" / "AI写作" / "AAIF".
        if wanted_tags and not wanted_tags.issubset(row_tags(r)):
            continue
        d = (r.get("published") or "")[:10]
        if args.date_from and d < args.date_from:
            continue
        if args.date_to and d > args.date_to:
            continue

        hay = " ".join([r.get("title", ""), r.get("original_title", ""), r.get("tags", ""), r.get("summary", "")]).lower()

        if not q:
            out.append(r)
            continue

        if args.full:
            # Search entry full text
            entry_path = ROOT / r.get("path", "")
            matched = False
            snippet = ""
            if entry_path.exists():
                try:
                    _, body = read_entry(entry_path)
                    if q in body.lower():
                        matched = True
                        snippet = extract_snippet(body, q, 60)
                except Exception:
                    pass
            if not matched and q in hay:
                matched = True
                snippet = extract_snippet(hay, q, 60)

            if matched:
                r_copy = dict(r)
                r_copy["snippet"] = snippet
                out.append(r_copy)
        else:
            if q in hay:
                out.append(r)

    out = out[: args.limit]
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if not out:
        print("no matches")
        return 0
    for r in out:
        print(f"{r.get('published','')[:10]}  [{r.get('category')}/{r.get('context')}]  {r.get('title')}")
        print(f"  {r.get('source')} · {r.get('summary_chars')} 字 · {r.get('url')}")
        if args.full and r.get("snippet"):
            print(f"  匹配片段: ...{r.get('snippet')}...")
        print(f"  {r.get('path')}")
    print(f"\n{len(out)} result(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
