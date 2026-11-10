#!/usr/bin/env python3
"""Validate Chinese summary length using the project counting rule."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from common import SHORT_MAX, SHORT_MIN, SUMMARY_MAX, SUMMARY_MIN, count_summary_chars


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text")
    ap.add_argument("--file")
    ap.add_argument("--min", type=int)
    ap.add_argument("--max", type=int)
    ap.add_argument("--short", action="store_true", help="use short-article range")
    args = ap.parse_args()

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.text:
        text = args.text
    else:
        text = sys.stdin.read()

    count = count_summary_chars(text)
    lo = args.min if args.min is not None else (SHORT_MIN if args.short else SUMMARY_MIN)
    hi = args.max if args.max is not None else (SHORT_MAX if args.short else SUMMARY_MAX)
    ok = lo <= count <= hi
    print(f"{count} chars (target {lo}-{hi}) {'OK' if ok else 'OUT_OF_RANGE'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
