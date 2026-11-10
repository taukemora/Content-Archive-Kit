#!/usr/bin/env python3
"""Sync each entry's first H1 with its frontmatter title."""
from __future__ import annotations

import argparse
import json

from common import ROOT, atx_h1_lines, compute_content_hash, iso, read_entry, write_entry


def sync_first_h1(body: str, title: str) -> tuple[str, str | None]:
    """Return (body with synced H1, previous H1 text or None when absent)."""
    headings = atx_h1_lines(body)
    if not headings:
        return body, None

    index, current = headings[0]
    lines = body.splitlines(keepends=True)
    original = lines[index]
    newline = "\n" if original.endswith("\n") else ""
    lines[index] = f"# {title}{newline}"
    return "".join(lines), current


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    changed = []
    for p in sorted((ROOT / "library").rglob("entry.md")):
        fm, body = read_entry(p)
        title = (fm.get("title") or "").strip()
        if not title:
            continue
        updated_body, current = sync_first_h1(body, title)
        if current is None or current == title:
            continue
        changed.append({"path": str(p.relative_to(ROOT)), "from": current, "to": title})
        if not args.dry_run:
            fm["updated"] = iso()
            fm["content_hash"] = compute_content_hash(updated_body)
            write_entry(p, fm, updated_body)
    print(json.dumps({"changed": len(changed), "items": changed}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
