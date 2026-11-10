#!/usr/bin/env python3
"""Remove duplicate H1 headings from entry bodies, keeping only the first title H1."""
from __future__ import annotations

import argparse
import json

from common import ROOT, atx_h1_lines, compute_content_hash, read_entry, write_entry


def cleanup_entry_h1(body: str) -> tuple[str, list[str]]:
    """Remove subsequent H1 headings outside fenced code blocks."""
    headings = atx_h1_lines(body)
    if len(headings) <= 1:
        return body, []

    lines = body.splitlines()
    remove = {index for index, _ in headings[1:]}
    removed = [lines[index] for index in sorted(remove)]
    kept = [line for index, line in enumerate(lines) if index not in remove]
    cleaned = "\n".join(kept)
    if body.endswith("\n"):
        cleaned += "\n"
    return cleaned, removed


def main() -> int:
    ap = argparse.ArgumentParser(description="Clean up duplicate H1 headings in library entries")
    ap.add_argument("--dry-run", action="store_true", help="Report duplicates without modifying files")
    args = ap.parse_args()

    changed = []
    for p in sorted((ROOT / "library").rglob("entry.md")):
        fm, body = read_entry(p)
        cleaned_body, removed = cleanup_entry_h1(body)
        if removed:
            rel = str(p.relative_to(ROOT))
            changed.append({
                "path": rel,
                "removed_h1": removed,
            })
            if not args.dry_run:
                fm["content_hash"] = compute_content_hash(cleaned_body)
                write_entry(p, fm, cleaned_body)

    print(json.dumps({"changed": len(changed), "items": changed}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
