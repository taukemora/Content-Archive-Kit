#!/usr/bin/env python3
"""Preserve video links and inline-SVG chart data that Markdown extractors drop.

The original article HTML is the only input; nothing here executes page
JavaScript or downloads video.  Charts are rendered as static text rows and any
optional SVG markup is sanitised: scripts, ``foreignObject``, event handlers,
remote references and remote images are removed.
"""
from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, Comment

MARKER = "<!-- media-preservation:v1 -->"
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_WATCH = "https://www.youtube.com/watch?v={video_id}"
MAX_VIDEOS = 20
MAX_CHARTS = 20
MAX_CHART_TEXT = 4000

_UNSAFE_TAGS = {
    "script",
    "foreignobject",
    "iframe",
    "object",
    "embed",
    "image",
    "animate",
    "animatemotion",
    "animatetransform",
    "set",
    "handler",
    "audio",
    "video",
    "canvas",
    "style",
}
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_MD_ESCAPE_RE = re.compile(r"([\\`!\[\]()])")
_MD_SCHEME_RE = re.compile(r"(?<=:)(//)")


def _clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def escape_markdown_text(value: str | None) -> str:
    """Render untrusted text as inert Markdown plain text.

    Page text (SVG labels, chart rows, section headings) is attacker-controlled.
    Escaping ``&``/``<``/``>`` stops live HTML, backslash-escaping ``\\``/``!``/
    ``[``/``]``/``(``/``)``/`` ``` `` stops Markdown image/link/code injection,
    and splitting ``://`` stops bare-URL autolinking.  The visible text is kept
    so the appendix stays readable.
    """
    text = _clean_text(value)
    if not text:
        return ""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = _MD_ESCAPE_RE.sub(r"\\\1", text)
    text = _MD_SCHEME_RE.sub(r"\\/\\/", text)
    return text


def _num(value: str | None) -> float | None:
    match = _NUM_RE.search(value or "")
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def extract_youtube_videos(html: str | None) -> list[dict[str, Any]]:
    """Return Google ``uni-youtube-player-*`` / ``video-id`` components.

    Only a link is recorded.  Embed markup (which would require executing the
    page's player script) is deliberately not reproduced.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for tag in soup.find_all(True):
        name = (tag.name or "").lower()
        if name.startswith("uni-youtube") or tag.has_attr("video-id"):
            candidates.append(tag)

    seen: set[str] = set()
    videos: list[dict[str, Any]] = []
    for tag in candidates:
        raw_id = (
            tag.get("video-id")
            or tag.get("data-video-id")
            or tag.get("videoid")
            or ""
        )
        video_id = _clean_text(str(raw_id))
        if not YOUTUBE_ID_RE.match(video_id) or video_id in seen:
            continue
        seen.add(video_id)
        videos.append(
            {
                "video_id": video_id,
                "url": YOUTUBE_WATCH.format(video_id=video_id),
                "component": (tag.name or "").lower(),
                "page_title": _clean_text(
                    tag.get("page-title") or tag.get("title") or tag.get("aria-label")
                ),
                "thumbnail_alt": _clean_text(tag.get("thumbnail-alt")),
            }
        )
        if len(videos) >= MAX_VIDEOS:
            break
    return videos


def _svg_rows(svg) -> list[str]:
    items: list[tuple[float, float, str]] = []
    for node in svg.find_all("text"):
        text = _clean_text(node.get_text(" ", strip=True))
        if not text:
            continue
        x = _num(node.get("x"))
        y = _num(node.get("y"))
        if x is None or y is None:
            continue
        items.append((y, x, text))

    if not items:
        return []

    items.sort(key=lambda item: (item[0], item[1]))
    groups: list[list[tuple[float, float, str]]] = []
    for item in items:
        if not groups or abs(item[0] - groups[-1][0][0]) > 3.0:
            groups.append([item])
        else:
            groups[-1].append(item)

    rows: list[str] = []
    for group in groups:
        group.sort(key=lambda item: item[1])
        labels: list[str] = []
        for _, _, text in group:
            if not labels or labels[-1] != text:
                labels.append(text)
        if labels:
            rows.append(" | ".join(labels))
    return rows


def extract_inline_svg_charts(html: str | None, min_text_nodes: int = 2) -> list[dict[str, Any]]:
    """Return ``role="img"`` SVGs that carry real chart text.

    Decorative icons (no label, no text) and the Google logo sprite are ignored.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    charts: list[dict[str, Any]] = []
    for svg in soup.find_all("svg"):
        if (svg.get("role") or "").lower() != "img":
            continue
        label = _clean_text(svg.get("aria-label") or "")
        title = svg.find("title")
        if not label and title:
            label = _clean_text(title.get_text(" ", strip=True))
        text_nodes = [n for n in svg.find_all("text") if _clean_text(n.get_text(" ", strip=True))]
        if len(text_nodes) < min_text_nodes:
            continue
        if not label and len(text_nodes) < 4:
            continue
        rows = _svg_rows(svg)
        if not rows:
            continue
        previous = svg.find_previous(["h2", "h3"])
        context = _clean_text(previous.get_text(" ", strip=True)) if previous else ""
        charts.append(
            {
                "aria_label": label,
                "context": context,
                "rows": rows[:200],
                "text": "\n".join(rows)[:MAX_CHART_TEXT],
                "markup": sanitize_svg(str(svg)),
            }
        )
        if len(charts) >= MAX_CHARTS:
            break
    return charts


def sanitize_svg(svg_markup: str) -> str:
    """Return static SVG with executable and remote pieces removed.

    Removed: script/foreignObject/iframe/object/embed/image/animation/style
    elements, every ``on*`` handler, non-local ``href``/``xlink:href``,
    ``src``/``srcset`` and CSS ``url(...)``/``expression`` rules.  Local
    fragment references (``#id``) are kept so in-document shapes still render.
    """
    if not svg_markup:
        return ""
    soup = BeautifulSoup(svg_markup, "html.parser")

    for tag in soup.find_all(_UNSAFE_TAGS):
        tag.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            lower = attr.lower()
            value = tag.attrs.get(attr)
            text = " ".join(value) if isinstance(value, list) else str(value or "")
            if lower.startswith("on"):
                del tag.attrs[attr]
                continue
            if lower.startswith("xmlns"):
                continue
            if lower in ("href", "xlink:href"):
                if not text.strip().startswith("#"):
                    del tag.attrs[attr]
                continue
            if lower in ("src", "srcset", "poster", "action"):
                del tag.attrs[attr]
                continue
            lowered = text.lower()
            if "javascript:" in lowered or "http://" in lowered or "https://" in lowered:
                del tag.attrs[attr]
                continue
            if "url(" in lowered:
                targets = re.findall(r"url\(\s*['\"]?([^'\")]+)", text, re.I)
                if any(not target.strip().startswith("#") for target in targets):
                    del tag.attrs[attr]
                    continue
            if lower == "style" and re.search(r"expression\s*\(", text, re.I):
                del tag.attrs[attr]
                continue
        if (tag.name or "").lower() == "use" and not (tag.get("href") or tag.get("xlink:href")):
            tag.decompose()

    return str(soup).strip()


def build_media_appendix(
    html: str | None,
    content: str = "",
) -> tuple[str, dict[str, Any]]:
    """Build a Markdown appendix for videos/charts not already in ``content``."""
    report: dict[str, Any] = {
        "appended": False,
        "youtube_videos": [],
        "video_already_present": False,
        "chart_count": 0,
        "charts": [],
    }
    if not html:
        return "", report
    if MARKER in (content or "") or "## 媒体保留" in (content or ""):
        report["reason"] = "already_present"
        return "", report

    videos = extract_youtube_videos(html)
    new_videos = [
        video
        for video in videos
        if video["video_id"] not in (content or "") and video["url"] not in (content or "")
    ]
    report["video_already_present"] = bool(videos) and not new_videos
    report["youtube_videos"] = videos

    charts = extract_inline_svg_charts(html)
    report["chart_count"] = len(charts)
    report["charts"] = [
        {"aria_label": chart["aria_label"], "context": chart["context"], "rows": len(chart["rows"])}
        for chart in charts
    ]

    if not new_videos and not charts:
        return "", report

    lines = [
        MARKER,
        "## 媒体保留",
        "",
        "> 以下内容由归档脚本从原始 HTML 静态提取；未执行页面脚本、未下载视频。",
        "",
    ]
    if new_videos:
        lines += ["### 视频", ""]
        for video in new_videos:
            note = (
                f"（原页面组件：{escape_markdown_text(video['component'])}）"
                if video.get("component")
                else ""
            )
            lines.append(f"- YouTube 视频：{video['url']} {note}".rstrip())
        lines.append("")
    if charts:
        lines += ["### 图表（内联 SVG 静态文本）", ""]
        for index, chart in enumerate(charts, 1):
            title = escape_markdown_text(
                chart["aria_label"] or chart["context"] or f"图 {index}"
            )
            lines.append(f"#### {index}. {title}")
            lines.append("")
            context = escape_markdown_text(chart["context"])
            if context and context != title:
                lines.append(f"所在章节：{context}")
                lines.append("")
            lines.append("图内文字：")
            lines.append("")
            lines.extend(f"- {escape_markdown_text(row)}" for row in chart["rows"])
            lines.append("")

    section = "\n".join(lines).rstrip() + "\n"
    report["appended"] = True
    return section, report


def preserve_media(
    content: str,
    html: str | None,
    url: str = "",
) -> tuple[str, dict[str, Any]]:
    """Append a media appendix to extracted ``content``; idempotent.

    Returns ``(new_content, report)``.  When there is nothing to preserve or
    the appendix already exists, ``content`` is returned unchanged.
    """
    section, report = build_media_appendix(html, content)
    if not section:
        return content, report
    base = (content or "").rstrip()
    merged = (base + "\n\n" + section).lstrip("\n") if base else section
    return merged, report
