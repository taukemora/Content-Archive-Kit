#!/usr/bin/env python3
"""Read-only HTTP acceptance check for a deployed archive reader.

Standard library only, no dependencies. Verifies over real HTTP that:
  * / and /index.html return the local index.html bytes and the HTML headers,
  * every image referenced by the embedded archive-data is served with the same
    bytes as the local file, the matching MIME type, no-cache/nosniff and the
    image CSP,
  * sensitive paths, missing files and non-GET/HEAD methods are refused.

It never reads local secrets: paths like .env are only probed over HTTP and the
response body is discarded. Nothing is written anywhere.

Optional deployment helper, not part of the unit-test suite: run it by hand
against a running reader (``--root`` is a local copy of the same archive used to
compare bytes). It makes no assumption about host names.
"""
from __future__ import annotations

import argparse
import hashlib
import html as html_mod
import json
import posixpath
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".avif", ".bmp", ".ico")

MIME_BY_EXT = {
    ".png": {"image/png"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".webp": {"image/webp"},
    ".gif": {"image/gif"},
    ".svg": {"image/svg+xml"},
    ".avif": {"image/avif"},
    # Both spellings appear in nginx mime.types across versions.
    ".bmp": {"image/x-ms-bmp", "image/bmp"},
    ".ico": {"image/x-icon", "image/vnd.microsoft.icon"},
}

IMAGE_CSP = (
    "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; sandbox"
)

# Never served: state, configs, sources, tooling, entry bodies and raw dumps.
SENSITIVE_PATHS = (
    "/.env",
    "/.state/",
    "/.state/seen.json",
    "/.venv/",
    "/index.csv",
    "/index.md",
    "/sources.yaml",
    "/taxonomy.yaml",
    "/README.md",
    "/AGENTS.md",
    "/requirements-web.txt",
    "/bin/ca",
    "/scripts/build_html.py",
    "/scripts/archive_page.html",
    "/deploy/nginx.conf",
    "/compose.yaml",
    "/trash/",
    "/library/",
    "/library/tech/",
)

HTML_PATHS = ("/", "/index.html")
MAX_FAILURE_DETAILS = 50


class Client:
    def __init__(self, base: str, timeout: float):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.checks = 0
        self.failures: list[dict[str, str]] = []

    def request(self, path: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
        req = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, {k.lower(): v for k, v in exc.headers.items()}, b""

    def expect(self, ok: bool, check: str, detail: str = "") -> None:
        self.checks += 1
        if not ok:
            self.failures.append({"check": check, "detail": detail})


def extract_image_paths(html_text: str) -> tuple[int, list[str], list[str]]:
    """Return (article count, all local image URLs, unexpected non-local srcs)."""
    match = re.search(
        r'<script id="archive-data" type="application/json">(.*?)</script>', html_text, re.S
    )
    if not match:
        raise ValueError("archive-data script not found in index.html")
    articles = json.loads(match.group(1))
    refs: list[str] = []
    unexpected: list[str] = []
    for article in articles:
        for src in re.findall(r'<img\s[^>]*\bsrc="([^"]*)"', article.get("body_html", "")):
            src = html_mod.unescape(src)
            path = src.split("#", 1)[0].split("?", 1)[0]
            if path.startswith("library/"):
                refs.append(path)
            else:
                unexpected.append(src)
    return len(articles), refs, unexpected


def entry_dir_of(image_path: str) -> str:
    return posixpath.dirname(posixpath.dirname(image_path))


def check_html(client: Client, html_bytes: bytes) -> None:
    for path in HTML_PATHS:
        status, headers, body = client.request(path)
        ctype = headers.get("content-type", "")
        client.expect(status == 200, f"GET {path}", f"status {status}")
        client.expect(
            body == html_bytes,
            f"GET {path}",
            f"body {len(body)} bytes != local index.html {len(html_bytes)} bytes",
        )
        client.expect(ctype.startswith("text/html"), f"GET {path}", f"content-type {ctype!r}")
        client.expect(
            "no-cache" in headers.get("cache-control", "").lower(),
            f"GET {path}",
            f"cache-control {headers.get('cache-control')!r}",
        )
        client.expect(
            headers.get("x-content-type-options") == "nosniff",
            f"GET {path}",
            f"x-content-type-options {headers.get('x-content-type-options')!r}",
        )
        client.expect(
            "sandbox" not in headers.get("content-security-policy", "").lower(),
            f"GET {path}",
            "HTML response must not carry the image CSP",
        )

    status, headers, _ = client.request("/", "HEAD")
    client.expect(status == 200, "HEAD /", f"status {status}")
    client.expect(
        headers.get("content-type", "").startswith("text/html"),
        "HEAD /",
        f"content-type {headers.get('content-type')!r}",
    )


def check_images(client: Client, root: Path, image_paths: list[str]) -> None:
    for rel in image_paths:
        path = "/" + rel
        ext = posixpath.splitext(rel)[1].lower()
        local = root / urllib.parse.unquote(rel)
        status, headers, body = client.request(path)
        client.expect(status == 200, f"GET {path}", f"status {status}")
        if not local.is_file():
            client.expect(False, f"GET {path}", f"local file missing: {local}")
            continue
        expected = local.read_bytes()
        client.expect(
            body == expected,
            f"GET {path}",
            f"body {len(body)} bytes (sha256 {hashlib.sha256(body).hexdigest()[:12]}) "
            f"!= local {len(expected)} bytes (sha256 {hashlib.sha256(expected).hexdigest()[:12]})",
        )
        ctype = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        client.expect(
            ext in MIME_BY_EXT and ctype in MIME_BY_EXT[ext],
            f"GET {path}",
            f"content-type {ctype!r} for {ext}",
        )
        client.expect(
            "no-cache" in headers.get("cache-control", "").lower(),
            f"GET {path}",
            f"cache-control {headers.get('cache-control')!r}",
        )
        client.expect(
            headers.get("x-content-type-options") == "nosniff",
            f"GET {path}",
            f"x-content-type-options {headers.get('x-content-type-options')!r}",
        )
        csp = " ".join(headers.get("content-security-policy", "").split())
        client.expect(csp == IMAGE_CSP, f"GET {path}", f"content-security-policy {csp!r}")


def check_negative(client: Client, image_paths: list[str]) -> None:
    # Keep negative probes and JSON output working for an empty archive too.
    sample = image_paths[0] if image_paths else "library/tech/__missing__/images/001.png"
    entry = entry_dir_of(sample)
    paths = list(SENSITIVE_PATHS) + [
        f"/{entry}/entry.md",
        f"/{entry}/raw.html.gz",
        f"/{entry}/extraction/",
        f"/{entry}/images/",
        f"/{entry}/images/__missing__.png",
        f"/{entry}/images/__missing__.svg",
        f"/{entry}/images/001.txt",
        f"/{entry}/images/",
        "/library/../README.md",
        "/library/%2e%2e/README.md",
        "/library/tech/./index.csv",
        "/index.html/",
    ]
    for path in paths:
        status, _, _ = client.request(path)
        client.expect(
            400 <= status < 500,
            f"GET {path}",
            f"status {status}, expected 4xx (no content leak)",
        )

    targets = ("/", "/" + sample)
    for path in targets:
        for method in ("PUT", "POST", "DELETE", "PATCH", "OPTIONS"):
            status, _, _ = client.request(path, method)
            client.expect(
                400 <= status < 500,
                f"{method} {path}",
                f"status {status}, expected 4xx",
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only HTTP check of the archive reader.")
    parser.add_argument("--url", required=True, help="base URL of the reader, e.g. http://127.0.0.1:8080")
    parser.add_argument("--root", required=True, help="local full copy of the archive")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-request seconds (default 30)")
    args = parser.parse_args()

    root = Path(args.root).expanduser()
    index = root / "index.html"
    result: dict[str, object] = {"ok": False, "target": args.url.rstrip("/"), "root": str(root)}
    if not index.is_file():
        result["error"] = f"local index.html not found under {root}"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2

    html_bytes = index.read_bytes()
    try:
        articles, refs, unexpected = extract_image_paths(html_bytes.decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        result["error"] = f"cannot parse index.html: {exc}"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2

    client = Client(args.url, args.timeout)
    try:
        client.expect(not unexpected, "index.html", f"non-local <img src>: {unexpected[:5]}")
        unique = sorted(set(refs))
        client.expect(bool(unique), "index.html", "no local image references found")
        for rel in unique:
            ext = posixpath.splitext(rel)[1].lower()
            client.expect(ext in IMAGE_EXTS, "index.html", f"image outside allowed set: {rel}")

        check_html(client, html_bytes)
        check_images(client, root, unique)
        check_negative(client, unique)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        result["error"] = f"cannot reach {client.base}: {exc}"
        result["checks"] = client.checks
        result["failures"] = client.failures[:MAX_FAILURE_DETAILS]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2

    result.update(
        {
            "ok": not client.failures,
            "checks": client.checks,
            "articles": articles,
            "images": {"referenced": len(refs), "unique": len(set(refs))},
            "failures_total": len(client.failures),
            "failures": client.failures[:MAX_FAILURE_DETAILS],
        }
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
