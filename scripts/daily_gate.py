#!/usr/bin/env python3
"""Daily ingest gate: start / finish checkpoints and candidate reconciliation.

Wave 1B (docs/plans/phase4-all-fixes.md P1 #1): every candidate that ``daily
start`` selected must be accounted for before the run can pass. A candidate is
accounted for when it is archived (present in ``seen.json``) or when its failure
was registered via ``daily fail-candidate``. Anything else is parked in
``.state/pending.json`` and fails the run, so a silent "web_extract 502" can no
longer make a candidate disappear.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from dateutil import parser as dateparser

from common import ROOT, TZ, canonicalize_url, dedupe_key, iso, load_json, load_seen, load_yaml, now, write_json_atomic

GATE_NAME = "daily-gate.json"
FETCH_NAME = "latest-fetch.json"


@contextmanager
def state_lock():
    """Exclusive advisory lock on ``.state/.lock`` (shared with fetch_sources)."""
    lock_path = ROOT / ".state" / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def gate_path() -> Path:
    return ROOT / ".state" / "runs" / GATE_NAME


def fetch_path() -> Path:
    return ROOT / ".state" / "runs" / FETCH_NAME


def pending_path() -> Path:
    return ROOT / ".state" / "pending.json"


# ---------------------------------------------------------- layered statuses

VALID_STATUSES = ("pending", "ok", "failed")


def archive_status_of(gate_data: dict | None) -> str:
    """Archive checkpoint status, deriving it from legacy ``status`` if needed."""
    data = gate_data if isinstance(gate_data, dict) else {}
    explicit = str(data.get("archive_status") or "").strip()
    if explicit in VALID_STATUSES:
        return explicit
    legacy = str(data.get("status") or "").strip()
    if legacy in ("ok", "failed"):
        return legacy
    return "pending"


def overall_status_of(gate_data: dict | None) -> str:
    """Overall run status: archive first, then the recorded delivery channels."""
    data = gate_data if isinstance(gate_data, dict) else {}
    archive = archive_status_of(data)
    if archive in ("failed", "pending"):
        return archive
    delivery = data.get("delivery")
    delivery = delivery if isinstance(delivery, dict) else {}
    states = [delivery.get(channel) for channel in ("email", "weixin")]
    if not any(isinstance(state, dict) and state.get("status") for state in states):
        return "pending"
    return "ok" if all(
        isinstance(state, dict) and state.get("status") == "ok" for state in states
    ) else "failed"


def record_delivery_failure(path: Path | None = None, detail: str = "") -> bool:
    """Persist a visible delivery failure for channels without a recorded ``ok``.

    Called when the delivery module crashes or cannot write its own result, so a
    lost result never leaves ``overall_status`` silently ``pending``/``ok``.
    """
    target = Path(path) if path is not None else gate_path()
    if not target.exists():
        return False
    try:
        with state_lock():
            data = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return False
            delivery = data.get("delivery")
            delivery = dict(delivery) if isinstance(delivery, dict) else {}
            stamp = iso()
            note = _one_line(detail)[:300] if detail else "delivery failed"
            for channel in ("email", "weixin"):
                rec = delivery.get(channel)
                rec = dict(rec) if isinstance(rec, dict) else {}
                if rec.get("status") == "ok":
                    continue  # a confirmed send stays confirmed
                if rec.get("status") == "failed" and rec.get("detail"):
                    continue  # keep the module's own failure reason
                rec["status"] = "failed"
                rec.setdefault("at", stamp)
                rec["detail"] = note
                delivery[channel] = rec
            delivery["updated_at"] = stamp
            delivery.setdefault("mode", "failure")
            delivery["email_status"] = (delivery.get("email") or {}).get("status", "")
            delivery["weixin_status"] = (delivery.get("weixin") or {}).get("status", "")
            data["delivery"] = delivery
            data["archive_status"] = archive_status_of(data)
            data["overall_status"] = "failed"
            write_json_atomic(target, data)
        return True
    except Exception as exc:
        print(f"[gate] could not record delivery failure: {_one_line(exc)[:200]}", file=sys.stderr)
        return False


def refresh_overall_status(path: Path | None = None) -> None:
    """Recompute archive/overall status from the latest gate, keeping delivery."""
    target = Path(path) if path is not None else gate_path()
    if not target.exists():
        return
    try:
        with state_lock():
            data = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            data["archive_status"] = archive_status_of(data)
            data["overall_status"] = overall_status_of(data)
            write_json_atomic(target, data)
    except Exception as exc:
        print(f"[gate] could not refresh overall status: {_one_line(exc)[:200]}", file=sys.stderr)


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def _selection_context():
    """Return ``(module, policy)``; ``(None, None)`` when selection is absent."""
    if not (ROOT / "selection.yaml").exists():
        return None, None
    try:
        import subscription_selection as module
    except Exception as exc:
        raise RuntimeError(
            f"selection.yaml exists but subscription_selection.py is unavailable: {exc}"
        ) from exc
    return module, module.load_policy(ROOT)



def cleanup_runs(days: int = 30) -> list[str]:
    """Delete old run JSON files while keeping the current gate/fetch files."""
    runs_dir = ROOT / ".state" / "runs"
    if not runs_dir.exists():
        return []
    keep = {GATE_NAME, FETCH_NAME}
    cutoff = now() - timedelta(days=days)
    removed: list[str] = []
    for p in runs_dir.glob("*.json"):
        if p.name in keep or not p.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=TZ)
            if mtime < cutoff:
                p.unlink()
                removed.append(p.name)
        except Exception:
            continue
    if removed:
        print(f"[runs] pruned {len(removed)} run file(s) older than {days} days", file=sys.stderr)
    return removed


def take_seen_snapshot() -> dict:
    try:
        from reconcile_seen import snapshot_seen
        return snapshot_seen(ROOT)
    except Exception as e:
        print(f"[seen] snapshot failed: {e}", file=sys.stderr)
        return {"status": "error", "error": str(e)}


def parse_dt(val: str) -> datetime:
    dt = dateparser.parse(val)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


# ---------------------------------------------------------------- delivery


def _run_delivery(*, dry_run: bool = False, force: bool = False, **kwargs) -> int:
    """Invoke ``deliver_daily.deliver`` without ever crashing the gate script."""
    try:
        from deliver_daily import deliver
    except Exception as e:  # pragma: no cover - import failure is fatal for delivery
        print(f"[CRON_FAILURE] delivery module unavailable: {e}", file=sys.stderr)
        return 1
    try:
        return int(deliver(dry_run=dry_run, force=force, **kwargs))
    except Exception as e:  # pragma: no cover - defensive
        print(f"[CRON_FAILURE] delivery crashed: {e}", file=sys.stderr)
        return 1



# ---------------------------------------------------------- candidate helpers


def candidate_keys(cand: dict) -> set[str]:
    """Every identity a candidate can be matched by (dedupe_key + aliases)."""
    keys: set[str] = set()
    url = str((cand or {}).get("url") or "")
    if url:
        keys.add(dedupe_key(url))
    for extra in (cand or {}).get("dedupe_keys") or []:
        extra = str(extra or "")
        if extra:
            keys.add(dedupe_key(extra))
    keys.discard("")
    return keys


def failed_keys(gate_data: dict) -> set[str]:
    keys: set[str] = set()
    for rec in gate_data.get("failed_candidates") or []:
        for key in (rec.get("key"), dedupe_key(str(rec.get("url") or ""))):
            if key:
                keys.add(str(key))
    keys.discard("")
    return keys


def _parse_pending_ts(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        dt = dateparser.parse(str(val))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except Exception:
        return None


def _earliest_queue_dt(rec: dict) -> datetime | None:
    found: list[datetime] = []
    for key in ("queued_at", "first_seen", "added_at"):
        dt = _parse_pending_ts(rec.get(key))
        if dt is not None:
            found.append(dt)
    if found:
        return min(found)
    for key in ("parked_at", "dropped_at"):
        dt = _parse_pending_ts(rec.get(key))
        if dt is not None:
            found.append(dt)
    return min(found) if found else None


def park_candidates(records: list[dict], reason: str, fetch_data: dict | None) -> list[str]:
    """Append records to ``.state/pending.json`` so the next run retries them.

    Full candidate records from ``latest-fetch.json`` are preferred (title and
    published date survive); a bare URL is still parked with a minimal record.
    Returns the parked keys.
    """
    by_key: dict[str, dict] = {}
    for cand in (fetch_data or {}).get("candidates") or []:
        for key in candidate_keys(cand):
            by_key.setdefault(key, cand)

    parked: dict[str, dict] = {}
    for rec in records:
        url = str((rec or {}).get("url") or "").strip()
        keys = candidate_keys(rec)
        if not url or not keys:
            continue
        key = dedupe_key(url)
        park_key = canonicalize_url(url, keep_fragment=True) if urlsplit(url).fragment else key
        source = by_key.get(key) or by_key.get(sorted(keys)[0]) or {}
        record = {k: v for k, v in source.items() if not k.startswith("_")}
        record.update({k: v for k, v in rec.items() if not k.startswith("_")})
        record["url"] = url
        if not record.get("source_id"):
            host = urlsplit(url).netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            cfg = load_yaml(ROOT / "sources.yaml") if (ROOT / "sources.yaml").exists() else {}
            for s in cfg.get("sources") or []:
                for k in ("feed", "list_url", "homepage"):
                    sh = urlsplit(str(s.get(k) or "")).netloc.lower()
                    if sh.startswith("www."):
                        sh = sh[4:]
                    if sh and (host == sh or host.endswith("." + sh) or sh.endswith("." + host)):
                        record["source_id"] = s.get("id", "")
                        break
                if record.get("source_id"):
                    break
        record["reason"] = reason
        record["parked_at"] = iso()
        record["updated_at"] = record["parked_at"]
        record.setdefault("dedupe_keys", sorted(keys - {key}))
        parked[park_key] = record

    if not parked:
        return []
    with state_lock():
        state = dict(load_json(pending_path(), {"entries": {}}))
        entries = dict(state.get("entries") or {})
        for park_key, new_rec in parked.items():
            existing = entries.get(park_key)
            if existing:
                merged = dict(existing)
                merged.update({k: v for k, v in new_rec.items() if v is not None and v != ""})
                for field in ("content_file", "cache_name", "raw_file"):
                    if existing.get(field) and not new_rec.get(field):
                        merged[field] = existing[field]
                merged_keys = list(existing.get("dedupe_keys") or [])
                for k in new_rec.get("dedupe_keys") or []:
                    if k not in merged_keys:
                        merged_keys.append(k)
                if merged_keys:
                    merged["dedupe_keys"] = merged_keys
                q_dt = _earliest_queue_dt(existing) or _earliest_queue_dt(new_rec)
                merged["queued_at"] = iso(q_dt or now())
                merged["parked_at"] = iso()
                merged["updated_at"] = merged["parked_at"]
                merged["reason"] = new_rec.get("reason") or existing.get("reason") or reason
                entries[park_key] = merged
            else:
                q_dt = _earliest_queue_dt(new_rec) or now()
                new_rec["queued_at"] = iso(q_dt)
                new_rec["parked_at"] = iso()
                new_rec["updated_at"] = new_rec["parked_at"]
                entries[park_key] = new_rec
        state["entries"] = entries
        state["updated_at"] = iso()
        write_json_atomic(pending_path(), state)
    return sorted(parked)


# ---------------------------------------------------------------- commands


def run_start(deliver_on_fail: bool = False) -> int:
    gate = gate_path()
    fetch_out = fetch_path()
    fetch_out.parent.mkdir(parents=True, exist_ok=True)
    cleanup_runs()

    def fail_start(msg: str) -> int:
        write_json_atomic(
            gate,
            {
                "started_at": iso(),
                "status": "failed",
                "archive_status": "failed",
                "overall_status": "failed",
                "error": msg,
                "fetch_generated_at": "",
            },
        )
        print(f"[CRON_FAILURE] {msg}", file=sys.stderr)
        if deliver_on_fail:
            code = _run_delivery(failure_reason=msg, failure_step="daily start")
            if code != 0:
                record_delivery_failure(
                    gate, f"failure notification failed (exit {code})"
                )
                print(
                    "[CRON_FAILURE] failure notification did not reach all channels",
                    file=sys.stderr,
                )
            refresh_overall_status(gate)
        return 1

    try:
        selection_module, selection_policy = _selection_context()
    except Exception as e:
        return fail_start(f"selection policy error: {e}")

    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "fetch_sources.py"),
        "--max",
        str(selection_policy.budgets["discovered"]) if selection_policy is not None else "10",
        "--json-out", str(fetch_out),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        err = res.stderr.strip() or res.stdout.strip() or f"exit code {res.returncode}"
        return fail_start(f"fetch_sources failed (exit {res.returncode}):\n{err}")

    try:
        data = json.loads(fetch_out.read_text(encoding="utf-8"))
    except Exception as e:
        return fail_start(f"failed to load fetch output: {e}")

    sources = data.get("sources", [])
    if bool(sources) and all(s.get("status") == "error" for s in sources):
        return fail_start("all sources returned status == error")

    not_ok = [s for s in sources if s.get("status") != "ok"]
    attention = [s for s in sources if s.get("needs_attention")]
    gate_data = {
        "started_at": iso(),
        "fetch_generated_at": data.get("generated_at", ""),
        "sources": sources,
        "candidates_total": len(data.get("candidates") or []),
        "failed_candidates": [],
        "not_ok": [
            {
                "id": s.get("id"),
                "label": s.get("label") or s.get("id"),
                "status": s.get("status"),
                "reason": s.get("empty_reason") or s.get("error") or "",
            }
            for s in not_ok
        ],
        "needs_attention": [s.get("id") for s in attention],
        "global_dropped": data.get("global_dropped", 0),
        "no_date_total": data.get("no_date_total", 0),
        "pending_total": data.get("pending_total", 0),
        "status": "started",
        "archive_status": "pending",
        "overall_status": "pending",
    }
    if selection_policy is not None:
        gate_data["selection"] = {
            "enabled": True,
            "policy_version": selection_policy.version,
            "policy_digest": selection_policy.digest,
            "budgets": dict(selection_policy.budgets),
            "fetch": data.get("selection") or {},
        }
    write_json_atomic(gate, gate_data)

    if not_ok:
        detail = "；".join(
            f"{s.get('label') or s.get('id')}="
            f"{s.get('status')}（{s.get('empty_reason') or s.get('error') or '-'}）"
            for s in not_ok
        )
        print(f"[fetch] 空/失败源 {len(not_ok)} 个：{detail}", file=sys.stderr)
    if attention:
        names = "、".join(str(s.get("label") or s.get("id")) for s in attention)
        print(f"[fetch] 需人工关注（连续空/失败）：{names}", file=sys.stderr)

    if res.stdout:
        print(res.stdout.strip())
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def run_fail_candidate(url: str, reason: str) -> int:
    """Record one candidate's fetch failure and park it for the next run."""
    gate = gate_path()
    if not gate.exists():
        print("[CRON_FAILURE] daily-gate.json 不存在：请先运行 daily start", file=sys.stderr)
        return 1
    try:
        gate_data = json.loads(gate.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[CRON_FAILURE] daily-gate.json 解析失败：{e}", file=sys.stderr)
        return 1
    if not isinstance(gate_data, dict):
        print("[CRON_FAILURE] daily-gate.json 结构异常", file=sys.stderr)
        return 1

    url = (url or "").strip()
    key = dedupe_key(url)
    if not url or not key:
        print("[CRON_FAILURE] fail-candidate 需要有效 URL", file=sys.stderr)
        return 1
    reason = (reason or "").strip() or "fetch failed"

    entry = {"url": url, "key": key, "reason": reason, "failed_at": iso()}
    failed = gate_data.setdefault("failed_candidates", [])
    if not isinstance(failed, list):
        failed = []
        gate_data["failed_candidates"] = failed
    for index, rec in enumerate(failed):
        if key in {str(rec.get("key") or ""), dedupe_key(str(rec.get("url") or ""))}:
            failed[index] = {**rec, **entry}
            break
    else:
        failed.append(entry)
    write_json_atomic(gate, gate_data)

    fetch_data = load_json(fetch_path(), {}) if fetch_path().exists() else {}
    parked = park_candidates([{"url": url}], "archive_failed", fetch_data)
    print(f"[fail-candidate] {url} 已登记失败（{reason}）", file=sys.stderr)
    if parked:
        print(f"[fail-candidate] 已加入待补队列 pending.json（{len(parked)} 条）", file=sys.stderr)
    else:
        print("[fail-candidate] 未写入待补队列（URL 为空）", file=sys.stderr)
    print(json.dumps(entry, ensure_ascii=False))
    return 0


def _preserve_latest_delivery(gate: Path, gate_data: dict) -> None:
    """Never let a stale in-memory snapshot clobber a newer delivered result."""
    latest = load_json(gate, {})
    if isinstance(latest, dict) and isinstance(latest.get("delivery"), dict):
        gate_data["delivery"] = latest["delivery"]


def run_finish(deliver: bool = False) -> int:
    gate = gate_path()
    fetch = fetch_path()

    def notify_failure(step: str, msg: str) -> None:
        """Send the failure email + Weixin short note (never a fallback email)."""
        if not deliver:
            return
        code = _run_delivery(failure_reason=f"{step} failed: {msg}", failure_step=step)
        if code != 0:
            record_delivery_failure(gate, f"failure notification failed (exit {code})")
            print(
                "[CRON_FAILURE] failure notification did not reach all channels",
                file=sys.stderr,
            )
        refresh_overall_status(gate)

    if not gate.exists() or not fetch.exists():
        msg = "missing daily-gate.json or latest-fetch.json"
        print(f"[CRON_FAILURE] {msg}", file=sys.stderr)
        notify_failure("daily finish", msg)
        return 1

    try:
        gate_data = json.loads(gate.read_text(encoding="utf-8"))
        fetch_data = json.loads(fetch.read_text(encoding="utf-8"))
    except Exception as e:
        msg = f"failed to parse daily-gate.json or latest-fetch.json: {e}"
        print(f"[CRON_FAILURE] {msg}", file=sys.stderr)
        notify_failure("daily finish", msg)
        return 1

    if gate_data.get("status") == "failed":
        err = gate_data.get("error", "previous run failed")
        print(f"[CRON_FAILURE] gate status is failed: {err}", file=sys.stderr)
        notify_failure("daily finish", f"gate status is failed: {err}")
        return 1

    started_at_str = gate_data.get("started_at")
    if not started_at_str:
        msg = "missing started_at in gate data"
        print(f"[CRON_FAILURE] {msg}", file=sys.stderr)
        notify_failure("daily finish", msg)
        return 1

    try:
        started_dt = parse_dt(started_at_str)
        now_dt = now()
        if started_dt > now_dt + timedelta(minutes=5):
            msg = f"stale gate: started_at ({started_at_str}) is in the future by >5min"
            print(f"[CRON_FAILURE] {msg}", file=sys.stderr)
            notify_failure("daily finish", msg)
            return 1
        if started_dt < now_dt - timedelta(hours=24):
            msg = f"stale gate: started_at ({started_at_str}) is older than 24h"
            print(f"[CRON_FAILURE] {msg}", file=sys.stderr)
            notify_failure("daily finish", msg)
            return 1
    except Exception as e:
        msg = f"timestamp parse error: {e}"
        print(f"[CRON_FAILURE] {msg}", file=sys.stderr)
        notify_failure("daily finish", msg)
        return 1

    gate_fetch_gen = gate_data.get("fetch_generated_at", "")
    latest_fetch_gen = fetch_data.get("generated_at", "")
    if not gate_fetch_gen or not latest_fetch_gen or gate_fetch_gen != latest_fetch_gen:
        msg = (
            f"fetch_generated_at mismatch: gate has '{gate_fetch_gen}', "
            f"latest-fetch has '{latest_fetch_gen}'"
        )
        print(f"[CRON_FAILURE] {msg}", file=sys.stderr)
        notify_failure("daily finish", msg)
        return 1

    def fail_finish(step: str, err_msg: str, retcode: int = 1) -> int:
        gate_data["status"] = "failed"
        gate_data["archive_status"] = "failed"
        gate_data["overall_status"] = "failed"
        gate_data["error"] = f"{step} failed: {err_msg}"
        _preserve_latest_delivery(gate, gate_data)
        write_json_atomic(gate, gate_data)
        print(f"[CRON_FAILURE] {step} failed (exit {retcode}):\n{err_msg}", file=sys.stderr)
        notify_failure(step, err_msg)
        return 1

    # Try to repair seen↔library drift before auditing it; never prune orphans.
    try:
        from reconcile_seen import reconcile_seen
        rec = reconcile_seen(ROOT, dry_run=False, prune_orphans=False, quiet=True)
        if rec.get("fixed"):
            print(f"[finish] reconciled seen.json (fixed={rec.get('fixed')})", file=sys.stderr)
    except Exception as e:
        print(f"[finish] reconcile-seen warning: {e}", file=sys.stderr)

    # ---- candidate accountability: archived, failed, or it fails the run.
    try:
        selection_module, selection_policy = _selection_context()
    except Exception as e:
        return fail_finish("selection_policy", str(e))

    candidates = fetch_data.get("candidates") or []
    unaccounted: list[dict] = []
    if candidates:
        try:
            seen = load_seen(ROOT / ".state" / "seen.json")
        except Exception as e:
            return fail_finish("load_seen", str(e))
        seen_keys = set((seen.get("entries") or {}).keys())
        known_failed = failed_keys(gate_data)
        if selection_policy is not None and selection_module is not None:
            try:
                ledger = selection_module.load_ledger(ROOT)
            except Exception as e:
                return fail_finish("selection_ledger", str(e))
            report = selection_module.classify_candidates(
                ledger,
                selection_policy,
                candidates,
                seen_keys=seen_keys,
                failed_keys=known_failed,
            )
            judgment_wait: dict = {}
            if report["unjudged"]:
                # Judgment-budget-limited work is a durable wait, not a
                # fabricated reject and not a permanently failing daily job.
                try:
                    judgment_wait = selection_module.mark_judgment_wait(
                        ROOT,
                        selection_policy,
                        keys=[row["key"] for row in report["unjudged"]],
                        run_gen=fetch_data.get("generated_at", ""),
                    )
                except Exception as e:
                    return fail_finish("selection_judgment_wait", str(e))
                if judgment_wait.get("marked"):
                    report = selection_module.classify_candidates(
                        selection_module.load_ledger(ROOT),
                        selection_policy,
                        candidates,
                        seen_keys=seen_keys,
                        failed_keys=known_failed,
                    )
            gate_data["selection"] = {
                **(gate_data.get("selection") or {}),
                "counts": report["counts"],
                "accounted": report["accounted"],
                "coverage": report["coverage"],
                "unjudged": report["unjudged"],
                "pending_archive": report["pending_archive"],
                "archive_unproven": report["archive_unproven"],
                "unresolved": report["unresolved"],
                "exclusions": report["exclusions"],
                "judgment_wait": judgment_wait,
            }
            unaccounted = report["unaccounted"]
            try:
                gate_data["selection"]["pending_pruned"] = selection_module.prune_pending(
                    ROOT, selection_policy
                )
            except Exception as e:
                return fail_finish("selection_pending_prune", str(e))
        else:
            for cand in candidates:
                keys = candidate_keys(cand)
                if not keys:
                    continue
                if keys & seen_keys or keys & known_failed:
                    continue
                unaccounted.append(cand)

    if unaccounted:
        parked = park_candidates(unaccounted, "unaccounted_at_finish", fetch_data)
        gate_data["unaccounted_candidates"] = [
            {"url": c.get("url"), "title": c.get("title") or ""} for c in unaccounted
        ]
        gate_data["unaccounted_parked"] = parked
        detail = "；".join(
            f"{c.get('kind') or 'unaccounted'}: {c.get('url')}" for c in unaccounted
        )
        return fail_finish(
            "unaccounted_candidates",
            f"{len(unaccounted)} 个候选未闭合选题账务（unjudged/pending_archive，"
            f"已入待补队列）：{detail}",
        )

    # Run rebuild_index.py BEFORE audit.py so newly added/finalized entries in
    # this run are reflected in index.csv/index.md when audit checks alignment.
    cmd_index = [sys.executable, str(ROOT / "scripts" / "rebuild_index.py")]
    index_env = dict(os.environ, CA_SKIP_AUTO_PUBLISH="1")
    res_index = subprocess.run(cmd_index, capture_output=True, text=True, env=index_env)
    if res_index.returncode != 0:
        err_msg = res_index.stderr.strip() or res_index.stdout.strip()
        return fail_finish("rebuild_index", err_msg, res_index.returncode)

    # Run audit.py
    cmd_audit = [sys.executable, str(ROOT / "scripts" / "audit.py")]
    res_audit = subprocess.run(cmd_audit, capture_output=True, text=True)
    if res_audit.returncode != 0:
        err_msg = res_audit.stdout.strip() or res_audit.stderr.strip()
        return fail_finish("audit", err_msg, res_audit.returncode)

    # Run build_digest.py (with the gate so failed candidates are surfaced)
    cmd_digest = [
        sys.executable,
        str(ROOT / "scripts" / "build_digest.py"),
        "--run", str(fetch),
        "--gate", str(gate),
        "--limit", "10",
    ]
    res_digest = subprocess.run(cmd_digest, capture_output=True, text=True)
    if res_digest.returncode != 0:
        err_msg = res_digest.stderr.strip() or res_digest.stdout.strip()
        return fail_finish("build_digest", err_msg, res_digest.returncode)

    # Backup seen and prune old run files after the digest succeeds.
    snapshot = take_seen_snapshot()
    pruned = cleanup_runs()
    gate_data["seen_snapshot"] = snapshot.get("snapshot") or snapshot.get("error", "")
    gate_data["runs_pruned"] = pruned

    # Update gate status only after all succeeded. ``status`` stays the archive
    # checkpoint for compatibility; ``overall_status`` also accounts for delivery.
    gate_data["finished_at"] = iso()
    gate_data["status"] = "ok"
    gate_data["archive_status"] = "ok"
    _preserve_latest_delivery(gate, gate_data)
    gate_data["overall_status"] = overall_status_of(gate_data)
    gate_data.pop("error", None)
    write_json_atomic(gate, gate_data)

    # Persist local success before attempting the optional external publication.
    # Publish the complete audited batch once, not from the index subprocess.
    try:
        from publish_site import maybe_publish
        gate_data["publication"] = maybe_publish(ROOT)
    except Exception:
        gate_data["publication"] = {"status": "failed", "reason": "publisher_unavailable"}
    _preserve_latest_delivery(gate, gate_data)
    gate_data["overall_status"] = overall_status_of(gate_data)
    write_json_atomic(gate, gate_data)

    digest_text = res_digest.stdout.strip()
    print(digest_text)

    if deliver:
        # Archiving is already successful: a delivery problem must not flip the
        # archive layer back to failed, but it does fail the overall run.
        code = _run_delivery(digest_text=digest_text)
        if code != 0:
            record_delivery_failure(gate, f"delivery failed (exit {code})")
        refresh_overall_status(gate)
        after = load_json(gate, {})
        state = after.get("delivery") if isinstance(after, dict) else {}
        state = state if isinstance(state, dict) else {}
        failed = [
            channel
            for channel in ("email", "weixin")
            if not isinstance(state.get(channel), dict)
            or state[channel].get("status") != "ok"
        ]
        if code != 0 or failed:
            print(
                f"[CRON_FAILURE] delivery failed: {', '.join(failed) or 'unknown channels'}",
                file=sys.stderr,
            )
            succeeded = [
                channel
                for channel in ("email", "weixin")
                if isinstance(state.get(channel), dict)
                and state[channel].get("status") == "ok"
            ]
            if succeeded and failed:
                print(
                    "[DELIVERY_PARTIAL_FAILURE] "
                    f"ok={','.join(succeeded)} failed={','.join(failed)}",
                    file=sys.stderr,
                )
            return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily ingest gate checkpoints")
    sub = ap.add_subparsers(dest="action", required=True)
    start_ap = sub.add_parser("start", help="start daily ingest checkpoint")
    start_ap.add_argument(
        "--deliver-on-fail",
        action="store_true",
        help="send the failure email + Weixin short note when start fails",
    )
    finish_ap = sub.add_parser("finish", help="finish daily ingest checkpoint")
    finish_ap.add_argument(
        "--deliver",
        action="store_true",
        help="deliver the digest by email + a <=200-char Weixin summary (also on failure)",
    )
    deliver_ap = sub.add_parser(
        "deliver",
        help="send (or preview) the dual-channel delivery for the current gate",
    )
    deliver_ap.add_argument("--dry-run", action="store_true", help="print the preview JSON only")
    deliver_ap.add_argument("--force", action="store_true", help="re-send channels already marked ok")
    fail_ap = sub.add_parser(
        "fail-candidate",
        help="record a candidate fetch failure and park it in the pending queue",
    )
    fail_ap.add_argument("url", help="candidate URL that could not be archived")
    fail_ap.add_argument("--reason", default="", help="why it failed (e.g. 'web_extract 502')")
    args = ap.parse_args()

    if args.action == "start":
        return run_start(deliver_on_fail=args.deliver_on_fail)
    elif args.action == "finish":
        return run_finish(deliver=args.deliver)
    elif args.action == "deliver":
        code = _run_delivery(dry_run=args.dry_run, force=args.force)
        if not args.dry_run:
            if code != 0:
                record_delivery_failure(gate_path(), f"delivery failed (exit {code})")
            refresh_overall_status(gate_path())
        return code
    elif args.action == "fail-candidate":
        return run_fail_candidate(args.url, args.reason)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
