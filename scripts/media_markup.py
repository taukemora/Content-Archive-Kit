#!/usr/bin/env python3
"""Portable Markdown media semantics for WeChat and generic article HTML.

The archive never stores raw article HTML.  This module turns structural media
evidence (``iframe.video_iframe`` / ``data-mpvid`` / ``<video>`` / explicit
``<figure><figcaption>`` and a narrow WeChat caption heuristic) into explicit
Markdown blockquotes, and installs the matching (safe) reader rendering.

Markdown contract::

    ![alt](images/001.png)

    > 图注：原文图注

    > **视频 1（未下载）**
    >
    > 观看：[前往原文观看](https://…)
    > 说明：…

Nothing here downloads media, executes page scripts or trusts page text.
"""
from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag
from markdown_it.token import Token

CAPTION_PREFIX = "图注："
VIDEO_HEADING_RE = re.compile(r"^\*\*视频\s+(\d+)（未下载）\*\*$")
_VIDEO_TOKEN_PREFIX = "@@CAVIDEO-"
_CAPTION_BLOCK_RE = re.compile(
    r"(?P<image>!\[[^\]]*\]\([^)\n]+\))[ \t]*\n{2,}(?P<caption>图注：[^\n]+)"
)
_CAPTION_LONG_THRESHOLD = 40
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YT_WATCH = "https://www.youtube.com/watch?v={video_id}"
_TAG_STRIP = {
    "script",
    "style",
    "iframe",
    "object",
    "embed",
    "form",
    "input",
    "button",
    "textarea",
    "select",
    "svg",
    "canvas",
    "video",
    "audio",
    "img",
    "source",
}
_SOURCE_LABEL_RE = re.compile(
    r"^\s*(图源|图片来源|来源|摄影|摄像|Credit|Photo|Source)\s*[:：]", re.I
)
_FONT_SIZE_RE = re.compile(r"font-size\s*:\s*([0-9.]+)\s*(px|pt|em|rem)", re.I)
_COLOR_RE = re.compile(
    r"(?<![-a-z])color\s*:\s*(rgba?\([^)]*\)|#[0-9a-fA-F]{3,8}|[a-zA-Z]+)", re.I
)
_CENTER_RE = re.compile(r"text-align\s*:\s*center", re.I)


def _clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _safe_http_url(value: str | None, base_url: str = "") -> str:
    """Return an absolute http(s) URL usable as a Markdown link target."""
    raw = (value or "").strip()
    if not raw or any(ch in raw for ch in "\r\n\t \"'<>") or any(ord(c) < 32 for c in raw):
        return ""
    try:
        absolute = urljoin(base_url, raw) if base_url else raw
        parsed = urlsplit(absolute)
    except Exception:
        return ""
    if parsed.scheme.lower() not in ("http", "https"):
        return ""
    if not parsed.netloc or not parsed.hostname:
        return ""
    return absolute


def _caption_fragment(node: Tag, page_url: str = "") -> str:
    """Return inert caption HTML (text + safe links) from a caption node."""
    clone = BeautifulSoup(str(node), "html.parser")
    root = clone.find() or clone
    for tag in list(root.find_all(True)):
        name = (tag.name or "").lower()
        if name in _TAG_STRIP:
            tag.decompose()
            continue
        if name == "a":
            href = _safe_http_url(tag.get("href"), page_url)
            if href:
                tag["href"] = href
            else:
                tag.unwrap()
            continue
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag.attrs[attr]
    # Caption text is page-controlled: keep it visible but inert when embedded.
    for string in list(root.find_all(string=True)):
        text = str(string)
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        string.replace_with(text)
    return root.decode_contents().strip()


def _caption_text(html: str) -> str:
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    text = _clean_text(text)
    if text.startswith(CAPTION_PREFIX):
        text = text[len(CAPTION_PREFIX):].strip()
    return text


def _new_caption_p(node: Tag, inner_html: str) -> Tag:
    soup = BeautifulSoup(f"<p data-ca-caption=\"1\">{CAPTION_PREFIX}{inner_html}</p>", "html.parser")
    paragraph = soup.find("p")
    for string in list(paragraph.find_all(string=True)):
        text = str(string)
        text = text.replace("<", "&lt;").replace(">", "&gt;")
        text = re.sub(r"([\\`!\[\]()])", r"\\\1", text)
        string.replace_with(text)
    return paragraph


def _mark_explicit_figcaptions(root: Tag, page_url: str = "") -> int:
    """Move every explicit ``<figcaption>`` next to its figure as 图注."""
    count = 0
    for caption in list(root.find_all("figcaption")):
        inner = _caption_fragment(caption, page_url)
        if not _caption_text(inner):
            caption.decompose()
            continue
        figure = caption.find_parent("figure") or caption.parent
        paragraph = _new_caption_p(root, inner)
        caption.decompose()
        if figure is not None:
            figure.insert_after(paragraph)
        count += 1
    return count


def _style_text(node: Tag) -> str:
    parts: list[str] = []
    for tag in [node, *node.find_all(True)]:
        value = tag.get("style")
        if value:
            parts.append(str(value))
    return " ; ".join(parts)


def _is_small(style: str) -> bool:
    sizes: list[float] = []
    for value, unit in _FONT_SIZE_RE.findall(style):
        try:
            size = float(value)
        except ValueError:
            continue
        if unit.lower() in ("em", "rem"):
            size *= 16
        sizes.append(size)
    return bool(sizes) and min(sizes) <= 14.0


def _is_grey(style: str) -> bool:
    for value in _COLOR_RE.findall(style):
        text = value.strip().lower()
        if text in ("gray", "grey"):
            return True
        if text.startswith("#"):
            digits = text[1:]
            if len(digits) == 3:
                digits = "".join(ch * 2 for ch in digits)
            if len(digits) >= 6:
                try:
                    r, g, b = (int(digits[i:i + 2], 16) for i in (0, 2, 4))
                except ValueError:
                    continue
                if max(r, g, b) - min(r, g, b) <= 14 and 70 <= r <= 200:
                    return True
        elif text.startswith("rgb"):
            numbers = re.findall(r"\d+(?:\.\d+)?", text)
            if len(numbers) >= 3:
                r, g, b = (float(numbers[i]) for i in range(3))
                if max(r, g, b) - min(r, g, b) <= 14 and 70 <= r <= 200:
                    return True
    return False


def _is_wechat_caption(node: Tag) -> bool:
    """Multi-signal caption test; a single centred/short sentence never wins."""
    text = _clean_text(node.get_text(" ", strip=True))
    if not text or len(text) > 140:
        return False
    if node.find(["img", "iframe", "video", "audio", "svg", "table", "ul", "ol", "blockquote", "br"]):
        return False
    if node.find(["p", "div", "section", "h1", "h2", "h3", "h4"]):
        return False
    style = _style_text(node)
    small = _is_small(style)
    grey = _is_grey(style)
    centered = bool(_CENTER_RE.search(style))
    source = bool(_SOURCE_LABEL_RE.match(text))
    return small and (grey or centered or source) or source and (grey or centered)


def _previous_image_container(node: Tag) -> Tag | None:
    sibling = node.previous_sibling
    while isinstance(sibling, NavigableString) and not str(sibling).strip():
        sibling = sibling.previous_sibling
    if not isinstance(sibling, Tag):
        return None
    if sibling.find("img") or (sibling.name or "").lower() == "img":
        return sibling
    return None


def _mark_wechat_captions(root: Tag, page_url: str = "") -> int:
    count = 0
    for image in list(root.find_all("img")):
        container: Tag = image
        # Climb out of an image-only wrapper so the caption sibling is found.
        while (
            isinstance(container.parent, Tag)
            and (container.parent.name or "").lower() in ("section", "span", "p", "a")
            and len(container.parent.find_all(["img", "iframe", "video"])) == 1
            and not _clean_text(container.parent.get_text(" ", strip=True))
        ):
            container = container.parent
        sibling = container.next_sibling
        while isinstance(sibling, NavigableString) and not str(sibling).strip():
            sibling = sibling.next_sibling
        if not isinstance(sibling, Tag):
            continue
        if sibling.has_attr("data-ca-caption"):
            continue
        if not _is_wechat_caption(sibling):
            continue
        inner = _caption_fragment(sibling, page_url)
        if not _caption_text(inner):
            continue
        paragraph = _new_caption_p(root, inner)
        sibling.replace_with(paragraph)
        count += 1
    return count


def _is_video_node(tag: Tag) -> bool:
    name = (tag.name or "").lower()
    if name in ("video", "mpvideo", "qqvideo"):
        return True
    if name.startswith("uni-youtube") or tag.has_attr("video-id"):
        return True
    if name == "iframe":
        classes = [str(c).lower() for c in (tag.get("class") or [])]
        return "video_iframe" in classes or bool(tag.get("data-mpvid"))
    return False


def _video_source(tag: Tag) -> str:
    src = tag.get("src")
    if not src and (tag.name or "").lower() == "video":
        source = tag.find("source")
        if source:
            src = source.get("src")
    return _clean_text(str(src or ""))


def _make_video(tag: Tag, page_url: str, index: int, nonce: str) -> dict[str, Any] | None:
    name = (tag.name or "").lower()
    kind = "html5"
    video_id = _clean_text(str(tag.get("data-mpvid") or ""))
    watch_url = ""
    watch_label = "前往原文观看"
    media_url = ""
    cover = ""

    if name.startswith("uni-youtube") or tag.has_attr("video-id"):
        candidate = _clean_text(str(
            tag.get("video-id") or tag.get("data-video-id") or tag.get("videoid") or ""
        ))
        if not _YT_ID_RE.match(candidate):
            return None
        kind = "youtube"
        video_id = candidate
        watch_url = _YT_WATCH.format(video_id=candidate)
        watch_label = "前往 YouTube 观看"
    elif name == "iframe" or video_id:
        kind = "wechat"
        watch_url = _safe_http_url(page_url)
        media = unquote(_clean_text(str(tag.get("data-cover") or "")))
        cover = _safe_http_url(media, page_url)
    else:
        media_url = _safe_http_url(_video_source(tag), page_url)
        cover = _safe_http_url(str(tag.get("poster") or ""), page_url)
        watch_url = _safe_http_url(page_url)

    if kind != "wechat" and not media_url and not watch_url:
        return None

    note = (
        "微信视频未下载；归档页不能播放，请前往原文观看。"
        if kind == "wechat"
        else "视频本体未下载；外部链接是否可播放取决于原站点。"
    )
    return {
        "index": index,
        "kind": kind,
        "token": f"{_VIDEO_TOKEN_PREFIX}{nonce}-{index}@@",
        "video_id": video_id,
        "watch_url": watch_url,
        "watch_label": watch_label,
        "media_url": media_url,
        "cover_url": cover,
        "note": note,
    }


def _replace_with_token(node: Tag, token: str) -> None:
    parent_name = (node.parent.name or "").lower() if isinstance(node.parent, Tag) else ""
    if parent_name in ("p", "span", "a", "strong", "em"):
        node.replace_with(NavigableString(f" {token} "))
        return
    placeholder = BeautifulSoup(f"<p>{token}</p>", "html.parser").find("p")
    node.replace_with(placeholder)


def _replace_videos(root: Tag, page_url: str, nonce: str) -> list[dict[str, Any]]:
    videos: list[dict[str, Any]] = []
    for tag in list(root.find_all(True)):
        if not _is_video_node(tag):
            continue
        record = _make_video(tag, page_url, len(videos) + 1, nonce)
        if record is None:
            continue
        _replace_with_token(tag, record["token"])
        videos.append(record)
    return videos


def _nonce() -> str:
    import secrets

    return secrets.token_hex(6)


def prepare_wechat_media(body: Tag, page_url: str) -> tuple[list[dict[str, Any]], int]:
    """Mutate a WeChat ``#js_content`` tree; return (videos, caption count)."""
    nonce = _nonce()
    captions = _mark_explicit_figcaptions(body, page_url)
    videos = _replace_videos(body, page_url, nonce)
    captions += _mark_wechat_captions(body, page_url)
    return videos, captions


def prepare_generic_html(html: str, page_url: str) -> tuple[str, list[dict[str, Any]], int]:
    """Return HTML with explicit figures/videos marked; no caption guessing."""
    soup = BeautifulSoup(html or "", "html.parser")
    root = soup.body or soup
    captions = _mark_explicit_figcaptions(root, page_url)
    videos = _replace_videos(root, page_url, _nonce())
    return str(soup), videos, captions


def _markdown_literal(value: Any) -> str:
    """Escape page-controlled text so it stays literal inside the card.

    ``!`` alone is not enough: ``archive_url._IMAGE_RE`` matches ``!\\[...``
    even when the bang is backslash-escaped, so the brackets must be escaped
    too.  Intraword underscores (``wxv_477…``) stay untouched for readability;
    entities are neutralised before Markdown metacharacters.
    """
    text = str(value or "")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("\\", "\\\\")
    text = re.sub(r"([`!\[\]()*])", r"\\\1", text)
    return re.sub(r"(?<![0-9A-Za-z])_|_(?![0-9A-Za-z])", r"\\_", text)


def _markdown_url(url: Any) -> str:
    """Percent-encode characters that would break a Markdown link target."""
    return (
        str(url or "")
        .replace("\\", "%5C")
        .replace("(", "%28")
        .replace(")", "%29")
    )


def _render_video_card(video: dict[str, Any]) -> str:
    lines = [f"> **视频 {video['index']}（未下载）**"]
    if video.get("cover_url"):
        lines += [">", f"> 封面：![视频封面]({_markdown_url(video['cover_url'])})"]
    if video.get("watch_url"):
        lines += [">", f"> 观看：[{video['watch_label']}]({_markdown_url(video['watch_url'])})"]
    if video.get("media_url") and video["media_url"] != video.get("watch_url"):
        lines += [">", f"> 视频地址：[打开视频地址]({_markdown_url(video['media_url'])})"]
    if video.get("video_id"):
        lines += [">", f"> 视频标识：{_markdown_literal(video['video_id'])}"]
    lines += [">", f"> 说明：{video['note']}"]
    return "\n".join(lines)


def finalize_media_markdown(
    text: str,
    videos: Iterable[dict[str, Any]],
) -> tuple[str, dict[str, int]]:
    """Replace temporary video tokens and promote explicit captions."""
    out = text or ""
    video_list = list(videos or [])
    for video in video_list:
        token = video.get("token")
        if not token:
            continue
        out = re.sub(
            r"[ \t]*" + re.escape(token) + r"[ \t]*",
            lambda _m, v=video: "\n\n" + _render_video_card(v) + "\n\n",
            out,
        )
    out = re.sub(r"\n{3,}", "\n\n", out)

    caption_count = 0

    def repl(match: re.Match) -> str:
        nonlocal caption_count
        caption_count += 1
        caption = match.group("caption").strip()
        caption = re.sub(r"^图注：\s*图注：", CAPTION_PREFIX, caption)
        return f"{match.group('image')}\n\n> {caption}"

    out = _CAPTION_BLOCK_RE.sub(repl, out)
    return out.strip(), {"videos": len(video_list), "captions": caption_count}


def summarize_media(content: str) -> dict[str, int]:
    return {
        "video_cards": len(re.findall(r"(?m)^> \*\*视频 \d+（未下载）\*\*$", content or "")),
        "captions": len(re.findall(r"(?m)^> 图注：", content or "")),
    }


# ---------------------------------------------------------------------------
# Reader rendering (Markdown remains authoritative)
# ---------------------------------------------------------------------------

def _image_only_inline(token: Token) -> bool:
    """True for a paragraph made only of images (plus whitespace/softbreaks)."""
    if token.type != "inline" or not token.children:
        return False
    images = 0
    for child in token.children:
        if child.type == "image":
            images += 1
        elif child.type == "softbreak":
            continue
        elif child.type == "text" and not child.content.strip():
            continue
        else:
            return False
    return images > 0


def _strip_caption_prefix(inline: Token) -> None:
    removed = False
    for child in inline.children or []:
        if removed or child.type != "text":
            continue
        if child.content.startswith(CAPTION_PREFIX):
            child.content = child.content[len(CAPTION_PREFIX):]
            removed = True
    if inline.content.startswith(CAPTION_PREFIX):
        inline.content = inline.content[len(CAPTION_PREFIX):]


def _caption_is_long(inline: Token) -> bool:
    text = inline.content
    if text.startswith(CAPTION_PREFIX):
        text = text[len(CAPTION_PREFIX):]
    return len(_clean_text(text)) > _CAPTION_LONG_THRESHOLD


def _media_block_tokens(state) -> None:
    tokens = state.tokens
    output: list[Token] = []
    i = 0
    total = len(tokens)
    while i < total:
        token = tokens[i]
        # image paragraph + explicit caption blockquote -> figure/figcaption
        if (
            token.type == "paragraph_open"
            and i + 7 < total
            and tokens[i + 1].type == "inline"
            and _image_only_inline(tokens[i + 1])
            and tokens[i + 2].type == "paragraph_close"
            and tokens[i + 3].type == "blockquote_open"
            and tokens[i + 4].type == "paragraph_open"
            and tokens[i + 5].type == "inline"
            and (tokens[i + 5].content or "").startswith(CAPTION_PREFIX)
            and tokens[i + 6].type == "paragraph_close"
            and tokens[i + 7].type == "blockquote_close"
            and _clean_text(tokens[i + 5].content[len(CAPTION_PREFIX):])
        ):
            caption = tokens[i + 5]
            _strip_caption_prefix(caption)
            figure_open = Token("figure_open", "figure", 1)
            figure_open.level = token.level
            figcaption_open = Token("figcaption_open", "figcaption", 1)
            figcaption_open.meta = {"long": _caption_is_long(tokens[i + 5])}
            output.extend([
                figure_open,
                tokens[i + 1],
                figcaption_open,
                caption,
                Token("figcaption_close", "figcaption", -1),
                Token("figure_close", "figure", -1),
            ])
            i += 8
            continue

        # explicit video card blockquote -> aside (same inner paragraphs)
        if token.type == "blockquote_open":
            depth = 0
            close_index = -1
            first_inline = ""
            for j in range(i, total):
                if tokens[j].type == "blockquote_open":
                    depth += 1
                elif tokens[j].type == "blockquote_close":
                    depth -= 1
                    if depth == 0:
                        close_index = j
                        break
                elif tokens[j].type == "inline" and not first_inline:
                    first_inline = tokens[j].content.strip()
            if close_index > 0 and VIDEO_HEADING_RE.match(first_inline):
                token.type = "video_card_open"
                token.tag = "aside"
                tokens[close_index].type = "video_card_close"
                tokens[close_index].tag = "aside"

        output.append(token)
        i += 1
    state.tokens = output


def install_media_renderer(md) -> None:
    """Install safe ``figure`` / ``figcaption`` / video-card rendering rules."""
    md.core.ruler.after("inline", "archive_media_blocks", _media_block_tokens)

    md.add_render_rule("figure_open", lambda self, tokens, idx, options, env: '<figure class="article-figure">')
    md.add_render_rule("figure_close", lambda self, tokens, idx, options, env: "</figure>")
    md.add_render_rule(
        "figcaption_open",
        lambda self, tokens, idx, options, env: (
            '<figcaption class="long">' if tokens[idx].meta.get("long") else "<figcaption>"
        ),
    )
    md.add_render_rule("figcaption_close", lambda self, tokens, idx, options, env: "</figcaption>")
    md.add_render_rule(
        "video_card_open",
        lambda self, tokens, idx, options, env: '<aside class="video-card">',
    )
    md.add_render_rule("video_card_close", lambda self, tokens, idx, options, env: "</aside>")
