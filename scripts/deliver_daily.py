#!/usr/bin/env python3
"""Cron dual-channel delivery: full digest by email, ≤200-char summary on Weixin.

Design contract: ``docs/plans/phase5-dual-delivery.md``.

Two *independent* channels, no cross-fallback:

* email carries the full digest, or a full failure diagnostic when the run did
  not pass the gate, to ``CA_EMAIL_TO`` (environment or env file);
* the Weixin chat target from ``CA_WEIXIN_TARGET`` / ``WEIXIN_HOME_CHANNEL``
  carries a hard-capped (``len(text) <= 200``) execution summary.  Both
  channels stay disabled until a target is configured.

A Weixin failure never triggers a second "sorry" email, and an email failure
never pushes the long digest into Weixin. Each channel is recorded separately in
``daily-gate.json["delivery"]`` so a re-run (without ``--force``) only re-sends
the channel that actually failed.

Credentials are read from ``CA_HERMES_ENV_FILE`` (default: ``.env`` in the
archive root) and are never written to disk, stdout, stderr, JSON previews or
the log.
"""
from __future__ import annotations

import argparse
import csv
import html as html_lib
import json
import os
import re
import shutil
import smtplib
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Callable, Iterable, Sequence
from urllib.parse import urlsplit

from dateutil import parser as dateparser

from common import ROOT, TZ, dedupe_key, iso, now, state_lock, write_json_atomic

GATE_NAME = "daily-gate.json"
FETCH_NAME = "latest-fetch.json"

WEIXIN_MAX_CHARS = 200
# Empty by design: a delivery target must be configured explicitly, so an
# unconfigured instance fails visibly instead of sending to a hard-coded address.
DEFAULT_EMAIL_TO = ""
DEFAULT_WEIXIN_HOME = ""
DEFAULT_ENV_FILE = str(ROOT / ".env")
DEFAULT_DELIVERY_LOG = str(ROOT / ".state" / "logs" / "content_archive_delivery.log")
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "[::1]"}

SEND_TIMEOUT_SECONDS = 90
WEIXIN_MAX_ATTEMPTS = 2
# Only a transient rate limit is worth a second attempt. A broken session or a
# bad target cannot fix itself, so sleeping 35s there is pure latency.
TRANSIENT_MARKERS = ("rate limited", "cooldown active")
NON_RETRYABLE_MARKERS = (
    "session not ready",
    "prepare failed",
    "could not resolve",
    "not configured",
    "usage error",
)

# Markdown markers to drop for the email lane: the plain part must never arrive
# with literal ``**`` around headings.
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
_MD_STRIKE_RE = re.compile(r"~~(.+?)~~", re.S)
_MD_CODE_RE = re.compile(r"`([^`]+)`")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)\s]+\)")
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_MD_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.M)

_URL_RE = re.compile(r"https?://[^\s<>\"）】]+")
_ERR_STEP_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9_\- ]{0,40}?)\s+failed(?:\s*\(exit\s*-?\d+\))?\s*:\s*(.*)$",
    re.S,
)


# --------------------------------------------------------------- small helpers


def gate_path() -> Path:
    return ROOT / ".state" / "runs" / GATE_NAME


def fetch_path() -> Path:
    return ROOT / ".state" / "runs" / FETCH_NAME


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1].rstrip() + "…"


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _enforce_cap(text: str, max_chars: int) -> str:
    """Absolute guard: the returned string is never longer than ``max_chars``."""
    if max_chars <= 0:
        return ""
    return text if len(text) <= max_chars else text[:max_chars]


def _int_or(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = dateparser.parse(str(value))
    except Exception:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _read_json(path: Path) -> tuple[dict | None, str]:
    if not path.exists():
        return None, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # unreadable / corrupt gate
        return None, _one_line(f"{path.name} parse error: {exc}")[:300]
    if not isinstance(data, dict):
        return None, _one_line(f"{path.name} is not a JSON object")[:300]
    return data, ""


# ------------------------------------------------------------- configuration


def read_env_file(path: str | Path | None = None) -> dict[str, str]:
    """Parse a ``KEY=value`` env file. Never logs what it read."""
    target = Path(path or os.environ.get("CA_HERMES_ENV_FILE") or DEFAULT_ENV_FILE)
    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in raw.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("export "):
            entry = entry[len("export ") :].strip()
        if "=" not in entry:
            continue
        key, value = entry.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def smtp_settings(env: dict[str, str]) -> dict:
    port = _int_or(env.get("EMAIL_SMTP_PORT"), 587) or 587
    security = (env.get("EMAIL_SMTP_SECURITY") or "").strip().lower()
    if not security:
        security = "tls" if port == 465 else "starttls"
    if security in ("ssl", "smtps", "implicit-tls", "implicit_tls"):
        security = "tls"
    if security in ("none", "plain", "off", "no", "plaintext"):
        security = "none"
    if security not in ("tls", "starttls", "none"):
        security = "starttls"
    return {
        "host": (env.get("EMAIL_SMTP_HOST") or "").strip(),
        "port": port,
        "security": security,
        "username": (env.get("EMAIL_ADDRESS") or "").strip(),
        "password": env.get("EMAIL_PASSWORD") or "",
        "mail_from": (env.get("EMAIL_ADDRESS") or "").strip(),
    }


def default_email_to(env: dict[str, str]) -> str:
    for candidate in (os.environ.get("CA_EMAIL_TO"), env.get("CA_EMAIL_TO")):
        if candidate and candidate.strip():
            return candidate.strip()
    return DEFAULT_EMAIL_TO.strip()


def default_weixin_target(env: dict[str, str]) -> str:
    override = (os.environ.get("CA_WEIXIN_TARGET") or "").strip()
    if override:
        return override
    home = (env.get("WEIXIN_HOME_CHANNEL") or "").strip() or DEFAULT_WEIXIN_HOME
    return ("weixin:" + home) if home else ""


def resolve_proxy(host: str) -> str:
    """Proxy for the SMTP hop: ``CA_SMTP_PROXY`` wins (empty string = direct)."""
    if (host or "").strip().lower() in LOOPBACK_HOSTS:
        return ""
    explicit = os.environ.get("CA_SMTP_PROXY")
    if explicit is not None:
        return explicit.strip()
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


# ------------------------------------------------------- Weixin text builders


def build_weixin_summary(
    date_label: str,
    titles: Sequence[str] | None = None,
    *,
    new_total: int | None = None,
    skipped: int = 0,
    not_ok_labels: Sequence[str] | None = None,
    pending: int = 0,
    max_chars: int = WEIXIN_MAX_CHARS,
    max_titles: int = 4,
) -> str:
    """Success-state Weixin short message. ``len(result) <= max_chars`` always.

    Titles are trimmed from 4 down to 0 and each one is clipped, so a run with
    ten long bilingual headlines still fits the 200-character budget.
    """
    # A pathological label must not eat the whole budget: keep the edges intact.
    label = _clip(_one_line(date_label), 32) or now().strftime("%m-%d")
    clean_titles = [t for t in (_one_line(x) for x in (titles or [])) if t]
    total = _int_or(new_total, len(clean_titles)) if new_total is not None else len(clean_titles)
    total = max(total, 0)
    labels = [x for x in (_one_line(v) for v in (not_ok_labels or [])) if x]
    skipped = max(_int_or(skipped, 0), 0)
    pending = max(_int_or(pending, 0), 0)

    header = f"📚 收藏夹日报 {label}"
    stats = f"新增 {total} 篇｜跳过 {skipped}｜异常源 {len(labels)}"
    if pending:
        stats += f"｜待补 {pending}"
    footer = "完整日报已发送至邮箱。"
    vendor = ("异常源：" + "、".join(labels)) if labels else ""
    shown_total = max(total, len(clean_titles))

    def compose(show: int, title_max: int, vendor_max: int) -> str:
        lines = [header, stats]
        if shown_total <= 0 or not clean_titles and total <= 0:
            lines.append(
                "本次无新增（有异常源，不能据此判断无新闻）" if labels else "今日无新增"
            )
        else:
            for index, title in enumerate(clean_titles[: max(show, 0)], 1):
                lines.append(f"{index}. {_clip(title, title_max)}")
            if show < shown_total:
                lines.append(f"…等 {shown_total} 篇")
        if vendor and vendor_max > 0:
            lines.append(_clip(vendor, vendor_max))
        lines.append(footer)
        return "\n".join(lines)

    top = min(max_titles, len(clean_titles))
    for show in range(top, -1, -1):
        for title_max in (80, 60, 44, 32, 24, 18, 14, 10, 6, 2):
            for vendor_max in (60, 40, 24, 12):
                text = compose(show, title_max, vendor_max)
                if len(text) <= max_chars:
                    return _enforce_cap(text, max_chars)

    # Degenerate fallback (very small budget or absurdly long stat labels).
    for stats_max in (0, 40, 28, 18, 10):
        stats_line = stats if stats_max == 0 else _clip(stats, stats_max)
        for body in ("", "…"):
            pieces = [header, stats_line] + ([body] if body else []) + [footer]
            text = "\n".join(pieces)
            if len(text) <= max_chars:
                return _enforce_cap(text, max_chars)
    bare = "\n".join([header, footer])
    if len(bare) <= max_chars:
        return _enforce_cap(bare, max_chars)
    return _enforce_cap(bare, max_chars)


def build_weixin_failure_summary(
    date_label: str,
    *,
    step: str = "",
    reason: str = "",
    max_chars: int = WEIXIN_MAX_CHARS,
) -> str:
    """Failure-state Weixin short message. ``len(result) <= max_chars`` always.

    The original error may be a multi-line traceback thousands of characters
    long; it is flattened to a single line and clipped until the whole message
    fits.
    """
    # A pathological label must not eat the whole budget: keep the edges intact.
    label = _clip(_one_line(date_label), 32) or now().strftime("%m-%d")
    header = f"⚠️ 收藏夹日报异常 {label}"
    footer = "详细诊断已发送至邮箱。"
    stage_line = f"阶段：{_one_line(step)}" if _one_line(step) else ""
    reason_one = _one_line(reason) or "未知错误"

    for budget in (0, 120, 90, 60, 40, 24, 12, 6):
        body = reason_one if budget == 0 else _clip(reason_one, budget)
        lines = [header]
        if stage_line:
            lines.append(_clip(stage_line, max(max_chars - len(header) - len(footer) - 8, 6)))
        lines.append(f"原因：{body}")
        lines.append(footer)
        text = "\n".join(lines)
        if len(text) <= max_chars:
            return _enforce_cap(text, max_chars)

    tail = "\n".join([header, footer])
    if len(tail) <= max_chars:
        return _enforce_cap(tail, max_chars)
    return _enforce_cap(f"{header}\n{footer}", max_chars)


# ------------------------------------------------------------- digest content


def _plain_text(markdown: str) -> str:
    """Strip Markdown emphasis markers so ``text/plain`` reads cleanly."""
    text = _MD_BOLD_RE.sub(r"\1", markdown or "")
    text = _MD_STRIKE_RE.sub(r"\1", text)
    text = _MD_CODE_RE.sub(r"\1", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)
    text = _MD_HEADING_RE.sub("", text)
    return text.strip("\n")


def _linkify(escaped: str) -> str:
    return _URL_RE.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', escaped)


def plain_to_html(text: str, *, title: str = "", accent: str = "#2563eb") -> str:
    """Render digest plain text into a readable HTML body with clickable links."""
    parts: list[str] = []
    bullets: list[str] = []

    def flush() -> None:
        if bullets:
            parts.append("<ul>" + "".join(bullets) + "</ul>")
            bullets.clear()

    for raw in (text or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            flush()
            continue
        if stripped.startswith("- ") or stripped.startswith("* "):
            bullets.append(f"<li>{_linkify(html_lib.escape(stripped[2:].strip()))}</li>")
            continue
        flush()
        if stripped.startswith("【") and stripped.endswith("】"):
            parts.append(f"<h3>{html_lib.escape(stripped.strip('【】'))}</h3>")
            continue
        if stripped.startswith("#"):
            parts.append(f"<h3>{html_lib.escape(stripped.lstrip('#').strip())}</h3>")
            continue
        if stripped.startswith(">"):
            parts.append(
                f'<blockquote>{_linkify(html_lib.escape(stripped.lstrip("> ").strip()))}</blockquote>'
            )
            continue
        numbered = re.match(r"^(\d+)\.\s+(.*)$", stripped)
        if numbered:
            parts.append(
                f'<p class="item"><span class="num">{numbered.group(1)}.</span> '
                f"{_linkify(html_lib.escape(numbered.group(2)))}</p>"
            )
            continue
        css = "meta" if raw.startswith("  ") else "line"
        parts.append(f'<p class="{css}">{_linkify(html_lib.escape(stripped))}</p>')

    flush()
    body = "\n".join(parts)
    safe_title = html_lib.escape(title or "内容收藏夹日报")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title>
<style>
  body {{ margin:0; padding:24px 14px; background:#f5f6f8;
         font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",Arial,sans-serif;
         color:#1f2430; line-height:1.7; }}
  .wrap {{ max-width:720px; margin:0 auto; background:#fff; border-radius:12px;
           padding:26px 30px; box-shadow:0 1px 3px rgba(16,24,40,.08); }}
  h1 {{ font-size:20px; margin:0 0 18px; padding-bottom:12px;
        border-bottom:2px solid {accent}; color:{accent}; }}
  h3 {{ font-size:16px; margin:22px 0 8px; color:#111827; }}
  p {{ margin:6px 0; font-size:15px; }}
  p.item {{ margin:10px 0 2px; font-weight:600; }}
  p.meta {{ margin:0 0 10px; color:#6b7280; font-size:13px; }}
  span.num {{ color:{accent}; }}
  ul {{ margin:6px 0 12px 20px; padding:0; }}
  li {{ margin:4px 0; font-size:15px; }}
  blockquote {{ margin:6px 0 0; padding:6px 12px; border-left:3px solid #d1d5db;
                color:#4b5563; font-size:14px; }}
  a {{ color:{accent}; text-decoration:none; word-break:break-all; }}
  .foot {{ margin-top:26px; padding-top:12px; border-top:1px solid #e5e7eb;
           color:#9ca3af; font-size:12px; }}
</style></head>
<body><div class="wrap">
<h1>{safe_title}</h1>
{body}
<p class="foot">由 ContentArchive 自动生成 · Cron 双通道推送</p>
</div></body></html>"""


def _candidate_keys(fetch_data: dict | None) -> set[str] | None:
    if not isinstance(fetch_data, dict) or "candidates" not in fetch_data:
        return None
    keys: set[str] = set()
    for cand in fetch_data.get("candidates") or []:
        if not isinstance(cand, dict):
            continue
        url = str(cand.get("url") or "")
        if url:
            keys.add(dedupe_key(url))
        for extra in cand.get("dedupe_keys") or []:
            if str(extra or ""):
                keys.add(dedupe_key(str(extra)))
    keys.discard("")
    return keys


def load_index_rows() -> list[dict]:
    path = ROOT / "index.csv"
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except Exception as exc:
        print(f"[deliver] index.csv unreadable: {exc}", file=sys.stderr)
        return []


def select_new_rows(fetch_data: dict | None, rows: Iterable[dict] | None = None) -> list[dict]:
    """Entries archived in this very run (same scoping as build_digest.py)."""
    fetch_data = fetch_data if isinstance(fetch_data, dict) else {}
    since_raw = str(fetch_data.get("generated_at") or "").strip()
    if since_raw:
        since_dt = _parse_dt(since_raw)
    else:
        since_dt = now().replace(hour=0, minute=0, second=0, microsecond=0)
        since_raw = since_dt.isoformat(timespec="seconds")
    cand_keys = _candidate_keys(fetch_data)

    selected: list[dict] = []
    for row in rows if rows is not None else load_index_rows():
        archived_raw = row.get("archived") or ""
        archived_dt = _parse_dt(archived_raw)
        if since_dt is not None:
            if archived_dt is None:
                if archived_raw < since_raw:
                    continue
            elif archived_dt < since_dt:
                continue
        if cand_keys is not None and dedupe_key(str(row.get("url") or "")) not in cand_keys:
            continue
        selected.append(row)
    selected.sort(key=lambda r: (r.get("published") or "", r.get("archived") or ""), reverse=True)
    return selected


def build_digest_text() -> tuple[bool, str]:
    """Run ``build_digest.py`` for this run's gate/fetch pair."""
    script = ROOT / "scripts" / "build_digest.py"
    if not script.exists():
        return False, f"build_digest.py not found at {script}"
    cmd = [
        sys.executable,
        str(script),
        "--run", str(fetch_path()),
        "--gate", str(gate_path()),
        "--limit", "10",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except Exception as exc:
        return False, f"build_digest.py failed to run: {exc}"
    if res.returncode != 0:
        return False, (res.stderr.strip() or res.stdout.strip() or f"exit {res.returncode}")
    return True, res.stdout.strip()


def split_failure_error(text: str) -> tuple[str, str]:
    """Split ``"<step> failed: <detail>"`` into (step, detail)."""
    raw = str(text or "").strip()
    match = _ERR_STEP_RE.match(raw)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return "", raw


def _sources(*candidates) -> list[dict]:
    for data in candidates:
        if isinstance(data, dict):
            sources = data.get("sources")
            if isinstance(sources, list) and sources:
                return [s for s in sources if isinstance(s, dict)]
    return []


def _source_line(src: dict) -> str:
    label = str(src.get("label") or src.get("id") or "未知源")
    status = str(src.get("status") or "unknown")
    bits = [status]
    if status == "ok" and src.get("selected") is not None:
        bits.append(f"候选 {src.get('selected')} 篇")
    if src.get("skipped") is not None:
        bits.append(f"跳过 {src.get('skipped')}")
    if src.get("dropped_by_max"):
        bits.append(f"顺延 {src.get('dropped_by_max')} 篇")
    reason = src.get("empty_reason") or src.get("error") or ""
    if status != "ok" and reason:
        bits.append(_one_line(reason)[:120])
    return f"- {label}：" + " · ".join(str(b) for b in bits)


def _failed_candidates(gate_data: dict | None) -> list[dict]:
    if not isinstance(gate_data, dict):
        return []
    raw = gate_data.get("failed_candidates")
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


def build_ok_content(
    *,
    gate_data: dict,
    fetch_data: dict,
    digest_text: str | None = None,
) -> dict:
    date_iso = now().strftime("%Y-%m-%d")
    date_md = now().strftime("%m-%d")
    sources = _sources(fetch_data, gate_data)
    not_ok = [s for s in sources if s.get("status") != "ok"]
    failed = _failed_candidates(gate_data)
    skipped = sum(max(_int_or(s.get("skipped"), 0), 0) for s in sources)
    deferred = max(_int_or(fetch_data.get("global_dropped"), 0), 0)
    pending = len(failed) + deferred

    rows_all = select_new_rows(fetch_data)
    rows = rows_all[:10]
    total_new = len(rows_all)

    body = (digest_text or "").strip()
    digest_source = "provided"
    if not body:
        ok, rendered = build_digest_text()
        if ok and rendered.strip():
            body, digest_source = rendered.strip(), "build_digest.py"
        else:
            digest_source = "fallback"
            fallback = [
                f"📚 内容收藏夹 · {date_iso}",
                f"新增 {total_new} 篇 · 空/失败 {len(not_ok)} 源",
                "",
            ]
            if rows:
                fallback += [f"{i}. {r.get('title', '')}" for i, r in enumerate(rows, 1)]
                fallback += [""]
            elif not_ok:
                fallback += [
                    "本次没有归档新增；存在空/失败源，不能据此判断无新闻。",
                    "",
                ]
            else:
                fallback += ["今日无新增。", ""]
            fallback += [f"日报正文渲染失败：{_one_line(rendered)[:300]}"]
            body = "\n".join(fallback)

    plain = _plain_text(body)
    heading = f"📚 内容收藏夹日报 · {date_iso}"
    subject = f"📚 内容收藏夹日报 · {date_iso}（新增 {total_new} 篇）"
    if not_ok or failed:
        subject += "｜含异常提醒"

    labels = [str(s.get("label") or s.get("id") or "").strip() for s in not_ok]
    weixin_text = build_weixin_summary(
        date_md,
        [r.get("title") or "" for r in rows],
        new_total=total_new,
        skipped=skipped,
        not_ok_labels=[x for x in labels if x],
        pending=pending,
    )

    return {
        "mode": "ok",
        "subject": subject,
        "plain": plain,
        "html": plain_to_html(plain, title=heading),
        "weixin_text": weixin_text,
        "digest_source": digest_source,
        "meta": {
            "date": date_iso,
            "new_count": total_new,
            "displayed_count": len(rows),
            "omitted_count": max(total_new - len(rows), 0),
            "skipped": skipped,
            "not_ok_sources": len(not_ok),
            "pending": pending,
            "failed_candidates": len(failed),
        },
    }


def build_failure_content(
    *,
    gate_data: dict | None,
    fetch_data: dict | None,
    failure_reason: str = "",
    failure_step: str = "",
    gate_error: str = "",
) -> dict:
    date_iso = now().strftime("%Y-%m-%d")
    date_md = now().strftime("%m-%d")

    raw_error = (failure_reason or "").strip()
    if not raw_error and isinstance(gate_data, dict):
        raw_error = str(gate_data.get("error") or "").strip()
    if not raw_error:
        raw_error = (gate_error or "").strip() or "未知错误（gate 缺失或状态非 ok）"

    parsed_step, detail = split_failure_error(raw_error)
    step = (failure_step or "").strip() or parsed_step or "daily gate"
    detail = detail or raw_error
    when = ""
    if isinstance(gate_data, dict):
        when = str(gate_data.get("finished_at") or gate_data.get("started_at") or "").strip()
    when = when or iso()

    lines = [
        "⚠️ 内容收藏夹日报异常",
        f"发生时间：{when}",
        f"失败阶段：{step}",
        "错误详情：",
    ]
    for line in str(detail).splitlines()[:80]:
        lines.append("  " + line.rstrip())
    lines.append("")

    sources = _sources(gate_data, fetch_data)
    if sources:
        lines.append("源状态明细：")
        lines.extend(_source_line(s) for s in sources)
        lines.append("")

    failed = _failed_candidates(gate_data)
    if failed:
        lines.append(f"失败候选（已登记，等待补抓）{len(failed)} 篇：")
        for rec in failed:
            url = rec.get("url") or rec.get("key") or ""
            reason = _one_line(rec.get("reason") or "抓取失败")
            lines.append(f"- {url}：{reason}")
        lines.append("")

    unaccounted = (gate_data or {}).get("unaccounted_candidates") if isinstance(gate_data, dict) else None
    if isinstance(unaccounted, list) and unaccounted:
        lines.append(f"悬空候选（既未归档也未登记失败）{len(unaccounted)} 篇：")
        for cand in unaccounted:
            if not isinstance(cand, dict):
                continue
            title = _one_line(cand.get("title") or "")
            suffix = f"（{title}）" if title else ""
            lines.append(f"- {cand.get('url') or ''}{suffix}")
        lines.append("")

    pending_note = ""
    if isinstance(gate_data, dict):
        pending_note = _one_line(gate_data.get("error") or "")[:120]
    lines.append("本次运行未通过完整性校验，归档结果保持原状，等待下次运行补抓。")
    if pending_note and pending_note not in detail:
        lines.append("")

    plain = _plain_text("\n".join(lines).rstrip())
    heading = f"⚠️ 内容收藏夹日报异常 · {date_iso}"
    return {
        "mode": "failure",
        "subject": heading,
        "plain": plain,
        "html": plain_to_html(plain, title=heading, accent="#dc2626"),
        "weixin_text": build_weixin_failure_summary(date_md, step=step, reason=detail),
        "digest_source": "failure-report",
        "meta": {
            "date": date_iso,
            "step": step,
            "sources": len(sources),
            "failed_candidates": len(_failed_candidates(gate_data)),
        },
    }


# ------------------------------------------------------------- email sending


class _TunnelSMTP(smtplib.SMTP):
    """``smtplib.SMTP`` that adopts an already-open (proxy-tunnelled) socket.

    ``host`` is stored as ``_host`` so that ``starttls()`` verifies the
    certificate against the *mail server* name, never the proxy's address.
    """

    def __init__(self, host: str, port: int, sock: socket.socket, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self._host = host
        self._port = port
        self.sock = sock
        self.file = None
        self.helo_resp = None
        self.ehlo_resp = None
        self.does_esmtp = 0
        # ``SMTP.connect()`` normally consumes the 220 greeting; adopting a
        # socket by hand must do the same or EHLO would read the greeting.
        code, message = self.getreply()
        if code != 220:
            self.close()
            raise smtplib.SMTPConnectError(code, message)


def http_connect_tunnel(proxy_url: str, host: str, port: int, timeout: float = 30.0) -> socket.socket:
    """Open a TCP tunnel to ``host:port`` through an HTTP proxy (CONNECT)."""
    target = proxy_url if "://" in proxy_url else f"http://{proxy_url}"
    parts = urlsplit(target)
    proxy_host = parts.hostname
    proxy_port = parts.port or (443 if parts.scheme == "https" else 8080)
    if not proxy_host:
        raise OSError(f"invalid proxy URL: {proxy_url}")
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        headers = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
        if parts.username:
            import base64

            token = base64.b64encode(
                f"{parts.username}:{parts.password or ''}".encode("utf-8")
            ).decode("ascii")
            auth_header = "Proxy-Authorization: Basic " + token
            headers.append(auth_header)
        sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("latin-1"))
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("proxy closed the connection during CONNECT")
            buffer += chunk
            if len(buffer) > 65536:
                raise OSError("proxy CONNECT response too large")
    except Exception:
        sock.close()
        raise
    status_line = buffer.split(b"\r\n", 1)[0].decode("latin-1", "replace").strip()
    match = re.match(r"HTTP/\d(?:\.\d)?\s+(\d{3})", status_line)
    if not match or match.group(1) != "200":
        sock.close()
        raise OSError(f"proxy CONNECT rejected: {status_line}")
    return sock


def build_email_message(
    *, subject: str, mail_from: str, to_addr: str, plain: str, html: str
) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = mail_from or "content-archive@localhost"
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    domain = (mail_from.split("@")[-1].strip() if "@" in (mail_from or "") else "") or "localhost"
    msg["Message-ID"] = make_msgid(domain=domain)
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    return msg


def send_email_smtp(
    *,
    host: str,
    port: int,
    security: str,
    username: str,
    password: str,
    mail_from: str,
    to_addr: str,
    subject: str,
    plain: str,
    html: str,
    proxy: str = "",
    timeout: float = 30.0,
) -> tuple[bool, str]:
    """Send one message. Returns ``(ok, detail)``; never raises, never logs secrets."""
    if not host:
        return False, "EMAIL_SMTP_HOST is not configured"
    if not to_addr:
        return False, "no recipient configured"

    msg = build_email_message(
        subject=subject, mail_from=mail_from or username, to_addr=to_addr, plain=plain, html=html
    )
    use_proxy = bool((proxy or "").strip()) and host.strip().lower() not in LOOPBACK_HOSTS
    context = ssl.create_default_context()
    client: smtplib.SMTP | None = None
    try:
        if use_proxy:
            raw_sock = http_connect_tunnel(proxy.strip(), host, port, timeout=timeout)
            if security == "tls":
                tls_sock = context.wrap_socket(raw_sock, server_hostname=host)
                client = _TunnelSMTP(host, port, tls_sock, timeout)
            else:
                client = _TunnelSMTP(host, port, raw_sock, timeout)
                client.ehlo()
                if security == "starttls":
                    client.starttls(context=context)
                    client.ehlo()
        elif security == "tls":
            client = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
            client.ehlo()
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
            client.ehlo()
            if security == "starttls":
                client.starttls(context=context)
                client.ehlo()

        if username and password:
            client.login(username, password)
        client.send_message(msg)
        return True, f"sent to {to_addr} via {host}:{port} ({security})"
    except Exception as exc:  # SMTPException, OSError, ssl.SSLError, ...
        return False, _one_line(f"{type(exc).__name__}: {exc}")[:300]
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass


# ------------------------------------------------------------ weixin sending


def hermes_binary() -> str | None:
    override = (os.environ.get("HERMES_BIN") or "").strip()
    if override and Path(override).exists():
        return override
    return shutil.which("hermes")


def _is_transient(detail: str) -> bool:
    lowered = (detail or "").lower()
    return any(marker in lowered for marker in TRANSIENT_MARKERS)


def _send_weixin_once(binary: str, target: str, text: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [binary, "send", "-t", target, "--json", text],
            capture_output=True, text=True, timeout=SEND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, f"send timed out after {SEND_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return False, f"could not run hermes send: {exc}"

    raw = (proc.stdout or "").strip()
    payload = None
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
    if isinstance(payload, dict):
        if payload.get("error"):
            return False, _one_line(str(payload["error"]))[:300]
        if payload.get("success") is False:
            return False, _one_line(str(payload.get("error") or "send reported failure"))[:300]
        if proc.returncode == 0:
            return True, str(payload.get("message_id") or "sent")
    if proc.returncode != 0:
        tail = (proc.stderr or raw or "").strip().replace("\n", " ")[:300]
        return False, f"exit {proc.returncode}: {tail}"
    return True, "sent"


def send_weixin_cli(
    target: str,
    text: str,
    *,
    binary: str | None = None,
    retry_delay: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    send_once: Callable[[str, str, str], tuple[bool, str]] = _send_weixin_once,
) -> tuple[bool, str]:
    """Send the ≤200-char summary through ``hermes send``.

    One retry only, and only for a transient rate limit. Non-transient errors
    (``session not ready``/``prepare failed``/bad target/usage) fail at once.
    """
    if not target:
        return False, "no Weixin target configured"
    binary = binary or hermes_binary()
    if not binary:
        return False, "hermes CLI not found (set HERMES_BIN)"

    text = _enforce_cap(text or "", WEIXIN_MAX_CHARS)
    delay = _int_or(os.environ.get("CA_WEIXIN_RETRY_DELAY", "35"), 35) if retry_delay is None else retry_delay

    ok, detail = send_once(binary, target, text)
    if ok:
        return True, detail
    if not _is_transient(detail) or any(m in (detail or "").lower() for m in NON_RETRYABLE_MARKERS):
        return False, detail
    try:
        sleep(max(float(delay), 0.0))
    except Exception:
        pass
    ok, detail2 = send_once(binary, target, text)
    if ok:
        return True, detail2
    return False, _one_line(f"{detail2} (after 1 retry; first attempt: {detail})")[:300]


# ------------------------------------------------------------ delivery state


def _delivery_state(gate_data: dict | None) -> dict:
    if not isinstance(gate_data, dict):
        return {}
    delivery = gate_data.get("delivery")
    return delivery if isinstance(delivery, dict) else {}


VALID_STATUSES = ("pending", "ok", "failed")


def _archive_status(gate_data: dict | None) -> str:
    """Archive checkpoint status, falling back to the legacy ``status`` field."""
    data = gate_data if isinstance(gate_data, dict) else {}
    explicit = str(data.get("archive_status") or "").strip()
    if explicit in VALID_STATUSES:
        return explicit
    legacy = str(data.get("status") or "").strip()
    if legacy in ("ok", "failed"):
        return legacy
    return "pending"


def _overall_status(gate_data: dict | None) -> str:
    data = gate_data if isinstance(gate_data, dict) else {}
    archive = _archive_status(data)
    if archive in ("failed", "pending"):
        return archive
    delivery = _delivery_state(data)
    states = [delivery.get(channel) for channel in ("email", "weixin")]
    if not any(isinstance(state, dict) and state.get("status") for state in states):
        return "pending"
    return "ok" if all(
        isinstance(state, dict) and state.get("status") == "ok" for state in states
    ) else "failed"


def record_delivery(results: dict, mode: str) -> bool:
    """Merge per-channel results into ``daily-gate.json`` and update overall status."""
    path = gate_path()
    if not path.exists():
        return False
    try:
        with state_lock():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return False
            delivery = data.get("delivery")
            delivery = dict(delivery) if isinstance(delivery, dict) else {}
            delivery.update(results)
            delivery.update(
                {
                    "mode": mode,
                    "updated_at": iso(),
                    "email_status": (results.get("email") or {}).get("status", ""),
                    "weixin_status": (results.get("weixin") or {}).get("status", ""),
                }
            )
            data["delivery"] = delivery
            # Archive stays a separate checkpoint: a delivery failure never
            # rolls it back, but it does drive the overall run status.
            data["archive_status"] = _archive_status(data)
            data["overall_status"] = _overall_status(data)
            write_json_atomic(path, data)
        return True
    except Exception as exc:
        print(f"[deliver] could not record delivery state: {_one_line(exc)[:200]}", file=sys.stderr)
        return False


def log_delivery(message: str) -> None:
    path = Path(os.environ.get("CA_DELIVERY_LOG") or DEFAULT_DELIVERY_LOG)
    line = f"{now().strftime('%Y-%m-%d %H:%M:%S')} {message}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    print(line, file=sys.stderr)


# ------------------------------------------------------------------ delivery


def _deliver_impl(
    *,
    dry_run: bool = False,
    force: bool = False,
    digest_text: str | None = None,
    failure_reason: str = "",
    failure_step: str = "",
) -> int:
    env = read_env_file()
    gate_file = gate_path()
    gate_data, gate_error = _read_json(gate_file)
    fetch_data, fetch_error = _read_json(fetch_path())
    fetch_data = fetch_data or {}

    forced_failure = bool((failure_reason or "").strip())
    if forced_failure:
        mode = "failure"
    elif not isinstance(gate_data, dict):
        mode = "failure"
    else:
        mode = "ok" if _archive_status(gate_data) == "ok" else "failure"

    if mode == "ok":
        content = build_ok_content(gate_data=gate_data or {}, fetch_data=fetch_data, digest_text=digest_text)
    else:
        content = build_failure_content(
            gate_data=gate_data,
            fetch_data=fetch_data,
            failure_reason=failure_reason,
            failure_step=failure_step,
            gate_error=gate_error or fetch_error,
        )

    weixin_text = _enforce_cap(content["weixin_text"], WEIXIN_MAX_CHARS)
    to_addr = default_email_to(env)
    wx_target = default_weixin_target(env)
    settings = smtp_settings(env)

    preview = {
        "mode": content["mode"],
        "email_to": to_addr,
        "email_subject": content["subject"],
        "email_plain_chars": len(content["plain"]),
        "email_html_chars": len(content["html"]),
        "weixin_target": wx_target,
        "weixin_text": weixin_text,
        "weixin_chars": len(weixin_text),
        "weixin_max_chars": WEIXIN_MAX_CHARS,
        "digest_source": content["digest_source"],
        "gate_path": str(gate_file),
        "gate_exists": gate_file.exists(),
        "gate_status": (gate_data or {}).get("status", "") if isinstance(gate_data, dict) else "",
        "archive_status": _archive_status(gate_data),
        "overall_status": _overall_status(gate_data),
        "smtp_host_configured": bool(settings["host"]),
        "smtp_security": settings["security"],
        "smtp_port": settings["port"],
    }
    preview.update(content["meta"])

    if dry_run:
        preview["dry_run"] = True
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0

    prior = _delivery_state(gate_data)
    results: dict[str, dict] = {}

    # ---- channel 1: email (full digest / full diagnostic)
    prior_email = prior.get("email") if isinstance(prior.get("email"), dict) else {}
    if not force and prior_email.get("status") == "ok":
        results["email"] = {
            "status": "ok",
            "detail": "already delivered (skipped)",
            "skipped": True,
            "at": prior_email.get("at") or iso(),
            "to": to_addr,
        }
    else:
        ok, detail = send_email_smtp(
            host=settings["host"],
            port=settings["port"],
            security=settings["security"],
            username=settings["username"],
            password=settings["password"],
            mail_from=settings["mail_from"] or settings["username"],
            to_addr=to_addr,
            subject=content["subject"],
            plain=content["plain"],
            html=content["html"],
            proxy=resolve_proxy(settings["host"]),
        )
        results["email"] = {
            "status": "ok" if ok else "failed",
            "detail": _one_line(detail)[:300],
            "skipped": False,
            "at": iso(),
            "to": to_addr,
            "subject": content["subject"],
            "chars": len(content["plain"]),
        }

    # ---- channel 2: Weixin (≤200-char summary only — never the long body)
    prior_wx = prior.get("weixin") if isinstance(prior.get("weixin"), dict) else {}
    if not force and prior_wx.get("status") == "ok":
        results["weixin"] = {
            "status": "ok",
            "detail": "already delivered (skipped)",
            "skipped": True,
            "at": prior_wx.get("at") or iso(),
            "target": wx_target,
        }
    else:
        ok, detail = send_weixin_cli(wx_target, weixin_text)
        results["weixin"] = {
            "status": "ok" if ok else "failed",
            "detail": _one_line(detail)[:300],
            "skipped": False,
            "at": iso(),
            "target": wx_target,
            "chars": len(weixin_text),
        }

    recorded = record_delivery(results, content["mode"])

    both_ok = all(results[ch].get("status") == "ok" for ch in ("email", "weixin"))
    log_delivery(
        f"[deliver] mode={content['mode']} email={results['email']['status']}"
        f"{' (skipped)' if results['email'].get('skipped') else ''}"
        f" weixin={results['weixin']['status']}"
        f"{' (skipped)' if results['weixin'].get('skipped') else ''}"
        f" weixin_chars={len(weixin_text)} new={content['meta'].get('new_count', '-')}"
        f" state_recorded={recorded}"
    )

    summary = dict(preview)
    summary.pop("weixin_text", None)
    latest, _ = _read_json(gate_path())
    if isinstance(latest, dict):
        summary["archive_status"] = _archive_status(latest)
        summary["overall_status"] = _overall_status(latest)
    summary.update(
        {
            "dry_run": False,
            "force": bool(force),
            "state_recorded": recorded,
            "email": results["email"],
            "weixin": results["weixin"],
            "ok": both_ok,
        }
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if not recorded:
        print(
            "[CRON_FAILURE] delivery result could not be persisted to daily-gate.json",
            file=sys.stderr,
        )
        return 1
    if not both_ok:
        for channel in ("email", "weixin"):
            if results[channel].get("status") != "ok":
                print(
                    f"[CRON_FAILURE] {channel} delivery failed: "
                    f"{results[channel].get('detail', '')}",
                    file=sys.stderr,
                )
        succeeded = [ch for ch in ("email", "weixin") if results[ch].get("status") == "ok"]
        failed = [ch for ch in ("email", "weixin") if results[ch].get("status") != "ok"]
        if succeeded and failed:
            print(
                f"[DELIVERY_PARTIAL_FAILURE] ok={','.join(succeeded)} "
                f"failed={','.join(failed)}",
                file=sys.stderr,
            )
        return 1
    return 0


def _record_crash_failure(detail: str) -> None:
    """Best-effort state write when delivery dies mid-flight."""
    try:
        from daily_gate import record_delivery_failure

        record_delivery_failure(gate_path(), f"delivery crashed: {detail}")
    except Exception:
        pass


def deliver(
    *,
    dry_run: bool = False,
    force: bool = False,
    digest_text: str | None = None,
    failure_reason: str = "",
    failure_step: str = "",
) -> int:
    """Run one delivery attempt; a crash is always a visible, non-zero failure."""
    try:
        return _deliver_impl(
            dry_run=dry_run,
            force=force,
            digest_text=digest_text,
            failure_reason=failure_reason,
            failure_step=failure_step,
        )
    except Exception as exc:
        detail = _one_line(f"{type(exc).__name__}: {exc}")[:300]
        if not dry_run:
            _record_crash_failure(detail)
        print(f"[CRON_FAILURE] delivery crashed: {detail}", file=sys.stderr)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Send the daily digest by email and a ≤200-char summary on Weixin"
    )
    ap.add_argument("--dry-run", action="store_true", help="render and print JSON only, send nothing")
    ap.add_argument("--force", action="store_true", help="re-send channels already marked ok")
    ap.add_argument("--digest-text", default="", help="pre-rendered digest body (default: run build_digest.py)")
    ap.add_argument("--digest-file", default="", help="read the digest body from a file")
    ap.add_argument("--failure-reason", default="", help="force failure mode with this error detail")
    ap.add_argument("--failure-step", default="", help="failed stage label for failure mode")
    args = ap.parse_args(argv)

    digest_text = (args.digest_text or "").strip()
    if args.digest_file:
        try:
            digest_text = Path(args.digest_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"[CRON_FAILURE] could not read --digest-file: {exc}", file=sys.stderr)
            return 1

    return deliver(
        dry_run=args.dry_run,
        force=args.force,
        digest_text=digest_text or None,
        failure_reason=args.failure_reason,
        failure_step=args.failure_step,
    )


if __name__ == "__main__":
    raise SystemExit(main())
