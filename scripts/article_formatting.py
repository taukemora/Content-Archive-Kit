#!/usr/bin/env python3
"""Normalise article HTML emphasis before Markdown conversion.

``markdownify`` and ``trafilatura`` only understand semantic tags.  Real
articles express weight and italics through inline CSS, wrap several blocks in
one ``<strong>``, and cancel inherited emphasis with ``font-weight: normal``.
Converting such HTML directly produces literal ``**`` spanning blank lines and
silently drops CSS-only emphasis.

This module rewrites the DOM *before* Markdown conversion:

* ``font-weight`` / ``font-style`` become semantic emphasis runs.
* A formatting element that encloses blocks is split per block, so an
  emphasis marker never spans a paragraph boundary.
* A ``normal`` descendant ends an inherited emphasis run instead of creating a
  nested exception that Markdown cannot express.
* Native ``h1``-``h6`` stay headings; a short bold paragraph is never guessed
  to be a heading.

The rewrite only adds/removes inline emphasis wrappers.  Text, links, images,
captions, tables and block structure are left to the existing converters.
"""
from __future__ import annotations

import re
import unicodedata

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

BOLD_TAGS = frozenset({"strong", "b"})
ITALIC_TAGS = frozenset({"em", "i"})
HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "body", "dd", "details",
    "dialog", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer",
    "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
    "nav", "ol", "p", "pre", "section", "table", "tbody", "td", "tfoot", "th",
    "thead", "tr", "ul",
})
# Presentational inline wrappers with no Markdown meaning of their own.
_FLATTEN_TAGS = frozenset({"big", "font", "small", "span"})
# Preformatted/code-like content must keep its literal characters.
_OPAQUE_TAGS = frozenset({"code", "kbd", "pre", "samp", "script", "style", "textarea"})
# Void/media nodes never deserve an emphasis wrapper of their own.
_MEDIA_TAGS = frozenset({
    "audio", "br", "button", "canvas", "embed", "hr", "iframe", "img", "input",
    "object", "picture", "source", "svg", "video", "wbr",
})
# A line break inside one paragraph can stay inside an emphasis run, but an
# image/video boundary must not move under emphasis markers.
_MEDIA_BREAK_TAGS = _MEDIA_TAGS - {"br", "wbr"}

_DECLARATION_RE = re.compile(r"(?:^|;)\s*([A-Za-z-]+)\s*:\s*([^;}]*)")
_IMPORTANT_RE = re.compile(r"\s*!\s*important\s*$", re.I)
_WEIGHT_KEYWORDS = {"bold": True, "bolder": True, "normal": False, "lighter": False}


def _strip_important(raw: str) -> tuple[str, bool]:
    text = (raw or "").strip()
    if _IMPORTANT_RE.search(text):
        return _IMPORTANT_RE.sub("", text).strip(), True
    return text, False


def _clean_value(raw: str) -> str:
    value, _important = _strip_important(raw)
    return value.lower()


def _weight_value(raw: str) -> bool | None:
    value = _clean_value(raw)
    if not value:
        return None
    if value in _WEIGHT_KEYWORDS:
        return _WEIGHT_KEYWORDS[value]
    match = re.fullmatch(r"(\d{1,4})", value)
    if not match:
        return None
    number = int(match.group(1))
    if number >= 600:
        return True
    if number <= 500:
        return False
    return None


def _slant_value(raw: str) -> bool | None:
    value = _clean_value(raw)
    if not value:
        return None
    if value == "normal":
        return False
    if value in ("italic", "oblique") or value.startswith("oblique "):
        return True
    return None


def parse_inline_style(style: str | None) -> tuple[bool | None, bool | None]:
    """Return ``(bold, italic)`` overrides from an inline style attribute.

    ``True`` forces the property on, ``False`` is an explicit ``normal``
    override and ``None`` means "inherit from the surrounding markup".  Within
    one attribute a later declaration wins, except that ``!important`` always
    beats a plain declaration; an ``!important`` declaration is only replaced
    by a later ``!important`` one.  Unparseable values are ignored rather than
    resetting an earlier valid declaration.
    """
    if not style:
        return (None, None)
    weight: bool | None = None
    weight_important = False
    slant: bool | None = None
    slant_important = False
    for name, raw in _DECLARATION_RE.findall(style):
        prop = name.strip().lower()
        if prop not in ("font-weight", "font-style"):
            continue
        important = _strip_important(raw)[1]
        if prop == "font-weight":
            value = _weight_value(raw)
            if value is None:
                continue
            if weight_important and not important:
                continue
            weight, weight_important = value, important
        else:
            value = _slant_value(raw)
            if value is None:
                continue
            if slant_important and not important:
                continue
            slant, slant_important = value, important
    return (weight, slant)


def _local_emphasis(tag: Tag) -> tuple[bool | None, bool | None]:
    name = (tag.name or "").lower()
    bold: bool | None = True if name in BOLD_TAGS else None
    italic: bool | None = True if name in ITALIC_TAGS else None
    style = tag.get("style")
    style_bold, style_italic = parse_inline_style(style if isinstance(style, str) else None)
    if style_bold is not None:
        bold = style_bold
    if style_italic is not None:
        italic = style_italic
    return bold, italic


def _is_block(node: object) -> bool:
    return isinstance(node, Tag) and (node.name or "").lower() in BLOCK_TAGS


def _breaks_emphasis_run(node: object) -> bool:
    if _is_block(node):
        return True
    return isinstance(node, Tag) and (node.name or "").lower() in _MEDIA_BREAK_TAGS


def _has_emphasis_content(nodes: list) -> bool:
    for node in nodes:
        if isinstance(node, Tag):
            name = (node.name or "").lower()
            if name in _MEDIA_TAGS or name in BLOCK_TAGS:
                continue
            return True
        if isinstance(node, NavigableString) and not isinstance(node, Comment):
            if str(node).strip():
                return True
    return False


def _split_edge_whitespace(nodes: list):
    """Move leading/trailing whitespace outside an emphasis wrapper.

    ``**text **`` is not valid CommonMark emphasis, so the whitespace has to
    stay outside the delimiters or the stars render literally.
    """
    head: list = []
    core = list(nodes)
    tail: list = []
    while core and isinstance(core[0], NavigableString) and not isinstance(core[0], Comment):
        text = str(core[0])
        match = re.match(r"\s+", text)
        if not match:
            break
        head.append(NavigableString(match.group(0)))
        rest = text[match.end():]
        if rest:
            core[0] = NavigableString(rest)
            break
        core.pop(0)
    while core and isinstance(core[-1], NavigableString) and not isinstance(core[-1], Comment):
        text = str(core[-1])
        match = re.search(r"\s+$", text)
        if not match:
            break
        tail.insert(0, NavigableString(match.group(0)))
        rest = text[:match.start()]
        if rest:
            core[-1] = NavigableString(rest)
            break
        core.pop()
    return head, core, tail


def _wrap(container: Tag, name: str, nodes: list) -> Tag:
    tag = container.new_tag(name)
    for node in nodes:
        if getattr(node, "parent", None) is not None:
            node.extract()
        tag.append(node)
    return tag


def _merge(items: list, implied_bold: bool, implied_italic: bool, container: Tag) -> list:
    """Merge ``(bold, italic, node)`` items into runs of real emphasis tags."""
    out: list = []
    index = 0
    while index < len(items):
        bold, italic, node = items[index]
        if _breaks_emphasis_run(node):
            out.append(node)
            index += 1
            continue
        run: list = []
        state = (bold, italic)
        while index < len(items):
            item_bold, item_italic, candidate = items[index]
            if _breaks_emphasis_run(candidate) or (item_bold, item_italic) != state:
                break
            run.append(candidate)
            index += 1
        head, core, tail = _split_edge_whitespace(run)
        wrap_bold = bold and not implied_bold
        wrap_italic = italic and not implied_italic
        if core and (wrap_bold or wrap_italic) and _has_emphasis_content(core):
            if wrap_bold:
                core = [_wrap(container, "strong", core)]
            if wrap_italic:
                core = [_wrap(container, "em", core)]
        out.extend(head)
        out.extend(core)
        out.extend(tail)
    return out


def _has_local_override(tag: Tag, bold: bool, italic: bool) -> bool:
    """True when ``tag`` contains emphasis that differs from its inheritance.

    Such an element cannot be wrapped as a whole: Markdown has no syntax for
    emphasising only part of a link's text, so the inner runs must be
    materialised inside the element instead.
    """
    for descendant in tag.find_all(True):
        name = (descendant.name or "").lower()
        if name in _OPAQUE_TAGS or name in ("script", "style"):
            continue
        local_bold, local_italic = _local_emphasis(descendant)
        if local_bold is not None and local_bold != bold:
            return True
        if local_italic is not None and local_italic != italic:
            return True
    return False


def _collect(children: list, bold: bool, italic: bool) -> list:
    items: list = []
    for child in children:
        if isinstance(child, Comment):
            continue
        if not isinstance(child, Tag):
            items.append((bold, italic, child))
            continue
        name = (child.name or "").lower()
        if name in ("script", "style"):
            continue
        if child.has_attr("data-ca-caption"):
            # Captions are rendered from their Markdown blockquote contract;
            # keep the existing caption pipeline authoritative.
            items.append((False, False, child))
            continue
        local_bold, local_italic = _local_emphasis(child)
        child_bold = bold if local_bold is None else local_bold
        child_italic = italic if local_italic is None else local_italic
        if name in BOLD_TAGS or name in ITALIC_TAGS or name in _FLATTEN_TAGS:
            items.extend(_collect(list(child.children), child_bold, child_italic))
            continue
        if name in _OPAQUE_TAGS:
            items.append((child_bold, child_italic, child))
            continue
        if name in BLOCK_TAGS:
            _render(child, child_bold, child_italic, name in HEADING_TAGS, False)
            items.append((False, False, child))
            continue
        if _has_local_override(child, child_bold, child_italic):
            # Materialise the inner runs and stop the outer merge from
            # wrapping the whole inline element (a link's text cannot be
            # partially emphasised from outside).
            _render(child, child_bold, child_italic, False, False)
            items.append((False, False, child))
            continue
        _render(child, child_bold, child_italic, child_bold, child_italic)
        items.append((child_bold, child_italic, child))
    return items


def _render(container: Tag, bold: bool, italic: bool, implied_bold: bool, implied_italic: bool) -> None:
    items = _collect(list(container.children), bold, italic)
    merged = _merge(items, implied_bold, implied_italic, container)
    container.clear()
    for node in merged:
        container.append(node)


def _is_unicode_punctuation(char: str) -> bool:
    """True for a CommonMark punctuation character (Unicode ``P`` or ``S``)."""
    return bool(char) and unicodedata.category(char)[0] in ("P", "S")


def _adjacent_flow_char(node: Tag, *, forward: bool) -> str | None:
    """Return the character Markdown will see just outside ``node``.

    Only plain text siblings count: when the neighbour is another inline tag the
    delimiter is adjacent to ``]``, a backtick or a ``*`` in the converted
    Markdown (punctuation), so no relocation is needed.  Walking up through
    inline parents finds the continuation of the same inline flow; a block
    boundary or an opaque container ends it.
    """
    current: Tag = node
    while True:
        sibling = current.next_sibling if forward else current.previous_sibling
        while sibling is not None:
            nxt = sibling.next_sibling if forward else sibling.previous_sibling
            if isinstance(sibling, Comment):
                sibling = nxt
                continue
            if isinstance(sibling, NavigableString):
                text = str(sibling)
                if text:
                    return text[0] if forward else text[-1]
            else:
                # Any inline element converts to Markdown punctuation at its
                # edge (link/code/image), which keeps the delimiter flanking.
                return None
            sibling = nxt
        parent = current.parent
        if not isinstance(parent, Tag):
            return None
        parent_name = (parent.name or "").lower()
        if parent_name in BLOCK_TAGS or parent_name in _OPAQUE_TAGS:
            return None
        current = parent


def _edge_punctuation(text: str, *, leading: bool) -> str:
    """Return the contiguous punctuation/symbol run at one edge of ``text``."""
    if leading:
        count = 0
        for char in text:
            if not _is_unicode_punctuation(char):
                break
            count += 1
        return text[:count]
    count = 0
    for char in reversed(text):
        if not _is_unicode_punctuation(char):
            break
        count += 1
    return text[len(text) - count:] if count else ""


def _edge_text_node(tag: Tag, *, first: bool) -> NavigableString | None:
    children = [child for child in tag.children if not isinstance(child, Comment)]
    if not children:
        return None
    node = children[0] if first else children[-1]
    return node if isinstance(node, NavigableString) else None


def _inside_opaque(tag: Tag) -> bool:
    """True when ``tag`` sits inside code/pre/script-like literal content."""
    parent = tag.parent
    while isinstance(parent, Tag):
        if (parent.name or "").lower() in _OPAQUE_TAGS:
            return True
        parent = parent.parent
    return False


def _emphasis_has_core(tag: Tag) -> bool:
    """True when some visible character is neither whitespace nor punctuation."""
    for node in tag.find_all(string=True):
        if isinstance(node, Comment):
            continue
        if any(
            not char.isspace() and not _is_unicode_punctuation(char)
            for char in str(node)
        ):
            return True
    return False


def _relocate_emphasis_edges(root: Tag) -> None:
    """Move punctuation that breaks CommonMark flanking outside emphasis.

    ``**文｜**合成作者`` renders its stars literally because the closing run is
    preceded by punctuation and followed by a letter; ``前文**，强调**后文``
    fails the mirrored opening rule.  A closing/opening run followed (preceded)
    by whitespace or punctuation is valid, so only those two failing shapes are
    touched: the contiguous edge punctuation moves outside the emphasis range
    while text order and ownership (links, code, media) stay intact.  When the
    emphasis range is punctuation only it degrades to plain text: the affected
    punctuation simply loses its weight, which is the documented cost of this
    DOM-local repair.
    """
    tags = list(root.find_all(["strong", "b", "em", "i"]))
    # Innermost emphasis first: moving punctuation out of an inner run lets an
    # outer run see the new edge on a later iteration.
    for tag in reversed(tags):
        if not isinstance(tag.parent, Tag):
            continue
        if _inside_opaque(tag):
            continue

        leading = _edge_text_node(tag, first=True)
        if leading is not None:
            run = _edge_punctuation(str(leading), leading=True)
            before = _adjacent_flow_char(tag, forward=False)
            if (
                run
                and before is not None
                and not before.isspace()
                and not _is_unicode_punctuation(before)
            ):
                leading.replace_with(NavigableString(str(leading)[len(run):]))
                tag.insert_before(NavigableString(run))

        trailing = _edge_text_node(tag, first=False)
        if trailing is not None:
            run = _edge_punctuation(str(trailing), leading=False)
            after = _adjacent_flow_char(tag, forward=True)
            if (
                run
                and after is not None
                and not after.isspace()
                and not _is_unicode_punctuation(after)
            ):
                text = str(trailing)
                trailing.replace_with(NavigableString(text[: len(text) - len(run)]))
                tag.insert_after(NavigableString(run))

        if not _emphasis_has_core(tag):
            tag.unwrap()


def normalize_formatting_tree(root: Tag) -> Tag:
    """Rewrite emphasis inside ``root`` in place and return it."""
    name = (root.name or "").lower()
    local_bold, local_italic = _local_emphasis(root)
    # A root style (WeChat's ``#js_content`` often sets font-weight) is
    # inherited by every descendant, so it must seed the traversal.  Text is
    # marked semantically inside the root because CSS on the wrapper itself is
    # invisible to Markdown; a root tag that is already semantic is implied.
    _render(
        root,
        bool(local_bold),
        bool(local_italic),
        name in BOLD_TAGS,
        name in ITALIC_TAGS,
    )
    _relocate_emphasis_edges(root)
    return root


def normalize_formatting_html(html: str) -> str:
    """Return ``html`` with CSS/cross-block emphasis normalised."""
    if not html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    root = soup.body or soup
    normalize_formatting_tree(root)
    return str(soup)
