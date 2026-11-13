#!/usr/bin/env python3
"""Optional subscription selection: policy validation, decisions, ledger.

The Agent makes editorial judgments; this module validates them against a
strict instance policy, binds them to a current discovery run, an exact
candidate key and recorded evidence, and persists them in one locked ledger.

Design contract: ``docs/subscription-selection.md``.

Fail-closed rules that matter for safety:

* a missing or ``enabled: false`` policy keeps the legacy discovery path;
* unknown keys, wrong types (including ``bool`` used as an integer), an
  unsupported mode and malformed limits raise :class:`SelectionConfigError`;
* a corrupt ledger raises :class:`SelectionStateError` instead of looking like
  an empty, healthy state;
* dry runs never write.

Nothing here performs model inference, archive writes, media localization or
publication. Article text and feed metadata are untrusted data: quotes are only
substring evidence, never instructions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from common import (
    ROOT,
    TZ,
    canonicalize_url,
    dedupe_key,
    is_allowed_file_path,
    iso,
    load_json,
    load_yaml,
    now,
    read_entry,
    state_lock,
    write_json_atomic,
    write_text_atomic,
)

POLICY_FILENAME = "selection.yaml"
SCHEMA_VERSION = 1
LEDGER_SCHEMA = 1
LEDGER_REL = Path(".state") / "selection" / "ledger.json"
PREVIEW_DIR_REL = Path(".state") / "selection" / "previews"

DEFAULT_BUDGETS = {
    "discovered": 40,
    "per_source": 20,
    "judgments": 20,
    "archives": 10,
}
DEFAULT_RETENTION = {
    "preview_days": 30,
    "identity_days": 180,
    "review_days": 14,
    "max_review_attempts": 2,
    "capacity": 20000,
}
DISCOVERY_LOOKBACK_DAYS = 30  # fetch_sources.MAX_WATERMARK_LOOKBACK_DAYS

MAX_PREVIEW_CHARS = 20000
PREVIEW_MAX_BYTES = 2 * 1024 * 1024
MAX_HISTORY = 5
MIN_QUOTE_CHARS = 4
ADMISSION_MAX_AGE_DAYS = 7

OUTCOMES = ("accept", "reject", "defer")
RULE_OUTCOMES = ("accept", "reject")
EVIDENCE_KINDS = ("metadata", "body", "either")

# Content outcome -- what the judgment said.
TERMINAL_OUTCOMES = ("accept", "reject", "defer")
# Execution state -- how far that outcome got. A reject/defer is a content
# outcome; its execution state records expiry, not a different judgment.
STATE_AWAITING = "awaiting_judgment"
STATE_BUDGET_WAIT = "budget_wait"
STATE_JUDGMENT_WAIT = "judgment_wait"
STATE_READY = "ready"
STATE_ARCHIVE_FAILURE = "archive_failure"
STATE_ARCHIVED = "archived"
STATE_REJECTED = "rejected"
STATE_DEFERRED = "unresolved"
STATE_EXECUTION_EXPIRED = "execution_expired"
STATES = (
    STATE_AWAITING,
    STATE_BUDGET_WAIT,
    STATE_JUDGMENT_WAIT,
    STATE_READY,
    STATE_ARCHIVE_FAILURE,
    STATE_ARCHIVED,
    STATE_REJECTED,
    STATE_DEFERRED,
    STATE_EXECUTION_EXPIRED,
)
OPEN_STATES = (
    STATE_AWAITING,
    STATE_BUDGET_WAIT,
    STATE_JUDGMENT_WAIT,
    STATE_READY,
    STATE_ARCHIVE_FAILURE,
)
ACCEPTED_OPEN_STATES = (STATE_BUDGET_WAIT, STATE_READY, STATE_ARCHIVE_FAILURE)
# Owed work that must never be aged out of the pending queue by the generic
# undecided-retry TTL: accepted admissions and explicit judgment-budget waits.
OWED_STATES = ACCEPTED_OPEN_STATES + (STATE_JUDGMENT_WAIT,)
MAX_RUN_HISTORY = 30

_POLICY_KEYS = {"schema", "enabled", "version", "mode", "rules", "budgets", "retention"}
_RULE_KEYS = {"id", "outcome", "description", "evidence"}
_RETENTION_KEYS = set(DEFAULT_RETENTION)
_RULE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class SelectionError(RuntimeError):
    """Base error with a process exit code."""

    exit_code = 3


class SelectionConfigError(SelectionError):
    exit_code = 2


class SelectionDisabledError(SelectionConfigError):
    exit_code = 2


class SelectionStateError(SelectionError):
    exit_code = 3


class SelectionBudgetError(SelectionError):
    exit_code = 3


class SelectionCapacityError(SelectionError):
    exit_code = 4


# ------------------------------------------------------------------ policy


@dataclass(frozen=True)
class Rule:
    id: str
    outcome: str
    description: str
    evidence: str

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "outcome": self.outcome,
            "description": self.description,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class Policy:
    schema: int
    enabled: bool
    version: int
    mode: str
    rules: tuple[Rule, ...]
    budgets: dict
    retention: dict
    digest: str
    source_path: str

    def rule(self, rule_id: str) -> Rule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise SelectionStateError(
            f"unknown rule id: {rule_id!r} (known: {', '.join(r.id for r in self.rules)})"
        )

    def rule_for_outcome(self, outcome: str) -> Rule:
        for rule in self.rules:
            if rule.outcome == outcome:
                return rule
        raise SelectionStateError(f"policy has no rule for outcome {outcome!r}")

    def summary(self) -> dict:
        return {
            "schema": self.schema,
            "version": self.version,
            "mode": self.mode,
            "digest": self.digest,
            "rules": [r.as_dict() for r in self.rules],
            "budgets": dict(self.budgets),
            "retention": dict(self.retention),
        }


def policy_path(root: Path | str | None = None, path: Path | str | None = None) -> Path:
    if path is not None:
        return Path(path)
    return (Path(root) if root is not None else ROOT) / POLICY_FILENAME


def _as_int(value, name: str, *, minimum: int = 1, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SelectionConfigError(f"{name} must be an integer (bool and strings are rejected)")
    if value < minimum:
        raise SelectionConfigError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise SelectionConfigError(f"{name} must be <= {maximum}, got {value}")
    return value


def _as_text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SelectionConfigError(f"{name} must be a non-empty string")
    return value.strip()


def parse_policy(data, *, source: str = "<memory>") -> Policy:
    """Validate one schema-1 policy mapping and compute its content digest."""
    if not isinstance(data, dict):
        raise SelectionConfigError(f"{source}: policy must be a YAML mapping")
    unknown = sorted(set(data) - _POLICY_KEYS)
    if unknown:
        raise SelectionConfigError(f"{source}: unknown policy key(s): {', '.join(unknown)}")
    for required in ("schema", "enabled", "version", "mode", "rules"):
        if required not in data:
            raise SelectionConfigError(f"{source}: missing required key {required!r}")

    schema = _as_int(data["schema"], "schema", minimum=1, maximum=SCHEMA_VERSION)
    if schema != SCHEMA_VERSION:
        raise SelectionConfigError(f"unsupported policy schema: {schema}")
    if not isinstance(data["enabled"], bool):
        raise SelectionConfigError("enabled must be a boolean")
    version = _as_int(data["version"], "version", minimum=1)
    mode = data["mode"]
    if mode == "shadow":
        raise SelectionConfigError(
            "shadow mode is not supported; only 'enforce' is implemented"
        )
    if mode != "enforce":
        raise SelectionConfigError(f"unsupported policy mode: {mode!r} (only 'enforce')")

    raw_rules = data["rules"]
    if not isinstance(raw_rules, list) or not raw_rules:
        raise SelectionConfigError("rules must be a non-empty list")
    rules: list[Rule] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_rules):
        where = f"rules[{index}]"
        if not isinstance(raw, dict):
            raise SelectionConfigError(f"{where} must be a mapping")
        unknown = sorted(set(raw) - _RULE_KEYS)
        if unknown:
            raise SelectionConfigError(f"{where}: unknown key(s): {', '.join(unknown)}")
        for required in _RULE_KEYS:
            if required not in raw:
                raise SelectionConfigError(f"{where}: missing required key {required!r}")
        rule_id = _as_text(raw["id"], f"{where}.id")
        if not _RULE_ID_RE.match(rule_id):
            raise SelectionConfigError(
                f"{where}.id must be a stable slug ([a-z0-9._-], max 64 chars)"
            )
        if rule_id in seen_ids:
            raise SelectionConfigError(f"duplicate rule id: {rule_id}")
        seen_ids.add(rule_id)
        outcome = raw["outcome"]
        if outcome not in RULE_OUTCOMES:
            raise SelectionConfigError(
                f"{where}.outcome must be one of {RULE_OUTCOMES}; "
                "defer is a decision with an explicit gap, not a rule"
            )
        evidence = raw["evidence"]
        if evidence not in EVIDENCE_KINDS:
            raise SelectionConfigError(
                f"{where}.evidence must be one of {EVIDENCE_KINDS}"
            )
        rules.append(
            Rule(
                id=rule_id,
                outcome=outcome,
                description=_as_text(raw["description"], f"{where}.description"),
                evidence=evidence,
            )
        )
    if not {r.outcome for r in rules} <= set(RULE_OUTCOMES):
        raise SelectionConfigError("rules must only declare accept/reject outcomes")

    raw_budgets = data.get("budgets")
    if raw_budgets is None:
        raw_budgets = {}
    if not isinstance(raw_budgets, dict):
        raise SelectionConfigError("budgets must be a mapping")
    unknown = sorted(set(raw_budgets) - set(DEFAULT_BUDGETS))
    if unknown:
        raise SelectionConfigError(f"unknown budget key(s): {', '.join(unknown)}")
    budgets = dict(DEFAULT_BUDGETS)
    for key, value in raw_budgets.items():
        budgets[key] = _as_int(value, f"budgets.{key}", minimum=1, maximum=10_000)

    raw_retention = data.get("retention")
    if raw_retention is None:
        raw_retention = {}
    if not isinstance(raw_retention, dict):
        raise SelectionConfigError("retention must be a mapping")
    unknown = sorted(set(raw_retention) - _RETENTION_KEYS)
    if unknown:
        raise SelectionConfigError(f"unknown retention key(s): {', '.join(unknown)}")
    retention = dict(DEFAULT_RETENTION)
    for key, value in raw_retention.items():
        retention[key] = _as_int(value, f"retention.{key}", minimum=1)
    if retention["identity_days"] <= DISCOVERY_LOOKBACK_DAYS:
        raise SelectionConfigError(
            "retention.identity_days must exceed the discovery lookback "
            f"({DISCOVERY_LOOKBACK_DAYS} days), got {retention['identity_days']}"
        )
    if retention["identity_days"] < retention["preview_days"]:
        raise SelectionConfigError(
            "retention.identity_days must be >= retention.preview_days"
        )
    if retention["max_review_attempts"] > 10:
        raise SelectionConfigError("retention.max_review_attempts must be <= 10")

    payload = {
        "schema": SCHEMA_VERSION,
        "enabled": bool(data["enabled"]),
        "version": version,
        "mode": "enforce",
        "rules": [r.as_dict() for r in rules],
        "budgets": budgets,
        "retention": retention,
    }
    digest = "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    return Policy(
        schema=SCHEMA_VERSION,
        enabled=bool(data["enabled"]),
        version=version,
        mode="enforce",
        rules=tuple(rules),
        budgets=budgets,
        retention=retention,
        digest=digest,
        source_path=source,
    )


def load_policy(root: Path | str | None = None, path: Path | str | None = None) -> Policy | None:
    """Load and validate the instance policy.

    Returns ``None`` only when the file is absent or the validated policy has
    ``enabled: false``. An unreadable or invalid file raises
    :class:`SelectionConfigError`: a broken policy is never \"disabled\".
    """
    target = policy_path(root, path)
    if not target.exists():
        return None
    if not target.is_file():
        raise SelectionConfigError(
            f"{target} exists but is not a regular file; "
            "refusing to treat a broken policy path as disabled"
        )
    try:
        data = load_yaml(target)
    except Exception as exc:
        raise SelectionConfigError(f"cannot read {target}: {exc}") from exc
    policy = parse_policy(data, source=str(target))
    return policy if policy.enabled else None


def policy_status(root: Path | str | None = None) -> dict:
    """Describe the policy without raising: status only, never a mutation."""
    target = policy_path(root)
    if not target.exists():
        return {"status": "missing", "enabled": False, "path": str(target)}
    if not target.is_file():
        return {
            "status": "invalid",
            "enabled": False,
            "path": str(target),
            "error": f"{target} exists but is not a regular file",
        }
    try:
        data = load_yaml(target)
        policy = parse_policy(data, source=str(target))
    except SelectionConfigError as exc:
        return {"status": "invalid", "enabled": False, "path": str(target), "error": str(exc)}
    summary = policy.summary()
    summary.update(
        {
            "status": "enabled" if policy.enabled else "disabled",
            "enabled": policy.enabled,
            "path": str(target),
        }
    )
    return summary


# ------------------------------------------------------------------ ledger


def ledger_path(root: Path | str | None = None) -> Path:
    return (Path(root) if root is not None else ROOT) / LEDGER_REL


def preview_dir(root: Path | str | None = None) -> Path:
    return (Path(root) if root is not None else ROOT) / PREVIEW_DIR_REL


def empty_ledger() -> dict:
    return {
        "schema": LEDGER_SCHEMA,
        "records": {},
        "rotation": 0,
        "runs": {},
        "previews": {},
    }


def _reject_record(path: Path, key: str, reason: str) -> None:
    raise SelectionStateError(
        f"selection ledger record {key!r} is invalid ({reason}): {path}"
    )


def _validate_record(path: Path, key: str, rec) -> None:
    """A malformed record is corruption, never an editorial outcome.

    Required bindings are validated so a partially written or hand-edited
    record cannot be read as a healthy reject/defer and silently disappear
    from accounting. The dictionary key, the embedded key and the canonical
    identity of the recorded URL must all name the same candidate: a foreign
    or collapsed identity would otherwise authorize the wrong work.
    """
    if not isinstance(rec, dict):
        _reject_record(path, key, "not a mapping")
    if not isinstance(rec.get("key"), str) or not rec.get("key"):
        _reject_record(path, key, "missing key binding")
    if not isinstance(rec.get("url"), str) or not rec.get("url"):
        _reject_record(path, key, "missing url binding")
    if rec["key"] != key:
        _reject_record(path, key, "dictionary key and embedded key disagree")
    if identity_for_url(rec["url"]) != rec["key"]:
        _reject_record(path, key, "embedded key and canonical URL identity disagree")
    state = rec.get("state")
    if state not in STATES:
        _reject_record(path, key, f"unsupported state {state!r}")
    outcome = rec.get("outcome", "")
    if outcome not in ("",) + OUTCOMES:
        _reject_record(path, key, f"unsupported outcome {outcome!r}")
    if "run_generated_at" not in rec:
        _reject_record(path, key, "missing run binding")
    if not isinstance(rec.get("run_generated_at"), str):
        _reject_record(path, key, "run binding is not a string")
    if "review_attempts" in rec:
        attempts = rec["review_attempts"]
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            _reject_record(path, key, "review_attempts is not a non-negative integer")
    if "history" in rec and not isinstance(rec["history"], list):
        _reject_record(path, key, "history is not a list")
    if "evidence" in rec and not isinstance(rec["evidence"], dict):
        _reject_record(path, key, "evidence is not a mapping")


def _validate_runs(path: Path, runs: dict) -> None:
    """Per-run counters are authoritative: present but malformed fails closed.

    Missing counter history is the documented legacy fallback (usage is then
    derived from recorded decisions). A present boolean, negative, fractional or
    non-integer counter is corruption and is never coerced to zero, because that
    would silently refund consumed model/archive work.
    """
    for run_gen, entry in runs.items():
        if not isinstance(entry, dict):
            raise SelectionStateError(
                f"selection ledger run {run_gen!r} is not a mapping: {path}"
            )
        for name in ("judgments", "archives"):
            if name not in entry:
                continue
            value = entry[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SelectionStateError(
                    f"selection ledger run {run_gen!r} {name} is not a "
                    f"non-negative integer: {path}"
                )


def load_ledger(root: Path | str | None = None) -> dict:
    """Read the authoritative ledger; corrupt state raises, never empty."""
    path = ledger_path(root)
    if not path.exists():
        return empty_ledger()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SelectionStateError(f"selection ledger is unreadable: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SelectionStateError(f"selection ledger is not a JSON object: {path}")
    schema = data.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != LEDGER_SCHEMA:
        raise SelectionStateError(
            f"selection ledger schema {data.get('schema')!r} is not {LEDGER_SCHEMA}: {path}"
        )
    records = data.get("records")
    if not isinstance(records, dict):
        raise SelectionStateError(f"selection ledger records is not a mapping: {path}")
    for key, rec in records.items():
        _validate_record(path, str(key), rec)
    runs = data.get("runs")
    if "runs" in data:
        if not isinstance(runs, dict):
            raise SelectionStateError(f"selection ledger runs is not a mapping: {path}")
        _validate_runs(path, runs)
    for name in ("previews",):
        if name in data and not isinstance(data[name], dict):
            raise SelectionStateError(f"selection ledger {name} is not a mapping: {path}")
    data.setdefault("rotation", 0)
    data.setdefault("runs", {})
    data.setdefault("previews", {})
    return data


def save_ledger(root: Path | str | None, ledger: dict) -> None:
    ledger["schema"] = LEDGER_SCHEMA
    runs = ledger.get("runs")
    if isinstance(runs, dict) and len(runs) > MAX_RUN_HISTORY:
        keep = sorted(str(key) for key in runs)[-MAX_RUN_HISTORY:]
        ledger["runs"] = {key: runs[key] for key in keep}
    ledger["updated_at"] = iso()
    write_json_atomic(ledger_path(root), ledger)


def _run_entry(ledger: dict, run_gen: str) -> dict:
    runs = ledger.setdefault("runs", {})
    entry = runs.get(run_gen)
    if not isinstance(entry, dict):
        entry = {}
        runs[run_gen] = entry
    return entry


def _judgment_count(records: dict, run_gen: str) -> int:
    return sum(
        1
        for rec in records.values()
        if isinstance(rec, dict) and rec.get("decided_run") == run_gen and rec.get("outcome")
    )


def _admitted_count(records: dict, run_gen: str) -> int:
    return sum(
        1
        for rec in records.values()
        if isinstance(rec, dict)
        and rec.get("outcome") == "accept"
        and rec.get("admitted_run") == run_gen
    )


def run_judgments_used(ledger: dict, run_gen: str) -> int:
    """Monotonic judgment usage for one run.

    The stored counter survives a reassessment that clears the current
    decision, so re-deciding can never refund a model-work slot. Older ledgers
    without a counter fall back to the recorded decisions.
    """
    entry = (ledger.get("runs") or {}).get(run_gen)
    stored = entry.get("judgments") if isinstance(entry, dict) else 0
    stored = stored if isinstance(stored, int) and not isinstance(stored, bool) else 0
    return max(stored, _judgment_count(ledger.get("records") or {}, run_gen))


def run_archives_used(ledger: dict, run_gen: str) -> int:
    entry = (ledger.get("runs") or {}).get(run_gen)
    stored = entry.get("archives") if isinstance(entry, dict) else 0
    stored = stored if isinstance(stored, int) and not isinstance(stored, bool) else 0
    return max(stored, _admitted_count(ledger.get("records") or {}, run_gen))


def _bump_run(ledger: dict, run_gen: str, *, judgments: int = 0, archives: int = 0) -> None:
    """Record work before the outcome it belongs to is written.

    Callers bump before mutating the record: the counter can then never be
    refunded by clearing a decision, and it never double counts the same
    decision.
    """
    entry = _run_entry(ledger, run_gen)
    entry["judgments"] = run_judgments_used(ledger, run_gen) + judgments
    entry["archives"] = run_archives_used(ledger, run_gen) + archives



def _set_run_archives(ledger: dict, run_gen: str, used: int) -> None:
    entry = _run_entry(ledger, run_gen)
    entry["archives"] = max(int(entry.get("archives") or 0), used)



def _with_ledger(root, mutate):
    """Lock, re-read, mutate, write. ``mutate`` returns ``(result, changed)``."""
    with state_lock(Path(root) if root is not None else ROOT):
        ledger = load_ledger(root)
        result, changed = mutate(ledger)
        if changed:
            save_ledger(root, ledger)
    return result


def counts_of(ledger: dict) -> dict:
    outcomes: dict[str, int] = {}
    states: dict[str, int] = {}
    for rec in (ledger.get("records") or {}).values():
        if not isinstance(rec, dict):
            continue
        outcome = str(rec.get("outcome") or "none")
        state = str(rec.get("state") or "none")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        states[state] = states.get(state, 0) + 1
    return {"outcomes": outcomes, "states": states}


def status(root: Path | str | None = None, policy: Policy | None = None) -> dict:
    """Aggregate policy + ledger status. Read-only; never creates state."""
    info = policy_status(root)
    if policy is not None:
        active = policy
    elif info.get("status") == "invalid":
        # Status still reports a machine-readable invalid policy; the command
        # stays visibly unsuccessful through its exit code.
        active = None
    else:
        active = load_policy(root)
    out = {
        "enabled": bool(active and active.enabled),
        "policy": info,
        "records": 0,
        "counts": {},
        "budgets": active.budgets if active else dict(DEFAULT_BUDGETS),
        "retention": active.retention if active else dict(DEFAULT_RETENTION),
        "next_action": "fix selection.yaml before enabling automatic selection"
        if info.get("status") == "invalid"
        else "none",
    }
    if info.get("status") == "invalid":
        out["invalid"] = True
        out["open"] = 0
        return out
    if not active or not active.enabled:
        return out
    ledger = load_ledger(root)
    records = ledger.get("records") or {}
    counts = counts_of(ledger)
    out["records"] = len(records)
    out["counts"] = counts["outcomes"]
    out["states"] = counts["states"]
    out["capacity"] = {"used": len(records), "limit": active.retention["capacity"]}
    out["open"] = sum(
        n for state, n in counts["states"].items() if state in OPEN_STATES
    )
    overview = ledger_overview(ledger, active)
    out["unresolved"] = len(overview["unresolved"])
    out["expired_unresolved"] = len(overview["expired_unresolved"])
    out["needs_reassessment"] = len(overview["needs_reassessment"])
    out["open_detail"] = {
        "counts": overview["open_counts"],
        "oldest_open_days": overview["oldest_open_days"],
        "stalled": overview["stalled"],
        "review_limit": overview["review_limit"],
        "overflow": sorted(
            key
            for key, rec in records.items()
            if isinstance(rec, dict) and rec.get("queue_overflow")
        ),
    }
    out["next_action"] = overview["next_action"]
    return out



# ------------------------------------------------------------- identity/run


def identity_for_url(url: str) -> str:
    """Exact candidate identity: a real documentation fragment is retained."""
    url = (url or "").strip()
    if not url:
        return ""
    if urlsplit(url).fragment:
        return canonicalize_url(url, keep_fragment=True)
    return dedupe_key(url)


def identity_for_candidate(cand: dict) -> str:
    return identity_for_url(str((cand or {}).get("url") or ""))


def metadata_text(cand: dict) -> str:
    """Recorded feed metadata only -- never the article body."""
    parts = [
        cand.get("title"),
        cand.get("summary"),
        cand.get("source"),
        cand.get("published"),
        cand.get("published_original"),
    ]
    return "\n".join(str(p) for p in parts if p)


def observed_fingerprint(cand: dict) -> str:
    payload = "|".join(
        str(cand.get(field) or "")
        for field in ("title", "summary", "published", "source", "source_id")
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def run_path_default(root: Path | str | None = None) -> Path:
    return (Path(root) if root is not None else ROOT) / ".state" / "runs" / "latest-fetch.json"


def load_run(path: Path | str | None = None, *, root: Path | str | None = None) -> dict:
    target = Path(path) if path is not None else run_path_default(root)
    if not target.is_file():
        raise SelectionStateError(f"current discovery run not found: {target}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SelectionStateError(f"discovery run is unreadable: {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise SelectionStateError(f"discovery run is not a JSON object: {target}")
    if not str(data.get("generated_at") or "").strip():
        raise SelectionStateError(f"discovery run has no generated_at: {target}")
    if not isinstance(data.get("candidates", []), list):
        raise SelectionStateError(f"discovery run candidates is not a list: {target}")
    data.setdefault("candidates", [])
    return data


def find_candidate(run: dict, *, key: str = "", url: str = "") -> dict | None:
    wanted = key or identity_for_url(url)
    if not wanted:
        return None
    for cand in run.get("candidates") or []:
        if isinstance(cand, dict) and identity_for_candidate(cand) == wanted:
            return cand
    return None


def register_discovered(
    root: Path | str | None,
    policy: Policy,
    *,
    run_path: Path | str | None = None,
    run_data: dict | None = None,
    candidates: list[dict] | None = None,
    dry_run: bool = False,
) -> dict:
    """Record discovered candidates in the ledger as ``awaiting_judgment``.

    Called by ``fetch_sources.py`` inside its commit lock. A terminal record is
    never requeued: changed metadata only marks it ``reassessment_required``.
    """
    if not policy or not policy.enabled:
        raise SelectionDisabledError("selection policy is disabled")
    run = run_data if run_data is not None else load_run(run_path, root=root)
    gen = str(run.get("generated_at") or "").strip()
    cands = candidates if candidates is not None else [
        c for c in (run.get("candidates") or []) if isinstance(c, dict)
    ]
    capacity = policy.retention["capacity"]

    def mutate(ledger: dict):
        stats = _register_into(ledger.setdefault("records", {}), policy, gen, cands)
        stats["run"] = gen
        stats["candidates"] = len(cands)
        return stats, not dry_run

    if dry_run:
        ledger = load_ledger(root)
        return _mutate_without_write(ledger, mutate)
    return _with_ledger(root, mutate)


def _mutate_without_write(ledger: dict, mutate):
    result, _changed = mutate(ledger)
    return result


_CANDIDATE_PAYLOAD_FIELDS = (
    "url",
    "title",
    "summary",
    "published",
    "published_original",
    "source",
    "source_id",
    "extractor",
    "content_file",
    "raw_file",
    "cache_name",
    "dedupe_keys",
    "section_id",
)


def _candidate_payload(cand: dict) -> dict:
    """Recorded candidate metadata kept so recovered work stays processable."""
    payload = {}
    for field in _CANDIDATE_PAYLOAD_FIELDS:
        value = cand.get(field)
        if value in (None, ""):
            continue
        payload[field] = value
    return payload


def _register_into(records: dict, policy: Policy, run_gen: str, cands: list[dict]) -> dict:
    """Shared registration arithmetic; the caller owns the lock and the save."""
    capacity = policy.retention["capacity"]
    new = refreshed = changed_observed = terminal = 0
    stamp_now = iso()
    for cand in cands:
        if not isinstance(cand, dict):
            continue
        key = identity_for_candidate(cand)
        if not key:
            continue
        fp = observed_fingerprint(cand)
        rec = records.get(key)
        if rec is None:
            if len(records) >= capacity:
                raise SelectionCapacityError(
                    f"selection ledger capacity {capacity} reached; "
                    "refusing new state (no suppression identity was evicted)"
                )
            records[key] = {
                "key": key,
                **_candidate_payload(cand),
                "url": str(cand.get("url") or ""),
                "source_id": str(cand.get("source_id") or ""),
                "source": str(cand.get("source") or ""),
                "title": str(cand.get("title") or ""),
                "published": str(cand.get("published") or ""),
                "outcome": "",
                "state": STATE_AWAITING,
                "policy_digest": policy.digest,
                "policy_version": policy.version,
                "run_generated_at": run_gen,
                "first_seen": stamp_now,
                "queued_at": stamp_now,
                "updated_at": stamp_now,
                "observed_fingerprint": fp,
                "review_attempts": 0,
                "history": [],
            }
            new += 1
            continue
        state = rec.get("state")
        if state in OPEN_STATES:
            if state in ACCEPTED_OPEN_STATES:
                # Evidence binding of a decided-but-open record is preserved:
                # a later observation difference asks for explicit reassessment
                # instead of being refreshed away.
                if (
                    rec.get("observed_fingerprint")
                    and rec["observed_fingerprint"] != fp
                ):
                    changed_observed += 1
                    rec["reassessment_required"] = True
                    rec["reassessment_reason"] = "observed_content_changed"
                    rec["reassessment_observed_fingerprint"] = fp
                    rec["updated_at"] = stamp_now
                else:
                    refreshed += 1
                continue
            update = {
                **_candidate_payload(cand),
                "url": str(cand.get("url") or rec.get("url") or ""),
                "source_id": str(cand.get("source_id") or rec.get("source_id") or ""),
                "source": str(cand.get("source") or rec.get("source") or ""),
                "title": str(cand.get("title") or rec.get("title") or ""),
                "published": str(cand.get("published") or rec.get("published") or ""),
                "run_generated_at": run_gen,
                "observed_fingerprint": fp,
                "updated_at": stamp_now,
            }
            if state == STATE_AWAITING:
                # No decision exists yet, so a policy change is not a reuse.
                update["policy_digest"] = policy.digest
                update["policy_version"] = policy.version
            elif state == STATE_JUDGMENT_WAIT:
                # A later run has a fresh judgment budget; durable wait work
                # becomes processable again without losing its history.
                if str(rec.get("run_generated_at") or "") != run_gen:
                    update["state"] = STATE_AWAITING
                    update["policy_digest"] = policy.digest
                    update["policy_version"] = policy.version
            rec.update(update)
            refreshed += 1
            continue
        terminal += 1
        if rec.get("observed_fingerprint") and rec["observed_fingerprint"] != fp:
            changed_observed += 1
            rec["reassessment_required"] = True
            rec["reassessment_reason"] = "observed_content_changed"
            rec["reassessment_observed_fingerprint"] = fp
            rec["updated_at"] = stamp_now
        rec["observed_fingerprint"] = fp
    return {
        "new": new,
        "refreshed": refreshed,
        "terminal": terminal,
        "changed_observed": changed_observed,
        "records": len(records),
    }



def suppressed_keys(ledger: dict) -> set[str]:
    """Terminal identities that automatic discovery must not resurrect.

    Execution expiry is a terminal *execution* state, not an editorial reject:
    it suppresses automatic rediscovery (so an expired identity cannot get a
    fresh TTL every run) while an explicit ``reassess`` can still reopen it.
    """
    keys: set[str] = set()
    for key, rec in (ledger.get("records") or {}).items():
        if not isinstance(rec, dict):
            continue
        if (
            rec.get("outcome") in ("reject", "defer")
            or rec.get("state") in (STATE_ARCHIVED, STATE_EXECUTION_EXPIRED)
        ):
            keys.add(key)
    return keys


def owed_keys(ledger: dict) -> set[str]:
    """Open work that the pending-retry TTL must not discard."""
    return {
        key
        for key, rec in (ledger.get("records") or {}).items()
        if isinstance(rec, dict) and rec.get("state") in OWED_STATES
    }


def pending_queue_limit() -> int:
    """The pending bound owned by ``fetch_sources`` (single queue)."""
    try:
        import fetch_sources as fs

        return int(fs.PENDING_LIMIT)
    except Exception:  # pragma: no cover - optional module without fetch
        return 300


def _recovery_pending_record(rec: dict) -> dict:
    """Requeue owed work with its recorded payload and first enqueue age."""
    stamp_now = iso()
    queued = str(rec.get("queued_at") or rec.get("first_seen") or "") or stamp_now
    payload = {
        key: rec[key]
        for key in _CANDIDATE_PAYLOAD_FIELDS
        if rec.get(key) not in (None, "")
    }
    payload.setdefault("url", str(rec.get("url") or ""))
    payload.setdefault("source_id", str(rec.get("source_id") or ""))
    payload.update(
        {
            "reason": "ledger_recovery",
            "queued_at": queued,
            "parked_at": stamp_now,
            "updated_at": stamp_now,
        }
    )
    return payload



def _seen_evidence(key: str, url: str, seen_keys: set[str]) -> bool:
    """Exact identity first: another section's generic URL is not proof.

    A fragment URL is its own candidate; only that exact identity (or its
    recorded dedupe alias) may close it. Non-section candidates keep the
    historic dedupe-key match used by ``archive_url.py``.
    """
    if not seen_keys:
        return False
    dedupe = dedupe_key(url)
    if key != dedupe and urlsplit(url).fragment:
        return key in seen_keys
    return key in seen_keys or (bool(dedupe) and dedupe in seen_keys)


def _mark_overflow(rec: dict, stamp_now: str) -> bool:
    if rec.get("queue_overflow"):
        return False
    rec["queue_overflow"] = True
    rec["queue_overflow_at"] = stamp_now
    rec["updated_at"] = stamp_now
    return True


def apply_commit(
    ledger: dict,
    policy: Policy,
    *,
    run_gen: str,
    candidates: list[dict],
    observed: list[dict] | None = None,
    merged: dict,
    evicted: list,
    dry_run: bool = False,
    seen_keys: set[str] | None = None,
    expired_keys: set[str] | None = None,
) -> dict:
    """Synchronise the pending queue with the durable ledger.

    Called by ``fetch_sources.commit_states`` while it already holds
    ``.state/.lock``: this function never locks or writes by itself. It

    * removes pending keys with a terminal reject/defer/archived identity;
    * records a deliberate TTL expiry as a non-editorial execution state;
    * closes automatic queue work whose identity is now in ``seen.json``;
    * re-derives queue work for every owed open ledger record the pending delta
      lost (crash between the ledger and queue writes), keeping the recorded
      payload and first enqueue age;
    * re-applies the pending bound and keeps overflow explicit in the ledger;
    * observes every discovered candidate so a changed title/summary on a
      terminal identity is reported as needing reassessment (never requeued);
    * registers this run's discovered candidates.
    """
    records = ledger.setdefault("records", {})
    terminal = suppressed_keys(ledger)
    seen = set(seen_keys or ())
    expired = set(expired_keys or ())
    suppressed = 0
    for key in list(merged.keys()):
        if key in terminal:
            rec = merged.pop(key)
            evicted.append((key, rec, "selection_terminal"))
            suppressed += 1

    stamp_now = iso()
    expired_count = 0
    for key in expired:
        rec = records.get(key)
        if not isinstance(rec, dict) or rec.get("state") not in (
            STATE_AWAITING,
            STATE_JUDGMENT_WAIT,
        ):
            continue
        history = list(rec.get("history") or [])
        history.append({"action": "execution_expired", "at": stamp_now})
        rec.update(
            {
                "state": STATE_EXECUTION_EXPIRED,
                "execution_expired_at": stamp_now,
                "queued_at": rec.get("queued_at") or "",
                "history": history[-MAX_HISTORY:],
                "updated_at": stamp_now,
            }
        )
        expired_count += 1

    seen_closed = 0
    if seen:
        # Durable seen evidence closes any eligible open execution state. That
        # includes accepted ready/budget_wait/archive_failure work: a crash after
        # the seen write and before mark_archived must not leave a phantom open
        # obligation. The editorial outcome and history are preserved.
        closable = (STATE_AWAITING, STATE_JUDGMENT_WAIT) + ACCEPTED_OPEN_STATES
        for key, rec in records.items():
            if not isinstance(rec, dict):
                continue
            if rec.get("state") not in closable:
                continue
            if not _seen_evidence(key, str(rec.get("url") or ""), seen):
                continue
            history = list(rec.get("history") or [])
            history.append({"action": "seen_archive", "at": stamp_now})
            rec.update(
                {
                    "state": STATE_ARCHIVED,
                    "archived_at": stamp_now,
                    "archive_evidence": "seen",
                    "history": history[-MAX_HISTORY:],
                    "updated_at": stamp_now,
                }
            )
            # Seen proof supersedes stale attention markers for this identity.
            rec.pop("queue_overflow", None)
            rec.pop("queue_overflow_at", None)
            rec.pop("failure_reason", None)
            merged.pop(key, None)
            seen_closed += 1

    stats = _register_into(records, policy, run_gen, candidates)
    observed_changes = 0
    if observed:
        for cand in observed:
            if not isinstance(cand, dict):
                continue
            key = identity_for_candidate(cand)
            rec = records.get(key)
            if rec is None or rec.get("state") in OPEN_STATES:
                continue
            fp = observed_fingerprint(cand)
            if rec.get("observed_fingerprint") and rec["observed_fingerprint"] != fp:
                observed_changes += 1
                rec["reassessment_required"] = True
                rec["reassessment_reason"] = "observed_content_changed"
                rec["reassessment_observed_fingerprint"] = fp
                rec["updated_at"] = iso()
            rec["observed_fingerprint"] = fp

    # Deliberate limit evictions from the merge are not crashes: never undo them.
    # They are, however, still outstanding accepted/open work: the corresponding
    # ledger record must keep its payload and enqueue age and stay explicitly
    # marked as overflow instead of silently disappearing from the queue.
    evicted_keys = {str(entry[0]) for entry in evicted if entry}
    evicted_overflow = 0
    for entry in evicted:
        if not entry:
            continue
        key = str(entry[0])
        reason = str(entry[2]) if len(entry) > 2 else ""
        target = records.get(key)
        if not isinstance(target, dict) or target.get("state") not in OPEN_STATES:
            continue
        if reason == "selection_terminal":
            continue
        if _mark_overflow(target, stamp_now):
            evicted_overflow += 1
    recoverable: list[tuple[bool, str, str, dict]] = []
    for key, rec in records.items():
        if not isinstance(rec, dict):
            continue
        if rec.get("state") not in OPEN_STATES:
            continue
        if key in merged or key in terminal or key in seen or key in evicted_keys:
            continue
        if not rec.get("url"):
            continue
        recoverable.append(
            (
                bool(rec.get("queue_overflow")),
                str(rec.get("queued_at") or rec.get("first_seen") or ""),
                key,
                rec,
            )
        )
    recoverable.sort(key=lambda item: (item[0], item[1], item[2]))

    limit = pending_queue_limit()
    room = max(limit - len(merged), 0)
    recovered = 0
    overflow = evicted_overflow
    for _was_overflow, _queued, key, rec in recoverable:
        if room > 0:
            merged[key] = _recovery_pending_record(rec)
            room -= 1
            recovered += 1
            continue
        if _mark_overflow(rec, stamp_now):
            overflow += 1


    # A record that is genuinely in the queue no longer reports overflow.
    for key in merged:
        rec = records.get(key)
        if isinstance(rec, dict) and rec.get("queue_overflow"):
            rec.pop("queue_overflow", None)
            rec.pop("queue_overflow_at", None)
            rec["updated_at"] = stamp_now

    if len(merged) > limit:
        ordered = sorted(
            merged.items(),
            key=lambda kv: str(kv[1].get("queued_at") or ""),
            reverse=True,
        )
        merged.clear()
        merged.update(ordered[:limit])
        for key, rec in ordered[limit:]:
            evicted.append((key, rec, "over_limit"))
            target = records.get(key)
            if isinstance(target, dict):
                _mark_overflow(target, stamp_now)

    return {
        "enabled": True,
        "run": run_gen,
        "policy_version": policy.version,
        "policy_digest": policy.digest,
        "suppressed": suppressed,
        "recovered": recovered,
        "overflow": overflow,
        "expired": expired_count,
        "seen_closed": seen_closed,
        "registered": stats["new"],
        "refreshed": stats["refreshed"],
        "terminal": stats["terminal"],
        "changed_observed": stats["changed_observed"] + observed_changes,
        "records": stats["records"],
        "dry_run": bool(dry_run),
    }



# ------------------------------------------------------------------ preview


def _read_allowed_file(path: Path, *, limit: int = 5 * 1024 * 1024) -> str:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise SelectionStateError(f"content file not found: {path}")
    if not is_allowed_file_path(resolved):
        raise SelectionStateError(f"content file path not allowed: {path}")
    if resolved.stat().st_size > limit:
        raise SelectionStateError(f"content file exceeds {limit} bytes: {path}")
    try:
        return resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SelectionStateError(f"content file is not UTF-8: {path}: {exc}") from exc


def _fetch_page_text(url: str) -> str:
    """Fetch a page through the existing SSRF/redirect/size boundary."""
    try:
        import fetch_sources as fs
    except Exception as exc:  # pragma: no cover - install error
        raise SelectionStateError(f"direct preview fetch unavailable: {exc}") from exc
    try:
        html = fs.fetch_text(url)
    except Exception as exc:
        raise SelectionStateError(f"preview fetch failed: {exc}") from exc
    try:
        import archive_url as au

        _meta, content = au.generic_extract(html, url)
    except Exception as exc:
        raise SelectionStateError(f"preview extraction failed: {exc}") from exc
    return str(content or "").strip()


def _preview_stem(key: str, body_hash: str) -> str:
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    body_hex = body_hash.split(":", 1)[-1][:16]
    return f"{key_hash}-{body_hex}"


def _preview_allowed_path(root, path) -> Path | None:
    """Resolve a preview cache path, refusing anything outside the subtree."""
    pdir = preview_dir(root)
    try:
        base = pdir.resolve()
        target = Path(str(path)).expanduser().resolve()
    except Exception:
        return None
    if target == base or base not in target.parents:
        return None
    if not target.is_file():
        return None
    return target


def _preview_binding(root, policy: Policy, key: str, run_gen: str) -> tuple[str, dict] | None:
    """Return the ledger's *current* preview body + its binding.

    The authoritative ledger records which same-run preview is current, so a
    second preview of the same key cannot be shadowed by whichever body-hash
    filename happens to sort first.
    """
    ledger = load_ledger(root)
    meta = (ledger.get("previews") or {}).get(key)
    if not isinstance(meta, dict):
        return None
    if (
        meta.get("key") != key
        or meta.get("policy_digest") != policy.digest
        or meta.get("run_generated_at") != run_gen
    ):
        return None
    target = _preview_allowed_path(root, meta.get("path"))
    if target is None:
        return None
    try:
        text = target.read_text(encoding="utf-8")
    except Exception:
        return None
    if "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest() != meta.get("body_hash"):
        return None
    recorded = dict(meta)
    recorded["path"] = str(target)
    return text, recorded



def preview(
    root: Path | str | None,
    policy: Policy,
    *,
    url: str = "",
    key: str = "",
    run_path: Path | str | None = None,
    run_data: dict | None = None,
    content_file: Path | str | None = None,
    text: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Prepare bounded preview text. Never archives, localizes or publishes."""
    if not policy or not policy.enabled:
        raise SelectionDisabledError("selection policy is disabled")
    run = None
    try:
        run = run_data if run_data is not None else load_run(run_path, root=root)
    except SelectionStateError:
        run = None
    cand = find_candidate(run, key=key, url=url) if run else None
    resolved_url = (url or (cand or {}).get("url") or "").strip()
    resolved_key = key or (
        identity_for_candidate(cand) if cand else identity_for_url(resolved_url)
    )
    if not resolved_key:
        raise SelectionStateError("preview needs --url or --key")
    run_gen = str((run or {}).get("generated_at") or "")
    source_id = str((cand or {}).get("source_id") or "")
    source = str((cand or {}).get("source") or "")

    body: str | None = None
    scope = "page"
    if text is not None:
        body = text
        scope = "section" if urlsplit(resolved_url).fragment else "document"
    elif content_file:
        body = _read_allowed_file(Path(content_file))
        scope = "section" if urlsplit(resolved_url).fragment else "document"
    elif cand and cand.get("content_file"):
        body = _read_allowed_file(Path(str(cand["content_file"])))
        scope = "section" if urlsplit(resolved_url).fragment else "document"
    if body is None:
        if not resolved_url:
            raise SelectionStateError("preview needs a URL or an existing content file")
        body = _fetch_page_text(resolved_url)
        scope = "page"
    body = body.strip()
    if not body:
        raise SelectionStateError("preview body is empty")

    truncated = len(body) > MAX_PREVIEW_CHARS
    if truncated:
        body = body[:MAX_PREVIEW_CHARS].rstrip()
    body_hash = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    binding = {
        "key": resolved_key,
        "url": resolved_url,
        "source_id": source_id,
        "source": source,
        "body_hash": body_hash,
        "policy_digest": policy.digest,
        "policy_version": policy.version,
        "run_generated_at": run_gen,
        "scope": scope,
        "chars": len(body),
        "truncated": truncated,
        "completeness": "unknown",
        "created_at": iso(),
    }

    cache_hit = False
    preview_path = ""
    if not dry_run:
        pdir = preview_dir(root)
        pdir.mkdir(parents=True, exist_ok=True)
        stem = _preview_stem(resolved_key, body_hash)
        target = pdir / f"{stem}.md"
        sidecar = pdir / f"{stem}.json"
        if target.is_file() and sidecar.is_file():
            try:
                existing = json.loads(sidecar.read_text(encoding="utf-8"))
                cache_hit = (
                    isinstance(existing, dict)
                    and existing.get("body_hash") == body_hash
                    and existing.get("policy_digest") == policy.digest
                    and existing.get("run_generated_at") == run_gen
                    # A tampered cache file is never reused as fresh evidence.
                    and "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
                    == body_hash
                )
            except Exception:
                cache_hit = False
        if not cache_hit:
            # Exact bytes only: the body hash is the reuse contract, so the
            # on-disk file must hash identically to the preview text.
            write_text_atomic(target, body)
            binding["path"] = str(target)
            write_json_atomic(sidecar, binding)
        else:
            binding["path"] = str(target)
        preview_path = str(target)
        # The ledger names the current preview for this key/run/policy, so the
        # decision command never has to guess between same-run sidecars.
        def remember(ledger: dict):
            ledger.setdefault("previews", {})[resolved_key] = dict(binding)
            return {"key": resolved_key}, True

        _with_ledger(root, remember)
    else:
        binding["path"] = ""


    return {
        "status": "ok",
        "key": resolved_key,
        "url": resolved_url,
        "source_id": source_id,
        "source": source,
        "scope": scope,
        "chars": len(body),
        "truncated": truncated,
        "body_hash": body_hash,
        "cache_hit": cache_hit,
        "preview_path": preview_path,
        "policy_digest": policy.digest,
        "policy_version": policy.version,
        "run_generated_at": run_gen,
        "completeness": "unknown",
        "dry_run": bool(dry_run),
        "text": body,
    }


# ------------------------------------------------------------------- decide


def _decision_fingerprint(*, outcome: str, rule_id: str, reason: str, evidence: dict) -> str:
    payload = {
        "outcome": outcome,
        "rule_id": rule_id,
        "reason": " ".join(str(reason or "").split()),
        "evidence": evidence.get("fingerprint") or "",
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()



def _check_quotes(rule: Rule, quotes: list[str], meta: str, body: str | None, body_hash: str) -> dict:
    normalized_quotes: list[str] = []
    for quote in quotes:
        norm = _normalize(quote)
        if len(norm) < MIN_QUOTE_CHARS:
            raise SelectionStateError(
                f"evidence quote is too short (min {MIN_QUOTE_CHARS} characters): {quote!r}"
            )
        if norm in normalized_quotes:
            continue
        if rule.evidence == "metadata":
            ok = norm in meta
        elif rule.evidence == "body":
            if body is None:
                raise SelectionStateError(
                    "this rule requires body evidence; run `select preview` first "
                    "(or pass a recorded content file)"
                )
            ok = norm in body
        else:
            ok = norm in meta or (body is not None and norm in body)
        if not ok:
            surface = {
                "metadata": "recorded metadata",
                "body": "recorded preview body",
                "either": "recorded metadata or preview body",
            }[rule.evidence]
            raise SelectionStateError(
                f"evidence quote does not occur in the {surface}: {quote!r}"
            )
        normalized_quotes.append(norm)
    if not normalized_quotes:
        raise SelectionStateError("at least one --evidence quote is required")
    payload = {
        "kind": rule.evidence,
        "quotes": sorted(normalized_quotes),
        "body_hash": body_hash if rule.evidence != "metadata" else "",
    }
    return {
        "kind": rule.evidence,
        "quotes": normalized_quotes,
        "body_hash": body_hash if rule.evidence != "metadata" else "",
        "fingerprint": "sha256:" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def decide(
    root: Path | str | None,
    policy: Policy,
    *,
    key: str,
    outcome: str,
    reason: str,
    quotes: list[str],
    rule_id: str = "",
    gap: str = "",
    follow_up: str = "",
    run_path: Path | str | None = None,
    run_data: dict | None = None,
    body: str | None = None,
    dry_run: bool = False,
) -> dict:
    if not policy or not policy.enabled:
        raise SelectionDisabledError("selection policy is disabled")
    if outcome not in OUTCOMES:
        raise SelectionStateError(f"outcome must be one of {OUTCOMES}")
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise SelectionStateError("a decision needs a non-empty --reason")

    run = run_data if run_data is not None else load_run(run_path, root=root)
    gen = str(run.get("generated_at") or "").strip()
    cand = find_candidate(run, key=key)
    if cand is None:
        raise SelectionStateError(
            f"{key!r} is not part of the current run ({gen}); "
            "re-run discovery or use the current latest-fetch.json"
        )

    if outcome == "defer":
        gap = " ".join(str(gap or "").split())
        follow_up = " ".join(str(follow_up or "").split())
        if not gap or not follow_up:
            raise SelectionStateError(
                "defer requires an explicit --gap (what information is missing) "
                "and --follow-up (what would resolve it)"
            )
        rule = None
        if rule_id:
            raise SelectionStateError("defer is not a policy rule; omit --rule")
    else:
        if not rule_id:
            raise SelectionStateError(f"{outcome} requires --rule <rule-id>")
        rule = policy.rule(rule_id)
        if rule.outcome != outcome:
            raise SelectionStateError(
                f"rule {rule_id!r} declares outcome {rule.outcome!r}, not {outcome!r}"
            )

    meta_text = _normalize(metadata_text(cand))
    if body is None:
        valid = _preview_binding(root, policy, key, gen)
        if valid is not None:
            body, _meta = valid
    body_norm = _normalize(body) if body is not None else None
    body_hash = (
        "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest() if body is not None else ""
    )
    cand_fingerprint = observed_fingerprint(cand)
    cand_source_id = str(cand.get("source_id") or "")


    # A defer still needs one checkable quote; a gap alone is not evidence.
    evidence_rule = rule if rule is not None else Rule(
        id="defer", outcome="defer", description="deferred decision", evidence="either"
    )
    evidence = _check_quotes(evidence_rule, list(quotes or []), meta_text, body_norm, body_hash)
    fingerprint = _decision_fingerprint(
        outcome=outcome, rule_id=rule_id, reason=reason, evidence=evidence
    )

    def mutate(ledger: dict):
        records = ledger.setdefault("records", {})
        rec = records.get(key)
        if rec is None:
            raise SelectionStateError(
                f"{key!r} is not registered in the selection ledger; run the fetch step first"
            )
        existing_outcome = str(rec.get("outcome") or "")
        if existing_outcome:
            if (
                existing_outcome == outcome
                and str(rec.get("rule_id") or "") == rule_id
                and rec.get("decision_fingerprint") == fingerprint
            ):
                payload = {
                    "status": "idempotent",
                    "key": key,
                    "outcome": existing_outcome,
                    "state": rec.get("state"),
                    "rule_id": rec.get("rule_id") or "",
                }
                if str(rec.get("policy_digest") or "") != policy.digest:
                    payload["reassessment_required"] = True
                    payload["reason"] = "policy_changed"
                return payload, False
            raise SelectionStateError(
                f"{key!r} already has a {existing_outcome!r} decision; "
                "a contradictory decision needs explicit reassessment "
                "(`select reassess --key ... --apply`)"
            )
        # Identity, source and observed-metadata bindings are re-checked against
        # the registered record under the lock: the same timestamp/key is not
        # proof that the candidate text is unchanged.
        if str(rec.get("source_id") or "") != cand_source_id:
            raise SelectionStateError(
                "candidate source changed after registration; "
                "re-run the fetch step before deciding"
            )
        recorded_fp = str(rec.get("observed_fingerprint") or "")
        if recorded_fp and recorded_fp != cand_fingerprint:
            raise SelectionStateError(
                "candidate metadata changed after registration; "
                "re-run the fetch step before deciding"
            )
        if rec.get("reassessment_required"):
            raise SelectionStateError(
                "candidate needs explicit reassessment "
                f"({rec.get('reassessment_reason') or 'observed_content_changed'}) "
                "before a new decision"
            )
        bound = str(rec.get("run_generated_at") or "")
        if bound != gen:
            raise SelectionStateError(
                f"stale run: candidate was registered for run {bound!r}, "
                f"but --run is {gen!r}; re-run discovery"
            )
        if str(rec.get("policy_digest") or "") != policy.digest:
            raise SelectionStateError(
                "selection policy changed after this candidate was registered; "
                "re-run the fetch step to rebind, or reassess an existing decision"
            )
        if body_hash:
            current = _preview_binding(root, policy, key, gen)
            current_hash = current[1]["body_hash"] if current is not None else ""
            if current_hash != body_hash:
                raise SelectionStateError(
                    "preview changed while deciding; re-run `select preview` "
                    "and decide against the current body"
                )
        judgments_used = run_judgments_used(ledger, gen)
        if judgments_used >= policy.budgets["judgments"]:
            raise SelectionBudgetError(
                f"judgment budget exhausted for this run "
                f"({judgments_used}/{policy.budgets['judgments']}); "
                "remaining candidates stay awaiting_judgment"
            )

        state = {
            "accept": STATE_READY,
            "reject": STATE_REJECTED,
            "defer": STATE_DEFERRED,
        }[outcome]
        admitted_run = ""
        admitted_at = ""
        admitted = False
        archives_used = run_archives_used(ledger, gen)
        if outcome == "accept":
            if archives_used >= policy.budgets["archives"]:
                state = STATE_BUDGET_WAIT
            else:
                admitted_run = gen
                admitted_at = iso()
                admitted = True
        _bump_run(ledger, gen, judgments=1, archives=1 if admitted else 0)


        now_stamp = iso()
        history = list(rec.get("history") or [])
        history.append(
            {
                "action": "decide",
                "outcome": outcome,
                "rule_id": rule_id,
                "reason": reason,
                "policy_digest": policy.digest,
                "policy_version": policy.version,
                "run_generated_at": gen,
                "at": now_stamp,
            }
        )
        rec.update(
            {
                "outcome": outcome,
                "state": state,
                "rule_id": rule_id,
                "reason": reason,
                "gap": gap,
                "follow_up": follow_up,
                "evidence": evidence,
                "decision_fingerprint": fingerprint,
                "policy_digest": policy.digest,
                "policy_version": policy.version,
                "decided_run": gen,
                "decided_at": now_stamp,
                "admitted_run": admitted_run,
                "admitted_at": admitted_at,
                "updated_at": now_stamp,
                "reassessment_required": False,
                "history": history[-MAX_HISTORY:],
            }
        )
        rec.pop("reassessment_reason", None)
        return {
            "status": "ok",
            "key": key,
            "outcome": outcome,
            "state": state,
            "rule_id": rule_id,
            "admitted_run": admitted_run,
            "evidence": {
                "kind": evidence["kind"],
                "quotes": evidence["quotes"],
                "fingerprint": evidence["fingerprint"],
            },
            "budget": {
                "judgments_used": judgments_used + 1,
                "judgments_limit": policy.budgets["judgments"],
                "archives_used": archives_used + (1 if admitted else 0),
                "archives_limit": policy.budgets["archives"],
            },
        }, not dry_run

    if dry_run:
        return _mutate_without_write(load_ledger(root), mutate)
    return _with_ledger(root, mutate)


# -------------------------------------------------------------------- ready


def _evidence_unchanged(rec: dict, cand: dict | None) -> bool:
    if cand is None:
        return False
    recorded = str(rec.get("observed_fingerprint") or "")
    return not recorded or recorded == observed_fingerprint(cand)


def _admission_blocker(rec: dict, policy: Policy, cand: dict | None) -> str:
    """Why a current candidate may not be (re-)admitted, or ``""``."""
    if cand is None:
        return "not_in_current_run"
    if rec.get("reassessment_required"):
        return "needs_reassessment"
    if str(rec.get("policy_digest") or "") != policy.digest:
        return "policy_changed"
    if not _evidence_unchanged(rec, cand):
        return "observed_changed"
    return ""


def ready(
    root: Path | str | None,
    policy: Policy,
    *,
    run_path: Path | str | None = None,
    run_data: dict | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    """List this run's admissions; re-admit prior-run work only when valid."""
    if not policy or not policy.enabled:
        raise SelectionDisabledError("selection policy is disabled")
    run = run_data if run_data is not None else load_run(run_path, root=root)
    gen = str(run.get("generated_at") or "").strip()
    if not gen:
        raise SelectionStateError(
            "selection requires a current discovery run with a generated_at; "
            "re-run discovery"
        )
    members = {
        identity_for_candidate(cand): cand
        for cand in (run.get("candidates") or [])
        if isinstance(cand, dict)
    }

    def mutate(ledger: dict):
        records = ledger.setdefault("records", {})
        budget = policy.budgets["archives"]
        used = run_archives_used(ledger, gen)
        admitted: list[dict] = []
        waiting: list[dict] = []
        not_readmitted: list[dict] = []
        changed = False

        def record_row(key: str, rec: dict, state: str) -> dict:
            return {
                "key": key,
                "url": rec.get("url") or "",
                "source_id": rec.get("source_id") or "",
                "state": state,
                "admitted_run": rec.get("admitted_run") or "",
                "admitted_at": rec.get("admitted_at") or "",
            }

        # Budget waits are owed work: promote them before re-admitting work that
        # an earlier run already admitted.
        promoted: set[str] = set()
        for key in sorted(records):
            rec = records[key]
            if not isinstance(rec, dict) or rec.get("outcome") != "accept":
                continue
            if rec.get("state") != STATE_BUDGET_WAIT:
                continue
            blocker = _admission_blocker(rec, policy, members.get(key))
            if blocker:
                waiting.append({"key": key, "reason": blocker})
                continue
            if used >= budget:
                waiting.append({"key": key, "reason": "archive_budget"})
                continue
            _bump_run(ledger, gen, archives=1)
            rec.update(
                {
                    "state": STATE_READY,
                    "admitted_run": gen,
                    "admitted_at": iso(),
                    "updated_at": iso(),
                }
            )
            used += 1
            changed = True
            promoted.add(key)
            admitted.append(record_row(key, rec, STATE_READY))

        for key in sorted(records):
            if key in promoted:
                continue
            rec = records[key]
            if not isinstance(rec, dict) or rec.get("outcome") != "accept":
                continue
            if rec.get("state") not in (STATE_READY, STATE_ARCHIVE_FAILURE):
                continue
            state = str(rec.get("state"))
            if rec.get("admitted_run") == gen:
                blocker = _admission_blocker(rec, policy, members.get(key))
                if blocker:
                    not_readmitted.append({"key": key, "reason": blocker})
                    continue
                admitted.append(record_row(key, rec, state))
                continue
            blocker = _admission_blocker(rec, policy, members.get(key))
            if blocker:
                not_readmitted.append({"key": key, "reason": blocker})
                continue
            if used >= budget:
                not_readmitted.append({"key": key, "reason": "archive_budget"})
                continue
            _bump_run(ledger, gen, archives=1)
            rec.update(
                {
                    "state": STATE_READY,
                    "admitted_run": gen,
                    "admitted_at": iso(),
                    "updated_at": iso(),
                }
            )
            used += 1
            changed = True
            admitted.append(record_row(key, rec, STATE_READY))

        _set_run_archives(ledger, gen, used)
        admitted.sort(key=lambda item: item["admitted_at"] or "")
        full_count = len(admitted)
        truncated = False
        if limit is not None and full_count > max(limit, 0):
            admitted = admitted[: max(limit, 0)]
            truncated = True
        return {
            "status": "ok",
            "run": gen,
            "admitted": admitted,
            "budget_wait": waiting,
            "not_readmitted": not_readmitted,
            "count": full_count,
            "displayed": len(admitted),
            "truncated": truncated,
            "archives_used": used,
            "archives_limit": budget,
        }, changed and not dry_run

    if dry_run:
        return _mutate_without_write(load_ledger(root), mutate)
    return _with_ledger(root, mutate)



# ------------------------------------------------------- automatic admission


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value or ""))
    except Exception:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=TZ)


def _preview_meta_for_path(root, key: str, path: Path) -> dict | None:
    pdir = preview_dir(root)
    if not pdir.is_dir():
        return None
    target = path.resolve()
    best: dict | None = None
    for sidecar in sorted(pdir.glob("*.json")):
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(meta, dict) or meta.get("key") != key:
            continue
        allowed = _preview_allowed_path(root, meta.get("path"))
        if allowed is None or allowed != target:
            continue
        best = meta
    return best



def admit(
    root: Path | str | None,
    policy: Policy,
    *,
    url: str,
    content_file: str | None = None,
    run_path: Path | str | None = None,
    run_data: dict | None = None,
) -> dict:
    """Gate an automatic ``add`` on a current accepted admission.

    A prior-run admission may be renewed only when the identity is a member of
    the current run with unchanged evidence/policy and remaining archive budget,
    so interrupted accepted work is retryable instead of hitting a dead end.

    Manual saves never call this. Raises :class:`SelectionStateError` so a
    refused automatic add is visible, not a silent skip.
    """
    if not policy or not policy.enabled:
        raise SelectionDisabledError("selection policy is disabled")
    key = identity_for_url(url)
    if not key:
        raise SelectionStateError("automatic add needs a valid URL")
    try:
        run = run_data if run_data is not None else load_run(run_path, root=root)
    except SelectionStateError:
        run = {}
    gen = str((run or {}).get("generated_at") or "").strip()
    if not gen:
        raise SelectionStateError(
            "automatic add blocked: no valid current discovery run; run discovery first"
        )
    members = {
        identity_for_candidate(cand): cand
        for cand in ((run or {}).get("candidates") or [])
        if isinstance(cand, dict)
    }

    def mutate(ledger: dict):
        rec = (ledger.get("records") or {}).get(key)
        if not isinstance(rec, dict):
            raise SelectionStateError(
                "automatic add blocked: no accepted admission for this candidate "
                "(manual add is unaffected)"
            )
        outcome = str(rec.get("outcome") or "")
        if outcome != "accept":
            raise SelectionStateError(
                f"automatic add blocked: candidate outcome is {outcome or 'undecided'!r}, "
                "not 'accept'"
            )
        state = str(rec.get("state") or "")
        if state not in (STATE_READY, STATE_ARCHIVE_FAILURE):
            raise SelectionStateError(
                f"automatic add blocked: admission state is {state!r} "
                "(budget_wait is an explicit wait, not an admission)"
            )
        if str(rec.get("policy_digest") or "") != policy.digest:
            raise SelectionStateError(
                "automatic add blocked: policy changed after admission; reassess the decision"
            )
        cand = members.get(key)
        if rec.get("reassessment_required"):
            raise SelectionStateError(
                "automatic add blocked: candidate needs explicit reassessment "
                f"({rec.get('reassessment_reason') or 'observed_content_changed'})"
            )
        admitted = _parse_iso(str(rec.get("admitted_at") or ""))
        stale = (
            admitted is None
            or str(rec.get("admitted_run") or "") != gen
            or now() - admitted > timedelta(days=ADMISSION_MAX_AGE_DAYS)
        )
        changed = False
        if stale:
            blocker = _admission_blocker(rec, policy, cand)
            if blocker == "not_in_current_run":
                raise SelectionStateError(
                    "automatic add blocked: admission expired and the candidate is "
                    "not part of the current run; run discovery or reassess"
                )
            if blocker:
                raise SelectionStateError(
                    f"automatic add blocked: admission not renewable ({blocker}); "
                    "reassess the decision"
                )
            if run_archives_used(ledger, gen) >= policy.budgets["archives"]:
                raise SelectionStateError(
                    "automatic add blocked: archive budget exhausted for this run"
                )
            _bump_run(ledger, gen, archives=1)
            rec.update(
                {
                    "admitted_run": gen,
                    "admitted_at": iso(),
                    "updated_at": iso(),
                }
            )
            state = str(rec.get("state") or state)
            changed = True
        else:
            # A fresh admission still needs current-run membership: a stale
            # matching timestamp is not proof that the candidate is in this run.
            if cand is None:
                raise SelectionStateError(
                    "automatic add blocked: candidate is not part of the current run; "
                    "run discovery or reassess the decision"
                )
            if not _evidence_unchanged(rec, cand):
                raise SelectionStateError(
                    "automatic add blocked: observed metadata changed after admission; "
                    "reassess the decision"
                )
        preview_reused = False
        preview_body_hash = ""
        if content_file:
            supplied = Path(content_file).expanduser().resolve()
            expected = ""
            binding = (ledger.get("previews") or {}).get(key)
            if isinstance(binding, dict) and binding.get("path"):
                recorded = _preview_allowed_path(root, binding.get("path"))
                if recorded is not None and recorded == supplied:
                    expected = str(binding.get("body_hash") or "")
            if not expected:
                meta = _preview_meta_for_path(root, key, supplied)
                if isinstance(meta, dict):
                    expected = str(meta.get("body_hash") or "")
            if expected:
                try:
                    actual = "sha256:" + hashlib.sha256(supplied.read_bytes()).hexdigest()
                except OSError as exc:
                    raise SelectionStateError(f"preview content is unreadable: {exc}") from exc
                if actual != expected:
                    raise SelectionStateError(
                        "preview hash mismatch: the supplied content file no longer "
                        "matches its recorded preview binding"
                    )
                preview_reused = True
                preview_body_hash = expected
        return {
            "status": "ok",
            "key": key,
            "state": state,
            "preview_reused": preview_reused,
            "preview_body_hash": preview_body_hash,
            "readmission": changed,
        }, changed

    return _with_ledger(root, mutate)



def mark_archived(
    root: Path | str | None,
    policy: Policy,
    *,
    url: str,
    entry_id: str = "",
    path: str = "",
    manual: bool = False,
) -> dict:
    """Record the outcome of a real add.

    A manual save never deletes an automatic exclusion: it appends a bounded
    ``manual_override`` history entry and leaves the editorial outcome intact.
    An accepted automatic admission becomes ``archived`` so retries stop.
    """
    if not policy or not policy.enabled:
        return {"status": "skipped", "reason": "disabled"}
    key = identity_for_url(url)
    if not key:
        return {"status": "skipped", "reason": "empty_url"}

    def mutate(ledger: dict):
        rec = (ledger.get("records") or {}).get(key)
        if not isinstance(rec, dict):
            return {"status": "not_tracked", "key": key}, False
        stamp_now = iso()
        if manual and rec.get("outcome") in ("reject", "defer"):
            history = list(rec.get("history") or [])
            history.append(
                {"action": "manual_override", "entry": entry_id, "path": path,
                 "at": stamp_now}
            )
            rec["history"] = history[-MAX_HISTORY:]
            rec["manual_override_at"] = stamp_now
            rec["updated_at"] = stamp_now
            return {"status": "manual_override", "key": key}, True
        if manual and not rec.get("outcome"):
            # A deliberate manual save closes the undecided automatic queue
            # work without inventing an editorial outcome.
            history = list(rec.get("history") or [])
            history.append(
                {"action": "manual_save", "entry": entry_id, "path": path,
                 "at": stamp_now}
            )
            rec.update(
                {
                    "state": STATE_ARCHIVED,
                    "archived_at": stamp_now,
                    "entry": entry_id,
                    "entry_path": path,
                    "archive_evidence": "seen",
                    "history": history[-MAX_HISTORY:],
                    "updated_at": stamp_now,
                }
            )
            rec.pop("failure_reason", None)
            return {"status": "manual_save", "key": key, "state": STATE_ARCHIVED}, True
        if rec.get("outcome") != "accept":
            return {"status": "unchanged", "key": key}, False
        history = list(rec.get("history") or [])
        if manual:
            history.append(
                {"action": "manual_save", "entry": entry_id, "path": path,
                 "at": stamp_now}
            )
        rec.update(
            {
                "state": STATE_ARCHIVED,
                "archived_at": stamp_now,
                "entry": entry_id,
                "entry_path": path,
                "archive_evidence": "seen",
                "history": history[-MAX_HISTORY:],
                "updated_at": stamp_now,
            }
        )
        rec.pop("failure_reason", None)
        return {"status": "archived", "key": key, "state": STATE_ARCHIVED}, True


    return _with_ledger(root, mutate)


def mark_archive_failure(
    root: Path | str | None,
    policy: Policy,
    *,
    url: str,
    reason: str,
) -> dict:
    """Record a technical archive failure; accepted work stays retryable."""
    if not policy or not policy.enabled:
        return {"status": "skipped", "reason": "disabled"}
    key = identity_for_url(url)
    if not key:
        return {"status": "skipped", "reason": "empty_url"}

    def mutate(ledger: dict):
        rec = (ledger.get("records") or {}).get(key)
        if not isinstance(rec, dict) or rec.get("outcome") != "accept":
            return {"status": "unchanged", "key": key}, False
        rec.update(
            {
                "state": STATE_ARCHIVE_FAILURE,
                "failure_reason": " ".join(str(reason or "").split())[:300],
                "failed_at": iso(),
                "updated_at": iso(),
            }
        )
        return {"status": "archive_failure", "key": key}, True

    return _with_ledger(root, mutate)


# ------------------------------------------------------------- reconciliation


ACCOUNTED_KINDS = (
    "archived",
    "rejected",
    "deferred",
    "budget_wait",
    "judgment_wait",
    "technical_failure",
    "execution_expired",
)
UNACCOUNTED_KINDS = ("unjudged", "pending_archive", "archive_unproven")



def _defer_expired(rec: dict, policy: Policy) -> bool:
    decided = _parse_iso(str(rec.get("decided_at") or ""))
    if decided is None:
        return False
    return now() - decided > timedelta(days=policy.retention["review_days"])


def classify_candidates(
    ledger: dict,
    policy: Policy,
    candidates: list[dict],
    *,
    seen_keys: set[str],
    failed_keys: set[str],
) -> dict:
    """Give every discovered candidate exactly one accounted outcome.

    Content outcomes (accept/reject/defer) are reported separately from execution
    states (budget wait, archive failure, archived). ``unjudged`` and
    ``pending_archive`` are the two unaccounted kinds that must fail a finish.
    """
    records = ledger.get("records") or {}
    counts = {kind: 0 for kind in ACCOUNTED_KINDS + UNACCOUNTED_KINDS}
    rows: list[dict] = []
    unjudged: list[dict] = []
    pending_archive: list[dict] = []
    archive_unproven: list[dict] = []
    unresolved: list[dict] = []
    exclusions: dict[str, dict] = {}
    seen_urls: set[str] = set()

    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        key = identity_for_candidate(cand)
        url = str(cand.get("url") or "")
        if not key or key in seen_urls:
            continue
        seen_urls.add(key)
        dedupe = dedupe_key(url)
        is_seen = _seen_evidence(key, url, seen_keys)
        is_failed = key in failed_keys or (dedupe and dedupe in failed_keys)
        rec = records.get(key) or {}
        outcome = str(rec.get("outcome") or "")
        state = str(rec.get("state") or "")

        if is_seen:
            kind = "archived"
        elif is_failed:
            kind = "technical_failure"
        elif outcome == "reject":
            kind = "rejected"
        elif outcome == "defer":
            kind = "deferred"
        elif outcome == "accept" and state == STATE_ARCHIVED:
            # A ledger flag alone is not archive proof: durable seen/library
            # evidence must still exist or the account is incomplete.
            kind = "archived"
        elif outcome == "accept" and state == STATE_BUDGET_WAIT:
            kind = "budget_wait"
        elif outcome == "accept" and state == STATE_ARCHIVE_FAILURE:
            kind = "technical_failure"
        elif outcome == "" and state == STATE_JUDGMENT_WAIT:
            kind = "judgment_wait"
        elif outcome == "" and state == STATE_ARCHIVED:
            kind = "archived" if is_seen else "archive_unproven"
        elif outcome == "" and state == STATE_EXECUTION_EXPIRED:
            kind = "execution_expired"
        elif outcome == "" and state == STATE_ARCHIVE_FAILURE:
            kind = "technical_failure"
        elif outcome == "accept":
            kind = "pending_archive"
        elif state == STATE_EXECUTION_EXPIRED:
            kind = "execution_expired"
        else:
            kind = "unjudged"
        if outcome == "accept" and state == STATE_ARCHIVED and not is_seen:
            kind = "archive_unproven"
        counts[kind] += 1


        row = {
            "key": key,
            "url": url,
            "title": str(cand.get("title") or rec.get("title") or ""),
            "source_id": str(cand.get("source_id") or rec.get("source_id") or ""),
            "kind": kind,
            "outcome": outcome,
            "state": state,
        }
        rows.append(row)

        if kind == "unjudged":
            unjudged.append(row)
        elif kind == "pending_archive":
            pending_archive.append(row)
        elif kind == "archive_unproven":
            archive_unproven.append(row)
        elif kind == "rejected":

            rule_id = str(rec.get("rule_id") or "unknown")
            entry = exclusions.setdefault(rule_id, {"rule_id": rule_id, "count": 0,
                                                    "reason": str(rec.get("reason") or "")})
            entry["count"] += 1
        elif kind == "deferred":
            unresolved.append(
                {
                    **row,
                    "gap": str(rec.get("gap") or ""),
                    "follow_up": str(rec.get("follow_up") or ""),
                    "review_attempts": int(rec.get("review_attempts") or 0),
                    "expired": _defer_expired(rec, policy),
                }
            )

    unaccounted = unjudged + pending_archive + archive_unproven
    return {
        "counts": counts,
        "rows": rows,
        "unjudged": unjudged,
        "pending_archive": pending_archive,
        "archive_unproven": archive_unproven,
        "unresolved": unresolved,
        "exclusions": sorted(exclusions.values(), key=lambda e: (-e["count"], e["rule_id"])),
        "unaccounted": unaccounted,
        "accounted": not unaccounted,
        "coverage": "accounting-complete" if not unaccounted else "accounting-incomplete",
    }



def ledger_overview(ledger: dict, policy: Policy) -> dict:
    """Read-only view of work that outlives a single run (digest/status)."""
    records = ledger.get("records") or {}
    unresolved: list[dict] = []
    reassess: list[dict] = []
    counts = {kind: 0 for kind in ACCOUNTED_KINDS + UNACCOUNTED_KINDS}
    now_dt = now()
    review_days = int(policy.retention["review_days"])
    oldest_open_days: int | None = None
    stalled = 0
    for key, rec in records.items():
        if not isinstance(rec, dict):
            continue
        outcome = str(rec.get("outcome") or "")
        state = str(rec.get("state") or "")
        if outcome == "defer":
            unresolved.append(
                {
                    "key": key,
                    "url": rec.get("url") or "",
                    "title": rec.get("title") or "",
                    "gap": rec.get("gap") or "",
                    "follow_up": rec.get("follow_up") or "",
                    "review_attempts": int(rec.get("review_attempts") or 0),
                    "expired": _defer_expired(rec, policy),
                }
            )
        if rec.get("reassessment_required") or (
            outcome and rec.get("policy_digest") != policy.digest
        ):
            reassess.append(
                {
                    "key": key,
                    "url": rec.get("url") or "",
                    "reason": rec.get("reassessment_reason") or "policy_changed",
                }
            )
        if state == STATE_BUDGET_WAIT:
            counts["budget_wait"] += 1
        elif state == STATE_JUDGMENT_WAIT:
            counts["judgment_wait"] += 1
        elif state == STATE_ARCHIVE_FAILURE:
            counts["technical_failure"] += 1
        elif state == STATE_AWAITING:
            counts["unjudged"] += 1
        elif state == STATE_READY:
            counts["pending_archive"] += 1
        elif state == STATE_EXECUTION_EXPIRED:
            counts["execution_expired"] += 1
        if state in OPEN_STATES or state == STATE_EXECUTION_EXPIRED:
            # Outstanding work is aged from its enqueue/open origin. Observation
            # refreshes and recovery only move ``updated_at``, so they must not
            # make old work look new again.
            stamp = _parse_iso(
                str(
                    rec.get("queued_at")
                    or rec.get("first_seen")
                    or ""
                )
            )
            if stamp is not None:
                age = max(int((now_dt - stamp).days), 0)
                oldest_open_days = age if oldest_open_days is None else max(oldest_open_days, age)
                if age > review_days:
                    stalled += 1
    open_work = (
        counts["budget_wait"]
        + counts["judgment_wait"]
        + counts["technical_failure"]
        + counts["unjudged"]
        + counts["pending_archive"]
        + counts["execution_expired"]
    )
    return {
        "unresolved": unresolved,
        "expired_unresolved": [u for u in unresolved if u["expired"]],
        "needs_reassessment": reassess,
        "open_counts": counts,
        "open_total": open_work,
        "oldest_open_days": oldest_open_days,
        "stalled": stalled,
        "overflow": sorted(
            key
            for key, rec in records.items()
            if isinstance(rec, dict) and rec.get("queue_overflow")
        ),
        "review_days": review_days,
        "review_limit": int(policy.retention["max_review_attempts"]),
        "next_action": (
            "bin/ca select status; resolve open work with `select ready --run` "
            "or `select reassess --key ... --apply`"
            if open_work
            else "none"
        ),
    }



def mark_judgment_wait(
    root: Path | str | None,
    policy: Policy,
    *,
    keys: list[str],
    run_gen: str,
) -> dict:
    """Mark genuinely judgment-budget-limited work as an explicit durable wait.

    This only applies once the run's actual judgment limit has been consumed;
    untouched work below the limit must still fail finish. The wait is not an
    editorial reject and is retried in a later run.
    """
    if not policy or not policy.enabled:
        raise SelectionDisabledError("selection policy is disabled")

    def mutate(ledger: dict):
        used = run_judgments_used(ledger, run_gen)
        if used < policy.budgets["judgments"]:
            return {
                "status": "below_budget",
                "judgments_used": used,
                "judgments_limit": policy.budgets["judgments"],
                "marked": [],
            }, False
        records = ledger.setdefault("records", {})
        stamp_now = iso()
        marked: list[str] = []
        for key in keys:
            rec = records.get(key)
            if not isinstance(rec, dict):
                continue
            if rec.get("state") != STATE_AWAITING:
                continue
            if str(rec.get("run_generated_at") or "") != run_gen:
                continue
            history = list(rec.get("history") or [])
            history.append({"action": "judgment_budget_wait", "at": stamp_now})
            rec.update(
                {
                    "state": STATE_JUDGMENT_WAIT,
                    "judgment_wait_at": stamp_now,
                    "history": history[-MAX_HISTORY:],
                    "updated_at": stamp_now,
                }
            )
            marked.append(key)
        return {
            "status": "ok",
            "judgments_used": used,
            "judgments_limit": policy.budgets["judgments"],
            "marked": marked,
        }, bool(marked)

    return _with_ledger(root, mutate)


def prune_pending(root: Path | str | None, policy: Policy) -> list[str]:
    """Drop terminal selection identities from ``pending.json``.

    A reject/defer (or archived) candidate is a valid outcome, not owed work: the
    queue must not keep re-offering it between fetch runs. Derived from the
    durable ledger, so it is safe to call after any interruption.
    """
    if not policy or not policy.enabled:
        return []
    with state_lock(Path(root) if root is not None else ROOT):
        ledger = load_ledger(root)
        terminal = suppressed_keys(ledger)
        if not terminal:
            return []
        path = (Path(root) if root is not None else ROOT) / ".state" / "pending.json"
        data = load_json(path, {"entries": {}})
        entries = dict(data.get("entries") or {})
        removed = sorted(key for key in entries if key in terminal)
        if not removed:
            return []
        for key in removed:
            entries.pop(key, None)
        data["entries"] = entries
        data["updated_at"] = iso()
        write_json_atomic(path, data)
    return removed


# ------------------------------------------------------- reassess / cleanup


def _requeue_identity(root, rec: dict) -> None:
    """Requeue exactly one explicitly reopened identity under the state lock."""
    key = str(rec.get("key") or "")
    if not key or not rec.get("url"):
        return
    path = (Path(root) if root is not None else ROOT) / ".state" / "pending.json"
    data = load_json(path, {"entries": {}})
    entries: dict = dict(data.get("entries") or {})
    stamp_now = iso()
    payload = _recovery_pending_record(rec)
    payload["reason"] = "reassess_reopen"
    payload["queued_at"] = stamp_now
    payload["parked_at"] = stamp_now
    payload["updated_at"] = stamp_now
    entries[key] = payload
    data["entries"] = entries
    data["updated_at"] = stamp_now
    write_json_atomic(path, data)


def reassess(
    root: Path | str | None,
    policy: Policy,
    *,
    key: str,
    run_path: Path | str | None = None,
    run_data: dict | None = None,
    apply: bool = False,
    reason: str = "",
) -> dict:
    """Preview-first, explicit and bounded reassessment of one decision.

    A terminal identity may be reopened after it disappears from
    ``latest-fetch``: the retained verified metadata is used to requeue only
    that identity, and a fresh discovery/preview/decision is still required
    (the run binding is cleared). Execution-expired undecided work is
    reassessable for the same reason, without becoming an editorial reject.
    """
    if not policy or not policy.enabled:
        raise SelectionDisabledError("selection policy is disabled")
    ledger = load_ledger(root)
    rec = (ledger.get("records") or {}).get(key)
    if not isinstance(rec, dict):
        raise SelectionStateError(f"{key!r} is not registered in the selection ledger")
    reopenable = bool(rec.get("outcome")) or rec.get("state") == STATE_EXECUTION_EXPIRED
    if not reopenable:
        raise SelectionStateError(
            f"{key!r} has no decision to reassess; decide it in the current run"
        )
    attempts = int(rec.get("review_attempts") or 0)
    limit = int(policy.retention["max_review_attempts"])
    try:
        run = run_data if run_data is not None else load_run(run_path, root=root)
    except SelectionStateError:
        run = None
    gen = str((run or {}).get("generated_at") or "").strip()
    member = find_candidate(run, key=key) if run else None
    plan = {
        "status": "plan",
        "key": key,
        "current": {
            "outcome": rec.get("outcome") or "",
            "state": rec.get("state") or "",
            "rule_id": rec.get("rule_id") or "",
            "reason": rec.get("reason") or "",
            "decided_at": rec.get("decided_at") or "",
            "evidence_fingerprint": (rec.get("evidence") or {}).get("fingerprint") or "",
        },
        "policy_changed": rec.get("policy_digest") != policy.digest,
        "observed_change": bool(rec.get("reassessment_required")),
        "review_attempts": attempts,
        "max_review_attempts": limit,
        "attempts_left": max(limit - attempts, 0),
        "in_current_run": member is not None,
        "reopen_pending_discovery": member is None,
        "requeue": True,
        "applied": False,
    }
    if not apply:
        return plan
    if attempts >= limit:
        raise SelectionBudgetError(
            f"reassessment attempt limit reached ({attempts}/{limit}) for {key!r}"
        )

    def mutate(ledger: dict):
        rec = (ledger.get("records") or {}).get(key)
        if not isinstance(rec, dict):
            raise SelectionStateError(f"{key!r} is not registered in the selection ledger")
        # The attempt limit is re-checked inside the mutation lock: a concurrent
        # official reassessment must not slip past a stale preflight count.
        attempts_now = int(rec.get("review_attempts") or 0)
        if attempts_now >= limit:
            raise SelectionBudgetError(
                f"reassessment attempt limit reached ({attempts_now}/{limit}) for {key!r}"
            )
        if not (rec.get("outcome") or rec.get("state") == STATE_EXECUTION_EXPIRED):
            raise SelectionStateError(f"{key!r} has no decision to reassess")
        # Queue capacity is checked before any durable change. Reopening a key
        # that is not already queued must never enlarge a full pending queue, and
        # a refusal must not consume an attempt or touch the ledger.
        pending_file = (Path(root) if root is not None else ROOT) / ".state" / "pending.json"
        queued = dict(load_json(pending_file, {"entries": {}}).get("entries") or {})
        limit_now = pending_queue_limit()
        if key not in queued and len(queued) >= limit_now:
            raise SelectionCapacityError(
                f"reassess refused: pending queue capacity is exhausted ({len(queued)}/{limit_now}); "
                "let it drain or clean up expired work, then reopen this identity"
            )
        stamp_now = iso()
        history = list(rec.get("history") or [])
        history.append(
            {
                "action": "reassess",
                "previous_outcome": rec.get("outcome") or "",
                "previous_state": rec.get("state") or "",
                "previous_rule_id": rec.get("rule_id") or "",
                "previous_reason": rec.get("reason") or "",
                "note": " ".join(str(reason or "").split())[:300],
                "policy_digest": policy.digest,
                "policy_version": policy.version,
                "run_generated_at": gen,
                "at": stamp_now,
            }
        )
        rec.update(
            {
                "outcome": "",
                "state": STATE_AWAITING,
                "rule_id": "",
                "reason": "",
                "gap": "",
                "follow_up": "",
                "evidence": {},
                "decision_fingerprint": "",
                "policy_digest": policy.digest,
                "policy_version": policy.version,
                "run_generated_at": gen if member is not None else "",
                "decided_run": "",
                "decided_at": "",
                "admitted_run": "",
                "admitted_at": "",
                "review_attempts": attempts_now + 1,
                "history": history[-MAX_HISTORY:],
                "reassessment_required": False,
                "reopen_pending_discovery": member is None,
                "updated_at": stamp_now,
            }
        )
        rec.pop("reassessment_reason", None)
        rec.pop("reassessment_observed_fingerprint", None)
        rec.pop("queue_overflow", None)
        rec.pop("queue_overflow_at", None)
        _requeue_identity(root, rec)
        return {
            "status": "ok",
            "key": key,
            "state": STATE_AWAITING,
            "run_generated_at": rec["run_generated_at"],
            "reopen_pending_discovery": member is None,
            "review_attempts": rec["review_attempts"],
            "max_review_attempts": limit,
            "applied": True,
        }, True

    return _with_ledger(root, mutate)



def _plan_cleanup(root, records: dict, pending_entries: dict, policy: Policy) -> dict:
    current = now()
    preview_cutoff = current - timedelta(days=policy.retention["preview_days"])
    identity_cutoff = current - timedelta(days=policy.retention["identity_days"])
    active = {
        key for key, rec in records.items()
        if isinstance(rec, dict) and rec.get("state") in OPEN_STATES
    }
    active |= set(pending_entries)
    expire_keys: list[str] = []
    drop_keys: list[str] = []
    for key, rec in records.items():
        if not isinstance(rec, dict) or key in active:
            continue
        if not (rec.get("outcome") in ("reject", "defer")
                or rec.get("state") == STATE_ARCHIVED):
            continue
        stamp = _parse_iso(str(rec.get("decided_at") or rec.get("updated_at")
                               or rec.get("first_seen") or ""))
        if stamp is None:
            continue
        if stamp < identity_cutoff:
            drop_keys.append(key)
        elif (
            stamp < preview_cutoff
            and not rec.get("detail_expired")
            and (rec.get("evidence") or {}).get("quotes")
        ):
            expire_keys.append(key)

    protected_stems = {
        hashlib.sha256(key.encode("utf-8")).hexdigest()[:16] for key in active
    }
    drop_previews: list[str] = []
    preview_files: list[Path] = []
    pdir = preview_dir(root)
    if pdir.is_dir():
        for path in sorted(pdir.glob("*.md")):
            stem = path.name.split("-", 1)[0]
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=TZ)
            except OSError:
                continue
            if stem in protected_stems or mtime >= preview_cutoff:
                continue
            drop_previews.append(path.name)
            preview_files.append(path)
    return {
        "active": active,
        "expire_keys": expire_keys,
        "drop_keys": drop_keys,
        "preview_files": preview_files,
        "drop_previews": drop_previews,
        "preview_cutoff": preview_cutoff,
    }


def cleanup(
    root: Path | str | None,
    policy: Policy,
    *,
    apply: bool = False,
) -> dict:
    """Plan (default) or apply retention / identity / preview-cache cleanup.

    ``--apply`` re-reads record state, timestamps and pending references while
    holding the state lock: a concurrent official reassessment cannot lose its
    reopened work to a stale plan, and the same lock guards the file drops.
    """
    if not policy or not policy.enabled:
        raise SelectionDisabledError("selection policy is disabled")
    root_path = Path(root) if root is not None else ROOT
    ledger = load_ledger(root)
    records = ledger.get("records") or {}
    pending_entries = dict(
        (load_json(root_path / ".state" / "pending.json", {"entries": {}}).get("entries") or {})
    )
    plan = _plan_cleanup(root_path, records, pending_entries, policy)
    out = {
        "status": "plan",
        "applied": bool(apply),
        "expire_evidence": len(plan["expire_keys"]),
        "expire_keys": sorted(plan["expire_keys"])[:20],
        "drop_identities": len(plan["drop_keys"]),
        "drop_keys": sorted(plan["drop_keys"])[:20],
        "drop_previews": plan["drop_previews"][:20],
        "protected": len(plan["active"]),
        "retention": dict(policy.retention),
    }
    if not apply:
        return out

    def mutate(ledger: dict):
        records = ledger.setdefault("records", {})
        fresh_pending = dict(
            (load_json(root_path / ".state" / "pending.json", {"entries": {}}).get("entries") or {})
        )
        fresh = _plan_cleanup(root_path, records, fresh_pending, policy)
        stamp_now = iso()
        expired = dropped = 0
        for key in fresh["expire_keys"]:
            rec = records.get(key)
            if not isinstance(rec, dict):
                continue
            evidence = rec.get("evidence") if isinstance(rec.get("evidence"), dict) else {}
            rec["evidence"] = {
                "kind": evidence.get("kind") or "",
                "fingerprint": evidence.get("fingerprint") or "",
                "quotes": [],
                "expired": True,
            }
            rec["detail_expired"] = True
            rec["detail_expired_at"] = stamp_now
            expired += 1
        for key in fresh["drop_keys"]:
            if records.pop(key, None) is not None:
                dropped += 1
        removed = 0
        for path in fresh["preview_files"]:
            for target in (path, path.with_suffix(".json")):
                try:
                    target.unlink()
                    removed += 1
                except OSError:
                    pass
        # Expired preview bindings must not keep authorizing a decision.
        previews = ledger.setdefault("previews", {})
        stale = [
            key for key, meta in previews.items()
            if not isinstance(meta, dict)
            or _preview_allowed_path(root_path, meta.get("path")) is None
        ]
        for key in stale:
            previews.pop(key, None)
        return {
            "status": "ok",
            "applied": True,
            "expired": expired,
            "dropped": dropped,
            "previews_removed": removed,
            "protected": len(fresh["active"]),
        }, bool(expired or dropped or removed or stale)

    return _with_ledger(root_path, mutate)



# --------------------------------------------------------------------- CLI


def _emit(payload: dict, code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Optional subscription selection")
    sub = ap.add_subparsers(dest="action", required=True)

    sub.add_parser("status", help="policy and ledger status").add_argument(
        "--json", action="store_true", help=argparse.SUPPRESS
    )

    preview_ap = sub.add_parser("preview", help="bounded preview text (never archives)")
    preview_ap.add_argument("--url", default="")
    preview_ap.add_argument("--key", default="")
    preview_ap.add_argument("--run", default="")
    preview_ap.add_argument("--content-file", default="")
    preview_ap.add_argument("--dry-run", action="store_true")

    decide_ap = sub.add_parser("decide", help="record one validated decision")
    decide_ap.add_argument("--key", required=True)
    decide_ap.add_argument("--outcome", required=True, choices=OUTCOMES)
    decide_ap.add_argument("--rule", default="")
    decide_ap.add_argument("--reason", required=True)
    decide_ap.add_argument("--evidence", action="append", default=[])
    decide_ap.add_argument("--gap", default="")
    decide_ap.add_argument("--follow-up", default="")
    decide_ap.add_argument("--run", default="")
    decide_ap.add_argument("--dry-run", action="store_true")

    ready_ap = sub.add_parser("ready", help="list admitted accepted candidates")
    ready_ap.add_argument("--run", default="")
    ready_ap.add_argument("--limit", type=int, default=None)
    ready_ap.add_argument("--dry-run", action="store_true")

    reassess_ap = sub.add_parser("reassess", help="preview-first explicit reassessment")
    reassess_ap.add_argument("--key", required=True)
    reassess_ap.add_argument("--run", default="")
    reassess_ap.add_argument("--apply", action="store_true")
    reassess_ap.add_argument("--reason", default="")

    cleanup_ap = sub.add_parser("cleanup", help="retention plan (read-only unless --apply)")
    cleanup_ap.add_argument("--apply", action="store_true")

    args = ap.parse_args(argv)

    try:
        if args.action == "status":
            payload = status(ROOT)
            code = 2 if (payload.get("policy") or {}).get("status") == "invalid" else 0
            return _emit(payload, code)

        policy = load_policy(ROOT)
        if policy is None:
            raise SelectionDisabledError(
                "selection policy is missing or disabled; "
                "automatic selection is not active"
            )
        run = Path(args.run) if getattr(args, "run", "") else run_path_default(ROOT)

        if args.action == "preview":
            return _emit(
                preview(
                    ROOT,
                    policy,
                    url=args.url,
                    key=args.key,
                    run_path=run,
                    content_file=args.content_file or None,
                    dry_run=args.dry_run,
                )
            )
        if args.action == "decide":
            return _emit(
                decide(
                    ROOT,
                    policy,
                    key=args.key,
                    outcome=args.outcome,
                    rule_id=args.rule,
                    reason=args.reason,
                    quotes=args.evidence,
                    gap=args.gap,
                    follow_up=args.follow_up,
                    run_path=run,
                    dry_run=args.dry_run,
                )
            )
        if args.action == "ready":
            return _emit(
                ready(
                    ROOT,
                    policy,
                    run_path=run,
                    limit=args.limit,
                    dry_run=args.dry_run,
                )
            )
        if args.action == "reassess":
            return _emit(
                reassess(
                    ROOT,
                    policy,
                    key=args.key,
                    run_path=run,
                    apply=args.apply,
                    reason=args.reason,
                )
            )
        if args.action == "cleanup":
            return _emit(cleanup(ROOT, policy, apply=args.apply))
        return 2
    except SelectionError as exc:
        print(f"[select] {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
