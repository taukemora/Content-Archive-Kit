#!/usr/bin/env python3
"""Rebuild index.csv and index.md from library entries."""
from __future__ import annotations

import csv
import json
import os
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from common import ROOT, category_slugs, read_entry, summary_section, taxonomy
from build_html import build_html

FIELDS = [
    "id", "published", "title", "original_title", "source", "source_type",
    "category", "subcategory", "context", "tags", "url", "summary_chars",
    "images", "status", "content_note", "relevance", "path", "archived", "language", "collection",
]

VENDOR_ORDER = ["OpenAI", "Anthropic", "Kimi", "GLM", "微信公众号", "其他"]


def _atomic_write(path: Path, render, encoding: str, newline: str | None = None) -> None:
    """Write via a same-directory temp file + ``os.replace``.

    Concurrent readers (search / digest / web UI) then see either the previous
    complete file or the new complete file, never a half-written one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            render(f)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def write_index_csv(path: Path, rows: list[dict]) -> None:
    def render(f) -> None:
        w = csv.DictWriter(f, fieldnames=FIELDS + ["summary"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS + ["summary"]})

    _atomic_write(path, render, encoding="utf-8-sig", newline="")


def write_index_md(path: Path, text: str) -> None:
    _atomic_write(path, lambda f: f.write(text), encoding="utf-8")


def archived_str(value) -> str:
    """Keep the full ISO timestamp, timezone offset included."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value).strip()


def vendor_name(r: dict) -> str:
    s = (r.get("source") or "").lower()
    st = (r.get("source_type") or "").lower()
    url = (r.get("url") or "").lower()
    if st == "wechat" or "mp.weixin.qq.com" in url or "微信" in s:
        return "微信公众号"
    if "openai" in s or "openai.com" in url:
        return "OpenAI"
    if "anthropic" in s or "anthropic.com" in url:
        return "Anthropic"
    if "kimi" in s or "moonshot" in s or "kimi.com" in url:
        return "Kimi"
    if "glm" in s or "智谱" in s or "bigmodel.cn" in url:
        return "GLM"
    return "其他"


def format_entry_line(r: dict) -> str:
    tags = f" · {r['tags']}" if r.get("tags") else ""
    mark = " ⚠️ 部分" if r.get("status") == "partial" else ""
    collection = {"manual": "手动收藏", "auto": "自动采集"}.get(r.get("collection"), "未标记")
    return f"- `{r.get('published', '')}` [{r.get('title', '')}]({r.get('url', '')}) — {r.get('source', '')} · {collection}{tags}{mark}"


def main() -> int:
    tx = taxonomy()
    labels = {c["slug"]: c["label"] for c in tx.get("categories", [])}
    rows = []
    for p in sorted((ROOT / "library").rglob("entry.md")):
        fm, body = read_entry(p)
        summary = summary_section(body)
        rows.append({
            "id": fm.get("id", p.parent.name),
            "published": str(fm.get("published", ""))[:10],
            "title": fm.get("title", ""),
            "original_title": fm.get("original_title", ""),
            "source": fm.get("source", ""),
            "source_type": fm.get("source_type", ""),
            "collection": fm.get("collection", ""),
            "category": fm.get("category", ""),
            "subcategory": fm.get("subcategory") or "",
            "context": fm.get("context", ""),
            "tags": ", ".join(fm.get("tags") or []),
            "url": fm.get("url", ""),
            "summary_chars": fm.get("summary_chars", 0),
            "images": fm.get("images", 0),
            "status": fm.get("status", ""),
            "content_note": fm.get("content_note") or "",
            "relevance": fm.get("relevance") or "",
            "path": str(p.relative_to(ROOT)),
            "archived": archived_str(fm.get("archived")),
            "language": fm.get("language", ""),
            "summary": summary,
        })

    rows.sort(key=lambda r: (r["published"] or "", r["archived"] or ""), reverse=True)

    # Render first: a bad input must not replace the previous HTML.
    html = build_html(ROOT, rows, labels)
    out = ROOT / "index.csv"
    write_index_csv(out, rows)

    # Human-readable index.md with three sections: 按分类, 按厂商, 按月份
    lines = ["# 内容收藏夹索引", "", f"共 {len(rows)} 篇。", ""]

    # Section 1: 按分类
    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)

    lines.append("## 按分类")
    lines.append("")
    for c in tx.get("categories", []):
        slug = c["slug"]
        label = c.get("label", slug)
        items = by_cat.get(slug, [])
        lines.append(f"### {label}（{len(items)}）")
        lines.append("")
        for r in items:
            lines.append(format_entry_line(r))
        if items:
            lines.append("")

    # Section 2: 按厂商
    by_vendor = defaultdict(list)
    for r in rows:
        by_vendor[vendor_name(r)].append(r)

    lines.append("## 按厂商")
    lines.append("")
    for v in VENDOR_ORDER:
        items = by_vendor.get(v, [])
        lines.append(f"### {v}（{len(items)}）")
        lines.append("")
        for r in items:
            lines.append(format_entry_line(r))
        if items:
            lines.append("")

    # Extra vendors not in predefined list
    for v in sorted(by_vendor.keys()):
        if v not in VENDOR_ORDER:
            items = by_vendor[v]
            lines.append(f"### {v}（{len(items)}）")
            lines.append("")
            for r in items:
                lines.append(format_entry_line(r))
            if items:
                lines.append("")

    # Section 3: 按月份
    by_month = defaultdict(list)
    for r in rows:
        m = (r.get("published") or "")[:7] or "未知月份"
        by_month[m].append(r)

    sorted_months = sorted(by_month.keys(), reverse=True)
    lines.append("## 按月份")
    lines.append("")
    for m in sorted_months:
        items = by_month[m]
        lines.append(f"### {m}（{len(items)}）")
        lines.append("")
        for r in items:
            lines.append(format_entry_line(r))
        if items:
            lines.append("")

    write_index_md(ROOT / "index.md", "\n".join(lines).rstrip() + "\n")

    _atomic_write(ROOT / "index.html", lambda f: f.write(html), encoding="utf-8")

    # Publication is a separate outcome, never a rollback of the local index.
    # Keep raw exception messages out of output: a transport may include secrets.
    try:
        from publish_site import maybe_publish
        publication = maybe_publish(ROOT)
    except Exception:
        publication = {"status": "failed", "reason": "publisher_unavailable"}

    stats = Counter(r["category"] for r in rows)
    print(json.dumps({
        "status": "ok",
        "entries": len(rows),
        "by_category": dict(stats),
        "by_vendor": {v: len(by_vendor.get(v, [])) for v in VENDOR_ORDER},
        "by_month": {m: len(by_month[m]) for m in sorted_months},
        "index_csv": str(out),
        "index_md": str(ROOT / "index.md"),
        "index_html": str(ROOT / "index.html"),
        "by_collection": dict(Counter(r["collection"] for r in rows)),
        "publication": publication,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
