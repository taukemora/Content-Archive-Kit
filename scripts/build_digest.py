#!/usr/bin/env python3
"""Build a daily digest from entries archived since a timestamp.

Phase 3 (§1/§2/§3): the digest must surface source health, not just "no new
items today" — empty/error sources, no-date entries, quota deferrals, stale
sources and sources that need attention.

Wave 1B (docs/plans/phase4-all-fixes.md P3 #11):
- timestamps are parsed (tz-aware, UTC+8) before comparing, never string-compared;
- when the run JSON carries a ``candidates`` list, only entries for *this* run's
  candidates count as new, so a manual bookmark archived concurrently cannot
  show up in the vendor digest;
- ``--gate`` (or an auto-detected sibling ``daily-gate.json``) surfaces the
  candidates that failed and were re-queued for the next run.
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

from dateutil import parser as dateparser

from common import ROOT, TZ, dedupe_key, load_seen, now

STALE_DAYS_WARN = 14
GATE_NAME = "daily-gate.json"
PENDING_NAME = "pending.json"
LATEST_RUN_NAME = "latest-fetch.json"


def live_pending_count(root: Path) -> tuple[int | None, str]:
    """Size of the live retry queue: ``(count, status)``.

    ``daily_gate.finish`` prunes terminal selections from ``.state/pending.json``
    after the run JSON is written, so ``run.pending_total`` is a fetch-time
    snapshot that may overstate the queue by the time the digest renders.

    ``status`` is ``"ok"`` (``count`` is the number of ``entries``),
    ``"missing"`` (no queue file at all) or ``"invalid"`` (present but
    unreadable, or not ``{"entries": {...}}``).  Missing and invalid are
    deliberately distinct: only a missing file falls back to the snapshot, while
    an unreadable one must be reported as unknown — never as zero.
    """
    path = Path(root) / ".state" / PENDING_NAME
    if not path.exists():
        return None, "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, "invalid"
    if not isinstance(data, dict):
        return None, "invalid"
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return None, "invalid"
    return len(entries), "ok"


def run_is_current(run_data: dict | None) -> bool:
    """True when this digest renders the instance's latest fetch run.

    The live queue is only read for a run identified by
    ``.state/runs/latest-fetch.json``: same non-empty ``generated_at`` (path
    alone is not identity — a copied or renamed run JSON still matches).
    Historical or uncheckable runs keep their own ``pending_total``.
    """
    if not isinstance(run_data, dict):
        return False
    gen = str(run_data.get("generated_at") or "").strip()
    if not gen:
        return False
    latest_path = ROOT / ".state" / "runs" / LATEST_RUN_NAME
    try:
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(latest, dict):
        return False
    return str(latest.get("generated_at") or "").strip() == gen


def load_run(path: str) -> dict | None:
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def parse_dt(value) -> datetime | None:
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


def load_gate(path: str, run_path: str, run_data: dict | None) -> dict:
    """Read the gate for this run: explicit ``--gate`` first, then sibling."""
    if path:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    if not run_path:
        return {}
    sibling = Path(run_path).parent / GATE_NAME
    if not sibling.exists():
        return {}
    try:
        data = json.loads(sibling.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    # Only trust a gate that belongs to this very fetch run.
    gate_gen = data.get("fetch_generated_at")
    run_gen = (run_data or {}).get("generated_at")
    if not gate_gen or not run_gen or gate_gen != run_gen:
        return {}
    return data


def candidate_key_set(run_data: dict | None) -> set[str] | None:
    """Keys of this run's candidates, or ``None`` when the run has no list."""
    if not isinstance(run_data, dict) or "candidates" not in run_data:
        return None
    keys: set[str] = set()
    for cand in run_data.get("candidates") or []:
        if not isinstance(cand, dict):
            continue
        url = str(cand.get("url") or "")
        if url:
            keys.add(dedupe_key(url))
        for extra in cand.get("dedupe_keys") or []:
            extra = str(extra or "")
            if extra:
                keys.add(dedupe_key(extra))
    keys.discard("")
    return keys


def status_reason(src: dict) -> str:
    status = src.get("status") or "unknown"
    if status == "empty":
        return src.get("empty_reason") or "未解析到条目"
    if status == "error":
        return src.get("error") or "抓取失败"
    return status


def rejection_records(sources: list[dict]) -> list[tuple[str, dict]]:
    """Per-section rejections flattened with their source label.

    GLM's adapter rejects sections without a real id anchor instead of
    fabricating one; those sections never reach candidates, so the digest is
    the only place they can stay visible. Older run JSON has no such field.
    """
    records: list[tuple[str, dict]] = []
    for src in sources:
        label = str(src.get("label") or src.get("id") or "")
        for rec in src.get("rejections") or []:
            if isinstance(rec, dict):
                records.append((label, rec))
    return records


def selection_view(run_data: dict | None, gate_data: dict) -> dict | None:
    """Read-only selection accounting for this digest, or ``None`` when disabled."""
    if not (ROOT / "selection.yaml").exists():
        return None
    try:
        import subscription_selection as mod

        policy = mod.load_policy(ROOT)
        if policy is None:
            return None
        ledger = mod.load_ledger(ROOT)
    except Exception as exc:
        return {"error": str(exc)}
    try:
        seen_keys = set((load_seen(ROOT / ".state" / "seen.json").get("entries") or {}).keys())
    except Exception:
        seen_keys = set()
    failed = {
        dedupe_key(str(rec.get("url") or ""))
        for rec in (gate_data.get("failed_candidates") or [])
        if isinstance(rec, dict)
    }
    failed.discard("")
    report = mod.classify_candidates(
        ledger,
        policy,
        (run_data or {}).get("candidates") or [],
        seen_keys=seen_keys,
        failed_keys=failed,
    )
    return {
        "policy": policy,
        "report": report,
        "overview": mod.ledger_overview(ledger, policy),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="", help="ISO timestamp; default today 00:00 +08:00")
    ap.add_argument("--run", default="", help="fetch_sources.py JSON output for failure section")
    ap.add_argument("--gate", default="", help="daily-gate.json (auto-detected next to --run)")
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()
    if args.limit < 0:
        ap.error("--limit must be 0 or greater")

    run_data = load_run(args.run)
    gate_data = load_gate(args.gate, args.run, run_data)
    sources = (run_data or {}).get("sources", []) or []
    not_ok = [s for s in sources if s.get("status") != "ok"]
    failed_candidates = [
        rec for rec in (gate_data.get("failed_candidates") or []) if isinstance(rec, dict)
    ]

    if args.since:
        since = args.since
    elif run_data and run_data.get("generated_at"):
        since = run_data["generated_at"]
    else:
        n = now()
        since = n.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    since_dt = parse_dt(since)

    with open(ROOT / "index.csv", encoding="utf-8-sig", newline="") as f:
        all_rows = list(csv.DictReader(f))

    cand_keys = candidate_key_set(run_data)

    rows = []
    for r in all_rows:
        archived_dt = parse_dt(r.get("archived"))
        if since_dt is not None:
            if archived_dt is None:
                # Unparsable timestamps fall back to the raw comparison.
                if (r.get("archived") or "") < since:
                    continue
            elif archived_dt < since_dt:
                continue
        if cand_keys is not None and dedupe_key(str(r.get("url") or "")) not in cand_keys:
            # A concurrent manual bookmark is not part of this run's vendor pull.
            continue
        rows.append(r)
    rows.sort(key=lambda r: (r.get("published") or "", r.get("archived") or ""), reverse=True)
    # Count the real total before truncating the display: the digest must never
    # pass off "展示 N 篇" as "新增 N 篇".
    total_new = len(rows)
    omitted = max(total_new - max(args.limit, 0), 0)
    run_rows = rows
    rows = rows[: args.limit]

    # Calculate skipped label
    if run_data:
        if any("skipped" in s for s in sources):
            skipped_total = sum(int(s.get("skipped", 0)) for s in sources)
            skipped_label = f"去重跳过 {skipped_total} 篇"
        else:
            skipped_label = "去重跳过 未知"
    else:
        skipped_label = "去重跳过 0 篇"

    global_dropped = int((run_data or {}).get("global_dropped") or 0)
    no_date_total = int(
        (run_data or {}).get("no_date_selected_total")
        if (run_data or {}).get("no_date_selected_total") is not None
        else (run_data or {}).get("no_date_total") or 0
    )
    pending_total = int((run_data or {}).get("pending_total") or 0)
    # Only the latest fetch run may report the live queue; a historical run keeps
    # its own fetch-time snapshot. An unreadable current queue is "unknown", not 0.
    pending_unknown = False
    if not args.run or run_is_current(run_data):
        live_count, pending_status = live_pending_count(ROOT)
        if pending_status == "ok":
            pending_total = int(live_count or 0)
        elif pending_status == "invalid":
            pending_unknown = True
    attention = [s for s in sources if s.get("needs_attention")]

    selection = selection_view(run_data, gate_data)
    selection_error = (selection or {}).get("error") if isinstance(selection, dict) else ""
    selection_report = (selection or {}).get("report") if isinstance(selection, dict) else None
    selection_overview = (selection or {}).get("overview") if isinstance(selection, dict) else {}
    review_limit = int((selection_overview or {}).get("review_limit") or 2)
    open_counts = dict((selection_overview or {}).get("open_counts") or {})
    overview_open = int((selection_overview or {}).get("open_total") or 0)
    selection_attention = False
    if selection_report:
        selection_counts = selection_report["counts"]
        selection_attention = any(
            selection_counts.get(kind)
            for kind in (
                "unjudged",
                "pending_archive",
                "archive_unproven",
                "budget_wait",
                "judgment_wait",
                "technical_failure",
                "execution_expired",
            )
        ) or bool((selection_overview or {}).get("unresolved")) or bool(
            (selection_overview or {}).get("needs_reassessment")
        )
    # Open work outside the current run is never a healthy empty result.
    selection_attention = selection_attention or bool(
        overview_open
        or (selection_overview or {}).get("stalled")
        or (selection_overview or {}).get("overflow")
    )
    if selection_error:
        selection_attention = True


    date_label = now().strftime("%Y-%m-%d")
    lines = [f"📚 内容收藏夹 · {date_label}"]

    head = [f"新增 {total_new} 篇", skipped_label]
    if total_new:
        head.append(f"展示 {len(rows)} 篇" + (f"，省略 {omitted} 篇" if omitted else ""))
    coverage_skipped = int((run_data or {}).get("coverage_skipped") or 0)
    if coverage_skipped:
        head.append(f"覆盖窗外跳过 {coverage_skipped} 篇")
    head.append(f"空/失败 {len(not_ok)} 源")
    if global_dropped:
        head.append(f"因配额顺延 {global_dropped} 篇")
    if selection_report:
        selection_counts = selection_report["counts"]
        head.append(
            f"选题：归档 {selection_counts['archived']} · 排除 {selection_counts['rejected']}"
            f" · 暂缓 {selection_counts['deferred']} · 预算等待 {selection_counts['budget_wait']}"
            f" · 判断预算等待 {selection_counts['judgment_wait']}"
            f" · 技术失败 {selection_counts['technical_failure']}"
        )
    lines.append(" · ".join(head))
    if selection_report and overview_open:
        oldest = (selection_overview or {}).get("oldest_open_days")
        lines.append(
            f"历史未结选题 {overview_open} 个"
            + (f"（最久 {oldest} 天）" if oldest is not None else "")
            + f" · 预算等待 {open_counts.get('budget_wait', 0)}"
            + f" · 判断预算等待 {open_counts.get('judgment_wait', 0)}"
            + f" · 执行暂停/过期 {open_counts.get('execution_expired', 0)}"
            + f" · 停滞 {int((selection_overview or {}).get('stalled') or 0)} 个"
            + "。下一步：`bin/ca select status` 查看并处理。"
        )

    overflow_keys = list((selection_overview or {}).get("overflow") or [])
    if overflow_keys:
        shown = "、".join(overflow_keys[:5])
        lines.append(
            f"待补队列溢出标记 {len(overflow_keys)} 条：{shown}"
            "（保留原入队时间，稍后有容量时自动恢复；也可显式 reassess）"
        )

    if selection_error:
        lines.append(f"⚠️ 选题账本不可读：{selection_error}（不能据此判断今日无新增）")
    if no_date_total:
        lines.append(f"其中 {no_date_total} 条无日期，已按抓取日期暂存")
    if pending_unknown:
        lines.append("⚠️ 待补队列 未知（当前队列不可读，不能视为已清零）")
    elif pending_total:
        lines.append(f"待补队列 {pending_total} 篇（下次优先）")
    if failed_candidates:
        lines.append(f"本轮抓取失败候选 {len(failed_candidates)} 篇（已入待补队列）")
    lines.append("")

    # If > 5 entries, add 3-5 highlights
    if len(rows) > 5:
        lines.append("## 本日要点")
        lines.append("")
        for r in rows[:5]:
            src = r.get("source", "")
            tit = r.get("title", "")
            lines.append(f"- {src}：{tit}")
        lines.append("")

    if rows:
        groups: dict[str, list[dict]] = {}
        for r in rows:
            s = r.get("source") or "其他"
            groups.setdefault(s, []).append(r)

        global_idx = 0
        for s, items in groups.items():
            items.sort(key=lambda r: (r.get("published") or "", r.get("archived") or ""), reverse=True)
            lines.append(f"【{s}】")
            lines.append("")
            for r in items:
                global_idx += 1
                title = r.get("title", "")
                if r.get("status") == "partial":
                    title += " ⚠️ 部分"
                lines.append(f"{global_idx}. {title}")
                lines.append(f"   {r.get('published','')[:10]} · {r.get('source')} · {r.get('url')}")
                if r.get("status") == "partial" and r.get("content_note"):
                    lines.append(f"   部分抓取：{r.get('content_note')}")
                relevance = (r.get("relevance") or "").strip()
                if relevance:
                    lines.append(f"   > 为什么值得看：{relevance}")
                lines.append(f"   摘要：{r.get('summary','').strip()}")
                lines.append("")
    else:
        if total_new > 0:
            lines.append(f"本次展示 0 篇（共新增 {total_new} 篇，全部省略）。")
            lines.append("")
            lines.append("条目已归档，只是被 --limit 截断展示，不代表没有新增。")
        elif not_ok:
            lines.append(
                "本次运行没有归档新增，但不是全源正常：不能据此断定今日无新增。"
            )
            lines.append("")
            lines.append("未达源见下方“源状态”，恢复后再判断今天是否真的没有更新。")
        elif selection_report and selection_report["rows"] and (
            # Only every current candidate rejected or deferred is an editorial
            # close: one mixed technical failure/unjudged/pending falls through.
            selection_report["counts"]["rejected"] + selection_report["counts"]["deferred"]
            == len(selection_report["rows"])
        ):
            lines.append(
                "本次没有归档新增：本轮候选均已判定排除或暂缓，详见下方选题。"
            )
        elif selection_attention:
            lines.append(
                "本次没有归档新增，但选题账务仍有未闭合项"
                "（未判断/待归档/预算等待/判断预算等待/技术失败/执行暂停），"
                "不能据此判断今天真的没有更新。"
            )
        elif sources:
            lines.append("今日无新增。")
        else:
            lines.append("无源状态数据，无法判断是否有新增。")
        lines.append("")

    # Coverage section (## 覆盖说明): every partial entry of this run, including
    # ones omitted from the display by --limit, stays labelled "本 run 新增".
    this_run_partials = [r for r in run_rows if r.get("status") == "partial"]
    this_run_ids = {r.get("id") for r in this_run_partials}

    today_00 = f"{now().strftime('%Y-%m-%d')}T00:00:00"
    existing_partials = [
        r for r in all_rows
        if r.get("status") == "partial"
        and (r.get("archived") or "") >= today_00
        and r.get("id") not in this_run_ids
    ]

    coverage_items: list[tuple[dict, str]] = [
        (r, "（本 run 新增）") for r in this_run_partials
    ] + [
        (r, "（今日已存）") for r in existing_partials
    ]

    if coverage_items:
        lines.append("## 覆盖说明")
        lines.append("")
        for r, tag in coverage_items:
            note = r.get("content_note", "").strip() or "正文在末尾截断，可能不完整"
            lines.append(f"- {r.get('title')}{tag}：{note}")
        lines.append("")

    pending_exits = [
        e for e in ((run_data or {}).get("pending_exits") or []) if isinstance(e, dict)
    ]
    pending_expired = int((run_data or {}).get("pending_expired") or 0)
    if pending_exits:
        lines.append(f"## 待补队列退出 {len(pending_exits)} 条")
        lines.append("")
        # seen.json is the only archive gate: an exited item can come back as a
        # *fresh* candidate only while it is still in the feed, inside the
        # coverage window and unseen. Seen exits are never re-selected.
        exit_note = (
            "退出只是队列生命周期事件，不等于净减少：已过期或超出待补容量的条目，"
            "只有仍出现在 feed、仍在覆盖窗口内且未被 seen 归档时，才会作为新候选重新进入；"
            "因 seen 归档而退出的条目不会再被新选。"
        )
        if pending_expired:
            exit_note = f"其中已过期 {pending_expired} 条。" + exit_note
        lines.append(exit_note)
        lines.append("")
        reason_labels = {
            "expired": "已过期（超过保留期）",
            "seen": "已归档（不再重试）",
            "over_limit": "超出待补容量",
            "selection_terminal": "已由选题终态取代（不再重试）",
        }
        for exit_rec in pending_exits:
            url = str(exit_rec.get("url") or exit_rec.get("key") or "")
            reason = str(exit_rec.get("reason") or "unknown")
            label = reason_labels.get(reason, reason)
            queued = str(exit_rec.get("queued_at") or "")[:10]
            suffix = f"（入队 {queued}）" if queued else ""
            lines.append(f"- {url}：{label}{suffix}")
        lines.append("")

    if failed_candidates:
        lines.append("未归档候选（已入待补队列）：")
        for rec in failed_candidates:
            url = rec.get("url") or rec.get("key") or ""
            reason = (rec.get("reason") or "").strip() or "抓取失败"
            lines.append(f"- {url}：{reason}")
        lines.append("")

    if not_ok:
        lines.append("未达源（空/失败）：")
        for s in not_ok:
            label = s.get("label") or s.get("id")
            lines.append(f"- {label}：{status_reason(s)}")
        lines.append("")

    rejections = rejection_records(sources)
    if rejections:
        count = int((run_data or {}).get("rejections_total") or len(rejections))
        lines.append(f"## 章节拒绝 {count} 条")
        lines.append("")
        lines.append(
            "以下章节未被归档，也不会编造链接：缺少真实 id 锚点的 section 一律拒绝。"
        )
        lines.append("")
        reason_labels = {"missing_section_id": "缺少章节 id 锚点"}
        for label, rec in rejections:
            title = str(rec.get("title") or "未命名章节")
            reason = str(rec.get("reason") or "unknown")
            detail = str(rec.get("message") or "").strip()
            date = str(rec.get("date") or "").strip()
            bits = [f"{reason_labels[reason]}（{reason}）" if reason in reason_labels else reason]
            if detail:
                bits.append(detail)
            if date:
                bits.append(date)
            lines.append(f"- {label}：{title} — " + " · ".join(bits))
        lines.append("")

    if selection_report:
        counts = selection_report["counts"]
        lines.append("## 选题")
        lines.append("")
        lines.append(
            f"本轮候选 {len(selection_report['rows'])} 个：归档 {counts['archived']}"
            f" · 排除 {counts['rejected']} · 暂缓 {counts['deferred']}"
            f" · 预算等待 {counts['budget_wait']} · 技术失败 {counts['technical_failure']}"
            f" · 判断预算等待 {counts['judgment_wait']}"
            f" · 未判断 {counts['unjudged']} · 待归档 {counts['pending_archive']}"
            f" · 待补证 {counts['archive_unproven']}"
        )
        unproven = [
            row for row in selection_report["rows"] if row["kind"] == "archive_unproven"
        ]
        if unproven:
            lines.append("")
            lines.append(
                f"归档缺少持久证据 {len(unproven)} 个（账务未闭合，需要审计/修复，不是选题拒绝）："
            )
            for row in unproven[:10]:
                lines.append(f"- {row['url']}")

        if selection_report["exclusions"]:
            lines.append("")
            lines.append("排除（按规则）：")
            for entry in selection_report["exclusions"][:10]:
                reason = f"（{entry['reason']}）" if entry.get("reason") else ""
                lines.append(f"- {entry['rule_id']}：{entry['count']} 篇{reason}")
        if selection_report["unresolved"]:
            lines.append("")
            lines.append("未解决的暂缓：")
            for item in selection_report["unresolved"][:10]:
                tail = "（已过期，仍未结案）" if item.get("expired") else ""
                lines.append(
                    f"- {item.get('title') or item.get('url')}："
                    f"缺口 {item.get('gap') or '—'}；后续 {item.get('follow_up') or '—'}"
                    f"（复核 {item.get('review_attempts') or 0}/{review_limit}）{tail}"

                )
        technical = [
            row for row in selection_report["rows"] if row["kind"] == "technical_failure"
        ]
        if technical:
            lines.append("")
            lines.append(f"技术失败 {len(technical)} 个（不是选题拒绝，仍会重试）：")
            for row in technical[:10]:
                lines.append(f"- {row['url']}")
        overview = selection_overview or {}
        if overview.get("needs_reassessment"):
            lines.append("")
            lines.append(
                f"需要显式重评 {len(overview['needs_reassessment'])} 个"
                "（政策或观测内容已变化，不会自动重排）"
            )
        if overview.get("unresolved") and not selection_report["unresolved"]:
            lines.append("")
            lines.append(f"历史未解决的暂缓 {len(overview['unresolved'])} 个（见 `bin/ca select status`）")
        lines.append("")
        lines.append(
            "选题账务闭合只说明本轮候选都已登记处理，不等于站点或研究全覆盖；"
            "预算、覆盖窗口与未扫描工作见上列。"
        )
        lines.append("")

    if sources:
        lines.append("## 源状态")
        lines.append("")
        for s in sources:
            label = s.get("label") or s.get("id")
            status = s.get("status") or "unknown"
            bits = [status]
            if s.get("selected") is not None and status == "ok":
                bits.append(f"候选 {s.get('selected')} 篇")
            if s.get("dropped_by_max"):
                bits.append(f"顺延 {s.get('dropped_by_max')} 篇")
            if s.get("no_date_selected"):
                bits.append(f"无日期候选 {s.get('no_date_selected')} 条")
            if s.get("coverage_window"):
                bits.append(f"覆盖窗口 {s.get('coverage_window')}")
            if s.get("pending_expired"):
                bits.append(f"待补过期 {s.get('pending_expired')} 条")
            if s.get("rejected"):
                bits.append(f"章节拒绝 {s.get('rejected')} 条")
            stale_days = s.get("stale_days")
            if stale_days is not None and int(stale_days) > STALE_DAYS_WARN:
                bits.append(f"该源 {stale_days} 天无更新")
            if status in ("empty", "error"):
                bits.append(status_reason(s))
            if s.get("needs_attention"):
                bits.append(f"连续 {s.get('streak') or 3} 次未成功，需人工关注")
            lines.append(f"- {label}：" + " · ".join(str(b) for b in bits))
        lines.append("")

    if attention:
        labels = "、".join((s.get("label") or s.get("id") or "") for s in attention)
        lines.append(f"⚠️ 需人工关注：{labels}（连续空/失败，先查源再跑 cron）")
        lines.append("")

    print("\n".join(lines).rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
