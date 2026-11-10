#!/usr/bin/env python3
"""Reconcile seen.json with library/ entries and manage snapshots."""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from common import (
    ROOT,
    TZ,
    dedupe_key,
    iso,
    load_seen,
    now,
    read_entry,
    save_seen,
    state_lock,
)


def snapshot_seen(root: Path = ROOT, retain_days: int = 14) -> dict:
    seen_file = root / ".state" / "seen.json"
    if not seen_file.exists():
        return {"status": "error", "error": f"{seen_file} does not exist"}

    backups_dir = root / ".state" / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)

    timestamp = now().strftime("%Y%m%d-%H%M%S-%f")
    snapshot_path = backups_dir / f"seen-{timestamp}.json.gz"

    content = seen_file.read_bytes()
    with gzip.open(snapshot_path, "wb") as f:
        f.write(content)

    cutoff = now() - timedelta(days=retain_days)
    pruned = []
    for p in backups_dir.glob("seen-*.json.gz"):
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=TZ)
            if mtime < cutoff:
                p.unlink()
                pruned.append(p.name)
        except Exception:
            pass

    return {
        "status": "ok",
        "snapshot": str(snapshot_path.relative_to(root)),
        "pruned": pruned,
    }


def reconcile_seen(
    root: Path = ROOT,
    dry_run: bool = False,
    prune_orphans: bool = False,
    quiet: bool = False,
) -> dict:
    lib_dir = root / "library"
    seen_file = root / ".state" / "seen.json"
    try:
        seen = load_seen(seen_file)
    except Exception as e:
        if not quiet:
            print(f"Error loading seen.json: {e}", file=sys.stderr)
        return {"status": "error", "error": str(e)}

    entries_map = seen.setdefault("entries", {})

    added = []
    updated = []
    orphans = []
    key_paths: dict[str, list[str]] = {}

    # 1. Scan library for all entry.md
    if lib_dir.exists():
        for p in sorted(lib_dir.rglob("entry.md")):
            try:
                fm, _ = read_entry(p)
            except Exception as e:
                if not quiet:
                    print(f"Warning: failed to read {p}: {e}", file=sys.stderr)
                continue

            rel = str(p.relative_to(root))
            raw_url = fm.get("canonical_url") or fm.get("url") or ""
            if not raw_url:
                continue
            key = dedupe_key(raw_url)
            entry_id = fm.get("id") or p.parent.name
            key_paths.setdefault(key, [])
            if rel not in key_paths[key]:
                key_paths[key].append(rel)

            if key not in entries_map:
                # Check if there is an entry with matching id under another key
                matched_alt_key = None
                for ek, ev in entries_map.items():
                    if ev.get("id") == entry_id:
                        matched_alt_key = ek
                        break

                if matched_alt_key:
                    ev = entries_map.pop(matched_alt_key)
                    ev["id"] = entry_id
                    ev["path"] = rel
                    entries_map[key] = ev
                    updated.append({"id": entry_id, "key": key, "old_key": matched_alt_key, "path": rel})
                else:
                    entries_map[key] = {
                        "id": entry_id,
                        "path": rel,
                        "first_seen": fm.get("archived") or iso(),
                    }
                    added.append({"id": entry_id, "key": key, "path": rel})
            else:
                curr = entries_map[key]
                changed = False
                if curr.get("path") != rel:
                    curr["path"] = rel
                    changed = True
                if curr.get("id") != entry_id:
                    curr["id"] = entry_id
                    changed = True
                if changed:
                    updated.append({"id": entry_id, "key": key, "path": rel})

    # 2. Check for orphans in seen
    orphan_keys = []
    for k, v in list(entries_map.items()):
        p_str = v.get("path", "")
        if not p_str or not (root / p_str).exists():
            orphans.append({"key": k, "path": p_str, "id": v.get("id")})
            orphan_keys.append(k)

    if prune_orphans:
        for k in orphan_keys:
            entries_map.pop(k, None)

    modified = bool(added or updated or (prune_orphans and orphan_keys))
    if modified and not dry_run:
        with state_lock(root):
            save_seen(seen, seen_file)

    duplicates = [
        {"key": key, "paths": paths}
        for key, paths in sorted(key_paths.items())
        if len(paths) > 1
    ]

    scanned_count = len(list(lib_dir.rglob("entry.md"))) if lib_dir.exists() else 0
    return {
        "status": "ok",
        "dry_run": dry_run,
        "scanned_entries": scanned_count,
        "added": added,
        "updated": updated,
        "orphans": orphans,
        "duplicates": duplicates,
        "pruned": len(orphan_keys) if prune_orphans else 0,
        "fixed": len(added) + len(updated) + (len(orphan_keys) if prune_orphans else 0),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Reconcile seen.json with library/ entries.")
    ap.add_argument("--dry-run", action="store_true", help="Report differences without saving")
    ap.add_argument("--prune-orphans", action="store_true", help="Remove seen keys without disk files")
    ap.add_argument("--snapshot", action="store_true", help="Create a gzip backup of seen.json in .state/backups/")
    ap.add_argument("--root", default="", help="Override project root directory")
    args = ap.parse_args()

    project_root = Path(args.root).resolve() if args.root else ROOT

    if args.snapshot:
        res = snapshot_seen(project_root)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("status") == "ok" else 1

    res = reconcile_seen(project_root, dry_run=args.dry_run, prune_orphans=args.prune_orphans)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
