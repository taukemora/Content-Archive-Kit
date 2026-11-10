#!/usr/bin/env python3
"""Shared helpers for Content Archive Kit scripts."""
from __future__ import annotations

import gzip
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import tempfile
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
import yaml

try:  # POSIX advisory locking; degrade gracefully on non-POSIX platforms.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX only
    fcntl = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))
SUMMARY_MIN = 200
SUMMARY_MAX = 250
SHORT_ARTICLE_CHARS = 800
SHORT_MIN = 100
SHORT_MAX = 150

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "scene", "wxfrom", "wx_lazy", "wx_co", "randomid", "tp",
    "isappinstalled", "clicktime", "enterid", "ascene", "devicetype",
    "version", "nettype", "lang", "exportkey", "pass_ticket",
    "realreporttime", "sessionid", "key", "uin", "nickname",
    "sharer_sharetime", "sharer_shareid", "chksm", "mpshare", "srcid",
    "sharer_shareinfo", "sharer_shareinfo_first", "subscene",
}

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DEFAULT_HEADERS = {"User-Agent": DEFAULT_UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}

CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
ULA_NET = ipaddress.ip_network("fc00::/7")  # covers fd00::/8


def now() -> datetime:
    return datetime.now(TZ)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).astimezone(TZ).isoformat(timespec="seconds")


def load_yaml(path: Path | str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def taxonomy() -> dict:
    return load_yaml(ROOT / "taxonomy.yaml")


def sources_config() -> dict:
    return load_yaml(ROOT / "sources.yaml")


def category_slugs() -> set[str]:
    return {c["slug"] for c in taxonomy().get("categories", [])}


def context_slugs() -> set[str]:
    return {c["slug"] for c in taxonomy().get("contexts", [])}


def is_prohibited_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (
        not ip.is_global
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
        or getattr(ip, "is_site_local", False)
    ):
        return True
    if ip.version == 4:
        if ip in CGNAT_NET:
            return True
    elif ip.version == 6:
        if ip in ULA_NET:
            return True
        if ip.ipv4_mapped and is_prohibited_ip(ip.ipv4_mapped):
            return True
        sixtofour = getattr(ip, "sixtofour", None)
        if sixtofour is not None and is_prohibited_ip(sixtofour):
            return True
        teredo = getattr(ip, "teredo", None)
        if teredo:
            for embedded in teredo:
                if embedded is not None and is_prohibited_ip(embedded):
                    return True
    return False


def assert_public_url(url: str) -> None:
    url = (url or "").strip()
    if not url:
        raise ValueError("URL cannot be empty")
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"Prohibited URL scheme: {scheme}")
    hostname = parts.hostname
    if not hostname:
        raise ValueError(f"URL missing hostname: {url}")

    try:
        ip = ipaddress.ip_address(hostname)
        if is_prohibited_ip(ip):
            raise ValueError(f"Prohibited IP address: {hostname}")
        return
    except ValueError as e:
        if "Prohibited IP address" in str(e):
            raise

    try:
        addr_infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except Exception as e:
        raise ValueError(f"DNS resolution failed for {hostname}: {e}")

    if not addr_infos:
        raise ValueError(f"No IP addresses resolved for {hostname}")

    for info in addr_infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            raise ValueError(f"Invalid resolved IP address: {ip_str}")
        if is_prohibited_ip(ip):
            raise ValueError(f"Prohibited IP address resolved for {hostname}: {ip_str}")


STREAM_CHUNK_SIZE = 64 * 1024


def _close_quietly(resp) -> None:
    close = getattr(resp, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _read_body_limited(resp, max_bytes: int) -> bytes:
    """Read at most ``max_bytes`` from a streaming response, else raise ValueError."""
    headers = getattr(resp, "headers", None)
    content_length = None
    if headers is not None:
        try:
            content_length = headers.get("Content-Length")
        except Exception:
            content_length = None
    if content_length is not None and str(content_length).strip().isdigit():
        if int(str(content_length).strip()) > max_bytes:
            _close_quietly(resp)
            raise ValueError(f"response Content-Length {content_length} exceeds {max_bytes} bytes")

    chunks: list[bytes] = []
    total = 0
    iter_content = getattr(resp, "iter_content", None)
    if callable(iter_content):
        try:
            iterator = iter_content(STREAM_CHUNK_SIZE)
            for chunk in iterator:
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    _close_quietly(resp)
                    raise ValueError(f"response body exceeds {max_bytes} bytes")
                chunks.append(chunk)
            return b"".join(chunks)
        except TypeError:
            # Not a real streaming response (e.g. unittest MagicMock): fall back below.
            chunks = []
            total = 0

    data = resp.content
    if data is None:
        data = b""
    if not isinstance(data, (bytes, bytearray)):
        try:
            data = bytes(data)
        except Exception:
            data = b""
    if len(data) > max_bytes:
        _close_quietly(resp)
        raise ValueError(f"response body exceeds {max_bytes} bytes")
    return bytes(data)


def safe_fetch(
    url: str,
    referer: str | None = None,
    headers: dict | None = None,
    max_redirects: int = 3,
    timeout: int = 40,
    session: requests.Session | None = None,
    stream: bool = False,
    max_bytes: int | None = 10 * 1024 * 1024,
) -> requests.Response:
    current_url = url
    req_headers = dict(DEFAULT_HEADERS)
    if headers:
        req_headers.update(headers)
    if referer:
        req_headers["Referer"] = referer

    s = session or requests.Session()
    s.trust_env = False
    redirects_followed = 0
    # Always stream at the transport layer so we can enforce max_bytes without
    # buffering an unbounded body in memory.
    transport_stream = bool(stream) or max_bytes is not None

    while True:
        assert_public_url(current_url)
        r = s.get(
            current_url,
            headers=req_headers,
            timeout=timeout,
            allow_redirects=False,
            stream=transport_stream,
        )

        if r.is_redirect and "Location" in r.headers:
            location = r.headers["Location"]
            r.close()
            redirects_followed += 1
            if redirects_followed > max_redirects:
                raise ValueError(f"Too many redirects (max {max_redirects}) for {url}")
            next_url = urljoin(current_url, location)
            assert_public_url(next_url)
            req_headers["Referer"] = current_url
            current_url = next_url
            continue

        r.raise_for_status()

        if stream:
            # NEVER touch r.apparent_encoding / r.text / r.content here: those
            # properties consume the whole (possibly unbounded) stream.
            return r

        if max_bytes is not None:
            data = _read_body_limited(r, max_bytes)
            try:
                r._content = data
                r._content_consumed = True
            except Exception:
                pass
            _close_quietly(r)

        current_encoding = ""
        try:
            current_encoding = (r.encoding or "").lower()
        except Exception:
            current_encoding = ""
        if not current_encoding or current_encoding in ("iso-8859-1", "latin-1", "ascii"):
            try:
                apparent = r.apparent_encoding
            except Exception:
                apparent = None
            if apparent:
                r.encoding = apparent
        return r


SCRATCH_ENV = "CA_SCRATCH_ROOT"


def scratch_root() -> Path:
    """Throwaway working directory for extraction output.

    ``CA_SCRATCH_ROOT`` when set, otherwise the platform temp directory
    (``tempfile.gettempdir()``, which honours ``TMPDIR``).  Point it at a
    dedicated directory: the scratch root is an allowlisted location for
    user-supplied content files.
    """
    override = (os.environ.get(SCRATCH_ENV) or "").strip()
    base = Path(override).expanduser() if override else Path(tempfile.gettempdir())
    return base.resolve()


def is_allowed_file_path(p: Path | str, root: Path | None = None) -> bool:
    rp = Path(p).resolve()
    base_root = (root or ROOT).resolve()

    # Anything inside the project root is only allowed under .state/: project
    # files (sources.yaml, taxonomy.yaml, ...) must never be accepted as a
    # user-supplied content/summary/raw file, even when the whole project has
    # been copied into an allowlisted sandbox such as the configured scratch
    # root.
    try:
        rp.relative_to(base_root)
        in_project = True
    except ValueError:
        in_project = False

    if in_project:
        try:
            rp.relative_to((base_root / ".state").resolve())
            return True
        except ValueError:
            return False

    allowed_dirs = [
        Path("/tmp").resolve(),
        scratch_root(),
    ]
    for d in allowed_dirs:
        try:
            rp.relative_to(d)
            return True
        except ValueError:
            continue
    return False


def canonicalize_url(url: str, keep_fragment: bool = False) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    scheme = (parts.scheme or "https").lower()
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in TRACKING_PARAMS and not k.startswith("utm_")
    ]
    query.sort()
    fragment = parts.fragment if keep_fragment else ""
    return urlunsplit((scheme, host, path, urlencode(query, doseq=True), fragment))


def dedupe_key(url: str, keep_fragment: bool | None = None) -> str:
    """Canonical URL for duplicate detection.

    Fragments are kept for documentation sites (``docs.bigmodel.cn`` or any
    ``type: docs`` host configured in ``sources.yaml``) because those pages use
    ``#anchor`` sections as distinct entries.

    ``keep_fragment`` is explicit when not None (backwards-compatible override).
    """
    if keep_fragment is not None:
        return canonicalize_url(url, keep_fragment=bool(keep_fragment))

    keep = "docs.bigmodel.cn" in (url or "")
    if not keep:
        try:
            host = (urlsplit(url or "").hostname or "").lower()
        except Exception:
            host = ""
        if host:
            for doc_host in docs_source_hosts():
                if host == doc_host or host.endswith("." + doc_host):
                    keep = True
                    break
    return canonicalize_url(url, keep_fragment=keep)


_DOCS_HOSTS_CACHE: dict[str, tuple[float, set[str]]] = {}


def docs_source_hosts() -> set[str]:
    """Hosts of enabled ``type: docs`` sources (cached by sources.yaml mtime)."""
    path = ROOT / "sources.yaml"
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return set()

    cached = _DOCS_HOSTS_CACHE.get(str(path))
    if cached is not None and cached[0] == stamp:
        return set(cached[1])

    hosts: set[str] = set()
    try:
        cfg = load_yaml(path)
    except Exception:
        cfg = {}
    for src in cfg.get("sources") or []:
        if not isinstance(src, dict):
            continue
        if src.get("type") != "docs" or not src.get("enabled", True):
            continue
        for field in ("homepage", "list_url", "feed", "url"):
            value = src.get(field)
            if not value:
                continue
            try:
                host = (urlsplit(str(value)).hostname or "").lower()
            except Exception:
                continue
            if host:
                hosts.add(host)

    _DOCS_HOSTS_CACHE[str(path)] = (stamp, hosts)
    return set(hosts)


def slugify(text: str, max_len: int = 48) -> str:
    text = unicodedata.normalize("NFKC", text or "").strip().lower()
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text, flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    if not text:
        text = hashlib.sha1((text or "entry").encode("utf-8")).hexdigest()[:12]
    return text[:max_len].strip("-")


def count_summary_chars(text: str) -> int:
    """CJK characters + Latin word tokens + numeric tokens; punctuation ignored."""
    text = text or ""
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]+(?:[-'’][A-Za-z]+)*", text))
    numeric = len(re.findall(r"\d+(?:\.\d+)?%?", text))
    return cjk + latin + numeric


def split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm = yaml.safe_load(parts[1]) or {}
    body = parts[2].lstrip("\n")
    return fm, body


def dump_frontmatter(fm: dict) -> str:
    return "---\n" + yaml.safe_dump(
        fm, allow_unicode=True, sort_keys=False, default_flow_style=False
    ).rstrip() + "\n---\n"


def read_entry(path: Path | str) -> tuple[dict, str]:
    text = Path(path).read_text(encoding="utf-8")
    return split_frontmatter(text)


def write_entry(path: Path | str, fm: dict, body: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = dump_frontmatter(fm) + "\n" + body.lstrip("\n")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def write_json_atomic(path: Path | str, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def write_text_atomic(path: Path | str, content: str, encoding: str = "utf-8") -> None:
    """Write text to ``path`` via a same-directory temp file + os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


@contextmanager
def state_lock(root: Path | None = None):
    """Exclusive advisory lock on ``(root or ROOT)/.state/.lock``."""
    state_dir = (Path(root) if root is not None else ROOT) / ".state"
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / ".lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield lock_path
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def load_json(path: Path | str, default):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def seen_path() -> Path:
    return ROOT / ".state" / "seen.json"


def seen_bak_path(sp: Path | None = None) -> Path:
    p = sp or seen_path()
    return p.with_suffix(p.suffix + ".bak")


def load_seen(path: Path | None = None) -> dict:
    p = path or seen_path()
    bak = seen_bak_path(p)

    if not p.exists():
        if bak.exists():
            try:
                data = json.loads(bak.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("entries", {})
                    return data
            except Exception as e:
                raise ValueError(f"Primary seen missing and backup failed to parse: {e}")
        return {"entries": {}}

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"seen file {p} is not a valid JSON dict")
        data.setdefault("entries", {})
        return data
    except Exception as primary_err:
        if bak.exists():
            try:
                bak_data = json.loads(bak.read_text(encoding="utf-8"))
                if isinstance(bak_data, dict):
                    bak_data.setdefault("entries", {})
                    return bak_data
                raise ValueError(f"seen backup {bak} is not a valid JSON dict")
            except Exception as bak_err:
                raise ValueError(f"Failed to parse {p} ({primary_err}) and backup {bak} ({bak_err})")
        raise ValueError(f"Failed to parse {p}: {primary_err}")


def save_seen(data: dict, path: Path | None = None) -> None:
    p = path or seen_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    bak = seen_bak_path(p)
    if p.exists():
        try:
            json.loads(p.read_text(encoding="utf-8"))
            tmp_bak = bak.with_name(bak.name + ".tmp")
            shutil.copy2(p, tmp_bak)
            os.replace(tmp_bak, bak)
        except Exception:
            pass
    write_json_atomic(p, data)


def find_entry(entry_id: str) -> Path | None:
    for p in (ROOT / "library").rglob("entry.md"):
        fm, _ = read_entry(p)
        if fm.get("id") == entry_id or p.parent.name == entry_id:
            return p
    return None


def strip_leading_partial_banner(text: str) -> str:
    """Drop a leading ``> ⚠️ **部分抓取** ...`` blockquote banner from body text."""
    if not text:
        return text
    lines = text.splitlines()
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines):
        return text
    first = lines[idx].lstrip()
    if not first.startswith(">"):
        return text
    if "部分抓取" not in first and "⚠️" not in first:
        return text
    end = idx
    while end < len(lines) and lines[end].lstrip().startswith(">"):
        end += 1
    return "\n".join(lines[end:]).strip()


def fulltext_section(body: str) -> str | None:
    """Return everything after the ``## 全文`` heading (None when absent)."""
    m = re.search(r"##\s*全文\s*\n(.*)", body or "", re.S)
    return m.group(1) if m else None


def entry_char_count(fm: dict, body: str) -> int:
    section = fulltext_section(body)
    text = section if section is not None else (body or "")
    text = strip_leading_partial_banner(text)
    return len(text.strip())


def compute_content_hash(content_or_body: str) -> str:
    """Deterministic ``sha256:<hex>`` of an article's body text.

    Accepts either a full entry body (``## 全文`` is used when present) or a raw
    content string; partial-extraction banners are stripped and Markdown image
    targets are normalized so the same article hashes identically before and
    after image localization or partial-banner updates.
    """
    text = content_or_body or ""
    section = fulltext_section(text)
    if section is not None:
        text = section
    text = strip_leading_partial_banner(text).strip()
    text = re.sub(r"!\[([^\]]*)\]\([^)\s]+(?:\s+\"[^\"]*\")?\)", r"![\1](IMAGE)", text)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_ATX_H1_RE = re.compile(r"^ {0,3}#[ \t]+(.*?)\s*#*$")


def _fence_mask(lines: list[str]) -> list[bool]:
    """True for every line that is inside (or is part of) a fenced code block."""
    mask: list[bool] = []
    fence_char: str | None = None
    fence_len = 0
    for line in lines:
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            char = marker[0]
            if fence_char is None:
                fence_char = char
                fence_len = len(marker)
            elif char == fence_char and len(marker) >= fence_len:
                fence_char = None
                fence_len = 0
            mask.append(True)
            continue
        mask.append(fence_char is not None)
    return mask


def normalize_body_headings(content: str, titles_to_strip: list[str] | None = None) -> str:
    """Strip a duplicated leading H1 and demote remaining body H1s to H2.

    Only ATX headings outside fenced code blocks are touched, so ``# comments``
    in shell/python snippets survive.  The first non-empty line is removed when
    it is an ``# H1`` matching one of ``titles_to_strip`` (case/space-insensitive).
    """
    if not content:
        return content

    strip_titles = {(t or "").strip().casefold() for t in (titles_to_strip or []) if (t or "").strip()}
    lines = content.splitlines()
    in_fence = _fence_mask(lines)

    remove: set[int] = set()
    if strip_titles:
        for index, line in enumerate(lines):
            if in_fence[index] or not line.strip():
                continue
            heading = _ATX_H1_RE.match(line)
            if heading and heading.group(1).strip().casefold() in strip_titles:
                remove.add(index)
            break

    out: list[str] = []
    for index, line in enumerate(lines):
        if index in remove:
            continue
        if not in_fence[index]:
            heading = _ATX_H1_RE.match(line)
            if heading:
                line = f"## {heading.group(1).strip()}"
        out.append(line)

    result = "\n".join(out)
    if remove:
        result = result.lstrip("\n")
    if content.endswith("\n"):
        result += "\n"
    return result


def summary_section(body: str) -> str:
    m = re.search(r"##\s*摘要[^\n]*\n(.*?)(?=\n##\s|\Z)", body, re.S)
    return (m.group(1).strip() if m else "")


def atx_h1_lines(body: str) -> list[tuple[int, str]]:
    """Return (line_index, text) for ATX H1s outside fenced code blocks.

    Fenced code blocks (``` or ~~~) are skipped so shell/Python comments that
    start with ``#`` are not mistaken for Markdown headings.
    """
    result: list[tuple[int, str]] = []
    fence_char: str | None = None
    fence_len = 0

    for index, line in enumerate(body.splitlines()):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            marker = fence.group(1)
            char = marker[0]
            if fence_char is None:
                fence_char = char
                fence_len = len(marker)
            elif char == fence_char and len(marker) >= fence_len:
                fence_char = None
                fence_len = 0
            continue

        if fence_char is not None:
            continue

        heading = re.match(r"^ {0,3}#[ \t]+(.*?)\s*#*$", line)
        if heading:
            result.append((index, heading.group(1).strip()))

    return result


def replace_summary(body: str, summary: str) -> str:
    summary = summary.strip()
    if re.search(r"##\s*摘要[^\n]*\n", body):
        return re.sub(
            r"(##\s*摘要[^\n]*\n)(.*?)(?=\n##\s|\Z)",
            lambda m: m.group(1) + "\n" + summary + "\n",
            body,
            count=1,
            flags=re.S,
        )
    return body.rstrip() + "\n\n## 摘要\n\n" + summary + "\n"
