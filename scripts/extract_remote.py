#!/usr/bin/env python3
"""Project-owned Exa MCP text acquisition with an explicit ``maxCharacters``.

The keyless Exa MCP endpoint defaults to 3000 characters.  Passing only
``urls`` therefore silently truncates long pages.  This module always sends an
explicit limit, records the raw JSON-RPC/SSE response plus extraction metadata,
and never declares an article complete: ``completeness`` is always
``unverified`` so a human/reviewer must check section endings.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import requests

from common import ROOT, assert_public_url, iso, scratch_root, write_json_atomic

EXA_MCP_ENDPOINT = "https://mcp.exa.ai/mcp"
PROVIDER = "exa_mcp"
TOOL = "web_fetch_exa"
DEFAULT_MAX_CHARACTERS = 50000
MIN_MAX_CHARACTERS = 1
MAX_MAX_CHARACTERS = 200000
DEFAULT_TIMEOUT = 90
DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
CONTENT_FILE = "content.md"
RAW_FILE = "raw.sse.txt"
METADATA_FILE = "metadata.json"


class _TooLarge(ValueError):
    pass


def build_payload(urls: list[str], max_characters: int) -> dict[str, Any]:
    """Build the fixed JSON-RPC tools/call payload with an explicit limit."""
    if not isinstance(urls, list) or not urls or not all(isinstance(u, str) and u.strip() for u in urls):
        raise ValueError("urls must be a non-empty list of URL strings")
    if isinstance(max_characters, bool) or not isinstance(max_characters, int):
        raise ValueError("maxCharacters must be an integer")
    if not (MIN_MAX_CHARACTERS <= max_characters <= MAX_MAX_CHARACTERS):
        raise ValueError(
            f"maxCharacters must be between {MIN_MAX_CHARACTERS} and {MAX_MAX_CHARACTERS}"
        )
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": TOOL,
            "arguments": {"urls": [u.strip() for u in urls], "maxCharacters": max_characters},
        },
    }


def _decode(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return raw or ""


def _sse_messages(text: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    data_lines: list[str] = []

    def flush() -> None:
        if not data_lines:
            return
        payload_text = "\n".join(data_lines)
        data_lines.clear()
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return
        if isinstance(payload, dict):
            messages.append(payload)

    for line in text.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif not line.strip():
            flush()
    flush()
    return messages


def _result_text(result: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    blocks = result.get("content") or []
    texts: list[str] = []
    meta: dict[str, Any] = {}
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") in (None, "text") and isinstance(block.get("text"), str):
                texts.append(block["text"])
            if isinstance(block.get("_meta"), dict) and not meta:
                meta = block["_meta"]
    return "\n".join(texts), meta


def parse_mcp_response(raw: bytes | str) -> dict[str, Any]:
    """Parse plain JSON or SSE; surface JSON-RPC errors, isError and empty text."""
    text = _decode(raw)
    if not text.strip():
        return {"ok": False, "error": {"code": "invalid_response", "message": "empty HTTP body"}}

    messages: list[dict[str, Any]] = []
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
            if isinstance(payload, dict):
                messages = [payload]
        except json.JSONDecodeError:
            messages = []
    if not messages:
        messages = _sse_messages(text)

    payload = next((m for m in messages if "result" in m or "error" in m), None)
    if payload is None:
        return {
            "ok": False,
            "error": {"code": "invalid_response", "message": "no JSON-RPC result in response"},
        }

    if "error" in payload:
        error = payload.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else str(error)
        return {
            "ok": False,
            "error": {
                "code": "jsonrpc_error",
                "message": str(message or "JSON-RPC error"),
                "details": error,
            },
        }

    result = payload.get("result") or {}
    content, meta = _result_text(result if isinstance(result, dict) else {})
    if isinstance(result, dict) and result.get("isError"):
        return {
            "ok": False,
            "error": {
                "code": "upstream_is_error",
                "message": content.strip() or "upstream returned isError",
            },
            "meta": meta,
        }
    if not content.strip():
        return {
            "ok": False,
            "error": {"code": "empty_content", "message": "upstream returned no text content"},
            "meta": meta,
        }
    return {"ok": True, "content": content, "meta": meta}


def _read_limited(response, max_bytes: int) -> bytes:
    headers = getattr(response, "headers", {}) or {}
    length = headers.get("Content-Length")
    if length and str(length).strip().isdigit() and int(str(length).strip()) > max_bytes:
        raise _TooLarge(f"response Content-Length {length} exceeds {max_bytes} bytes")
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise _TooLarge(f"response body exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _post_mcp(
    payload: dict[str, Any],
    timeout: int = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> tuple[int, bytes]:
    """POST to the fixed Exa endpoint; bounded time and response size."""
    session = requests.Session()
    session.trust_env = False
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    response = session.post(
        EXA_MCP_ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        timeout=(min(10, timeout), timeout),
        allow_redirects=False,
        stream=True,
    )
    try:
        if 300 <= response.status_code < 400:
            raise ValueError("upstream redirect refused")
        response.raise_for_status()
        body = _read_limited(response, max_bytes)
        return response.status_code, body
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def validate_output_dir(value: str | Path) -> Path:
    """Only scratch or the project's own ``.state`` may receive probe output.

    A path inside the project root is restricted to ``.state`` first (mirrors
    ``common.is_allowed_file_path``); only paths outside the project fall back
    to the general scratch allowlist.  This matters when the whole project has
    been copied into scratch: the copy's ``library/`` must still be refused.
    """
    path = Path(value).expanduser()
    if not path.is_absolute():
        if path.parts and path.parts[0] == ".state":
            path = ROOT / path
        else:
            path = Path.cwd() / path
    path = path.resolve()
    if path.exists() and not path.is_dir():
        raise ValueError(f"output path is not a directory: {path}")

    project_root = Path(ROOT).resolve()
    try:
        path.relative_to(project_root)
        inside_project = True
    except ValueError:
        inside_project = False

    if inside_project:
        try:
            path.relative_to((project_root / ".state").resolve())
            return path
        except ValueError:
            raise ValueError(
                f"output dir inside the project must be under {project_root / '.state'}: {path}"
            )

    scratch = scratch_root()
    try:
        path.relative_to(scratch)
        return path
    except ValueError:
        pass
    raise ValueError(f"output dir must be under {scratch} or {ROOT / '.state'}: {path}")


def _base_metadata(requested_url: str, max_characters: int) -> dict[str, Any]:
    return {
        "schema": 1,
        "provider": PROVIDER,
        "tool": TOOL,
        "endpoint": EXA_MCP_ENDPOINT,
        "requested_url": requested_url,
        "fetched_at": iso(),
        "max_characters": max_characters,
        "content_chars": 0,
        "limit_reached": False,
        "completeness": "unverified",
        "archive_status": "not_archived",
    }


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _committed_metadata(out: Path) -> dict[str, Any] | None:
    """Return the metadata of a complete successful extraction, else ``None``.

    Success metadata is the commit marker: it is written last and carries
    ``fetch_status: ok`` plus content/raw hashes.
    """
    path = out / METADATA_FILE
    if not path.exists():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    if meta.get("fetch_status") == "ok":
        return meta
    if "content_sha256" in meta and "error_code" not in meta:
        return meta
    return None


def _has_committed_output(out: Path) -> bool:
    return _committed_metadata(out) is not None


def _quarantine_target(out: Path) -> Path:
    qroot = out / ".quarantine"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    counter = 1
    while True:
        suffix = "" if counter == 1 else f"-{counter}"
        target = qroot / f"{stamp}{suffix}"
        counter += 1
        try:
            target.mkdir(parents=True)
            return target
        except FileExistsError:
            continue


def _commit_outputs(out: Path, content: str, raw: bytes, metadata: dict[str, Any]) -> None:
    """Publish content + raw + metadata as one staged generation.

    All three files are written into a staging directory first; the previous
    successful generation (if any) is copied into ``.quarantine/``; then files
    are ``os.replace``d into place with metadata last as the commit marker.  A
    failure before the metadata replace restores the previous version, so a
    half-written generation is never mistaken for a complete one.
    """
    staging = Path(tempfile.mkdtemp(dir=str(out), prefix=".staging-"))
    backup: Path | None = None
    replaced: list[str] = []
    try:
        (staging / CONTENT_FILE).write_text(content, encoding="utf-8")
        (staging / RAW_FILE).write_bytes(raw)
        write_json_atomic(staging / METADATA_FILE, metadata)
        if _has_committed_output(out):
            backup = _quarantine_target(out)
            for name in (CONTENT_FILE, RAW_FILE, METADATA_FILE):
                src = out / name
                if src.exists():
                    shutil.copy2(src, backup / name)
        try:
            for name in (RAW_FILE, CONTENT_FILE, METADATA_FILE):
                src = staging / name
                if not src.exists():
                    continue
                os.replace(src, out / name)
                replaced.append(name)
        except Exception:
            if backup is not None:
                for name in replaced:
                    saved = backup / name
                    if saved.exists():
                        os.replace(saved, out / name)
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _write_error(out: Path, result: dict[str, Any], raw: bytes | None = None) -> dict[str, Any]:
    meta = dict(result.get("metadata") or {})
    meta.setdefault("schema", 1)
    meta.setdefault("provider", PROVIDER)
    meta.setdefault("tool", TOOL)
    meta.setdefault("endpoint", EXA_MCP_ENDPOINT)
    meta.setdefault("requested_url", result.get("requested_url", ""))
    meta.setdefault("fetched_at", iso())
    meta.setdefault("max_characters", result.get("max_characters", DEFAULT_MAX_CHARACTERS))
    meta.setdefault("content_chars", 0)
    meta.setdefault("limit_reached", False)
    meta.setdefault("completeness", "unverified")
    meta.setdefault("archive_status", "not_archived")
    meta["fetch_status"] = "error"
    meta["error_code"] = result.get("error_code", "error")
    meta["error"] = result.get("error", "")
    meta["output_dir"] = str(out)
    meta["metadata_file"] = METADATA_FILE
    if _has_committed_output(out):
        # Never overwrite a complete generation with an error record.
        result["metadata"] = meta
        result["preserved_previous"] = True
        return result
    if raw is not None:
        _write_bytes_atomic(out / RAW_FILE, raw)
        meta["raw_file"] = RAW_FILE
        meta["raw_sha256"] = _sha256(raw)
        meta["raw_bytes"] = len(raw)
    write_json_atomic(out / METADATA_FILE, meta)
    result["metadata"] = meta
    result["metadata_file"] = str(out / METADATA_FILE)
    return result


def extract_url(
    url: str,
    *,
    max_characters: int = DEFAULT_MAX_CHARACTERS,
    out_dir: str | Path,
    timeout: int = DEFAULT_TIMEOUT,
    force: bool = False,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    post: Callable[..., tuple[int, bytes]] | None = None,
) -> dict[str, Any]:
    """Fetch one URL through Exa MCP and save raw + content + metadata.

    ``completeness`` is always ``unverified``: a large body, a body below the
    requested limit, or a natural final period is not proof that the article is
    complete.  Callers must perform section/ending review.
    """
    requested_url = (url or "").strip()
    out = validate_output_dir(out_dir)
    started = time.monotonic()

    result: dict[str, Any] = {
        "fetch_status": "error",
        "requested_url": requested_url,
        "provider": PROVIDER,
        "tool": TOOL,
        "endpoint": EXA_MCP_ENDPOINT,
        "max_characters": max_characters,
        "content_chars": 0,
        "limit_reached": False,
        "completeness": "unverified",
        "archive_status": "not_archived",
    }
    metadata = _base_metadata(requested_url, max_characters)
    result["metadata"] = metadata

    # Refuse to touch an existing output before any argument/URL validation can
    # fail: a bad invocation must not overwrite a previous good extraction.
    if not force and any((out / name).exists() for name in (CONTENT_FILE, RAW_FILE, METADATA_FILE)):
        result.update(
            error_code="output_exists",
            error=f"output files already exist in {out}; pass --force to overwrite",
        )
        result.pop("metadata", None)
        return result

    out.mkdir(parents=True, exist_ok=True)

    if not requested_url:
        result.update(error_code="invalid_url", error="URL cannot be empty")
        return _write_error(out, result)

    try:
        payload = build_payload([requested_url], max_characters)
    except ValueError as e:
        result.update(error_code="invalid_argument", error=str(e))
        return _write_error(out, result)

    try:
        assert_public_url(requested_url)
    except Exception as e:
        result.update(error_code="blocked_url", error=f"URL blocked by SSRF check: {e}")
        return _write_error(out, result)

    post_fn = post or _post_mcp
    try:
        status, raw = post_fn(payload, timeout=timeout, max_bytes=max_response_bytes)
    except requests.Timeout as e:
        result.update(error_code="timeout", error=f"upstream timeout: {e}")
        return _write_error(out, result)
    except _TooLarge as e:
        result.update(error_code="response_too_large", error=str(e))
        return _write_error(out, result)
    except requests.RequestException as e:
        result.update(error_code="network_error", error=f"{type(e).__name__}: {e}")
        return _write_error(out, result)
    except ValueError as e:
        result.update(error_code="upstream_error", error=str(e))
        return _write_error(out, result)

    raw = bytes(raw)
    if len(raw) > max_response_bytes:
        result.update(
            error_code="response_too_large",
            error=f"response body {len(raw)} exceeds {max_response_bytes} bytes",
        )
        return _write_error(out, result, raw)
    if status != 200:
        result.update(
            error_code="http_error",
            error=f"upstream HTTP {status}",
            http_status=status,
            metadata={**metadata, "http_status": status},
        )
        return _write_error(out, result, raw)

    parsed = parse_mcp_response(raw)
    if not parsed.get("ok"):
        error = parsed.get("error") or {}
        result.update(
            error_code=error.get("code", "error"),
            error=error.get("message", "upstream extraction failed"),
            http_status=status,
            metadata={**metadata, "http_status": status, "upstream_meta": parsed.get("meta") or {}},
        )
        return _write_error(out, result, raw)

    content = parsed["content"]
    content_chars = len(content)
    limit_reached = content_chars >= int(max_characters)
    warnings: list[str] = [
        "completeness is unverified; a body below maxCharacters may still be truncated upstream"
    ]
    if limit_reached:
        warnings.append(
            "upstream text reached the requested maxCharacters; completeness is still unverified"
        )
    parsed_meta = parsed.get("meta") or {}

    content_bytes = content.encode("utf-8")

    metadata.update(
        {
            "fetched_at": iso(),
            "fetch_status": "ok",  # commit marker: written last, after content+raw
            "http_status": status,
            "content_chars": content_chars,
            "content_bytes": len(content_bytes),
            "content_sha256": _sha256(content_bytes),
            "raw_sha256": _sha256(raw),
            "raw_bytes": len(raw),
            "limit_reached": limit_reached,
            "completeness": "unverified",
            "archive_status": "not_archived",
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "upstream_meta": parsed_meta,
            "warnings": warnings,
            "output_dir": str(out),
            "content_file": CONTENT_FILE,
            "raw_file": RAW_FILE,
            "metadata_file": METADATA_FILE,
        }
    )

    try:
        _commit_outputs(out, content, raw, metadata)
    except Exception as e:
        result.update(error_code="write_error", error=f"failed to commit output files: {e}")
        if _has_committed_output(out):
            result["metadata"] = metadata
            result["preserved_previous"] = True
            return result
        return _write_error(out, result, raw)

    result.update(
        {
            "fetch_status": "ok",
            "content_chars": content_chars,
            "limit_reached": limit_reached,
            "completeness": "unverified",
            "archive_status": "not_archived",
            "http_status": status,
            "warnings": warnings,
            "content_file": str(out / CONTENT_FILE),
            "raw_file": str(out / RAW_FILE),
            "metadata_file": str(out / METADATA_FILE),
            "metadata": metadata,
        }
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--out-dir", required=True, help="scratch or project .state directory")
    parser.add_argument(
        "--max-characters",
        type=int,
        default=DEFAULT_MAX_CHARACTERS,
        help="explicit upstream maxCharacters (default 50000; use 100000 for long pages)",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--force", action="store_true", help="overwrite existing output files")
    args = parser.parse_args(argv)

    try:
        result = extract_url(
            args.url,
            max_characters=args.max_characters,
            out_dir=args.out_dir,
            timeout=args.timeout,
            force=args.force,
        )
    except ValueError as e:
        print(json.dumps({"fetch_status": "error", "error_code": "invalid_argument", "error": str(e)}, ensure_ascii=False, indent=2))
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("fetch_status") == "ok":
        return 0
    return {
        "output_exists": 5,
        "blocked_url": 2,
        "invalid_argument": 2,
        "invalid_url": 2,
        "empty_content": 4,
    }.get(result.get("error_code", ""), 3)


if __name__ == "__main__":
    raise SystemExit(main())
