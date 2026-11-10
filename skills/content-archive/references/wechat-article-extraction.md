# WeChat Article Extraction

Use for `mp.weixin.qq.com/s/...` article links. This recipe was validated against a long-form public article: a direct HTTP request returned the full HTML, the body yielded about 20.5k characters and 32 images, and title/account/author/date were recoverable. A headless browser was redirected to `wappoc_appmsgcaptcha`, so browser automation is not the primary path.

## Fetch

- GET the article URL with a desktop Chrome User-Agent and `Accept-Language: zh-CN,zh;q=0.9`.
- Do not send cookies unless the user supplied them; public articles usually work without.
- Rate-limit batch runs: one request per article with a short delay; cache the HTML by URL.

## Selectors

| Field | Selector / pattern |
|---|---|
| Title | `#activity-name` or `meta[property="og:title"]` |
| Account | `#js_name` |
| Author | `#js_author_name` |
| Publish time | `var ct = "(\d+)"` (Unix seconds); `#publish_time` is often empty |
| Body | `#js_content` |
| Images | `#js_content img[data-src]` |

## Extraction

1. Parse with BeautifulSoup (`html.parser`).
2. Take `#js_content`; remove scripts/styles.
3. For every `img`, set `src = data-src or src`, delete `data-src`, and drop tracking attributes if desired.
4. Convert the node to Markdown with `markdownify(..., heading_style="ATX")`. Expect a leading account-promo block; keep it or strip it per project policy, but record the choice.
5. Save `raw.html.gz` before conversion.

## Image Localization

- Collect `data-src` URLs; HTML-unescape `&amp;`.
- Download with `Referer: https://mp.weixin.qq.com/` and a normal User-Agent.
- Detect the real format from magic bytes (`RIFF....WEBP`, `\x89PNG`, `\xff\xd8\xff`), not the URL suffix.
- Save as `images/001.webp`, etc.; rewrite Markdown image links to the local relative path.
- Cap count and bytes per article; on failure keep the remote URL and log it.

## Failure Modes

- `wappoc_appmsgcaptcha` redirect: the article is challenging automation. Fall back to a user-supplied HTML save or a manual browser save; do not loop retries.
- Empty `#js_content`: the page may be a video/album or a deleted article; report it, do not fabricate.
- `var ct` missing: use `meta[property="article:published_time"]` or the fetch date with `date_source: fetched` and `published_unknown: true`.

## Minimal Python Sketch

```python
import re
import urllib.request
from bs4 import BeautifulSoup
from markdownify import markdownify


def fetch(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")


html = fetch(url)
soup = BeautifulSoup(html, "html.parser")
title = soup.select_one("#activity-name").get_text(strip=True)
account = soup.select_one("#js_name").get_text(strip=True)
author = soup.select_one("#js_author_name")
ct = re.search(r'var ct = "(\d+)"', html)
body = soup.select_one("#js_content")
for img in body.select("img"):
    img["src"] = img.get("data-src") or img.get("src")
    img.attrs.pop("data-src", None)
md = markdownify(str(body), heading_style="ATX")
```

Install `beautifulsoup4` and `markdownify`; for generic extraction install `trafilatura`. Before relying on trafilatura, verify `import lxml.etree` in the chosen venv — a too-new interpreter can install an lxml build that does not import.
