#!/usr/bin/env python3
"""Optional subscription-selection tests (synthetic fixtures only).

Every test builds a throwaway project under the shared scratch root and copies
the shipped ``scripts/`` into it, so no real archive, state file or configured
instance is read or written. Model judgment is supplied by the test itself: the
toolkit only validates and persists decisions.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))

PY = _support.PYTHON
SCRATCH = _support.scratch_dir("subscription-")
TZ = timezone(timedelta(hours=8))

try:  # The module is the implementation under test; absent => RED.
    import subscription_selection as sel  # noqa: E402
except Exception:  # pragma: no cover - only while the slice is RED
    sel = None


def base_policy(**overrides) -> dict:
    data = {
        "schema": 1,
        "enabled": True,
        "version": 1,
        "mode": "enforce",
        "rules": [
            {
                "id": "material-release",
                "outcome": "accept",
                "description": "Material platform or model release.",
                "evidence": "metadata",
            },
            {
                "id": "routine-roundup",
                "outcome": "reject",
                "description": "Routine promotional roundup without original reporting.",
                "evidence": "either",
            },
            {
                "id": "original-research",
                "outcome": "accept",
                "description": "Original first-hand measurements.",
                "evidence": "body",
            },
        ],
    }
    data.update(overrides)
    return data


def stamp(**delta) -> str:
    return (datetime.now(TZ) + timedelta(**delta)).isoformat(timespec="seconds")


def candidate(url: str, **over) -> dict:
    cand = {
        "url": url,
        "title": "Synthetic platform release",
        "summary": "A material platform release with original detail.",
        "published": stamp(days=-1),
        "source": "Vendor Alpha",
        "source_id": "alpha",
    }
    cand.update(over)
    return cand


class ProjectCase(unittest.TestCase):
    """Scratch archive with its own ``scripts/`` copy and optional policy."""

    def setUp(self):
        self._roots: list[Path] = []

    def tearDown(self):
        for root in self._roots:
            shutil.rmtree(root, ignore_errors=True)
        self._roots = []

    def project(
        self,
        policy: dict | str | None = None,
        *,
        seen: dict | None = None,
        sources: dict | None = None,
    ) -> Path:
        root = _support.archive_project(
            "selproj_",
            configs=("taxonomy.yaml",),
        )
        (root / ".state").mkdir(parents=True, exist_ok=True)
        (root / ".state" / "seen.json").write_text(
            json.dumps({"entries": seen or {}}), encoding="utf-8"
        )
        (root / "library").mkdir(exist_ok=True)
        if policy is not None:
            if isinstance(policy, str):
                (root / "selection.yaml").write_text(policy, encoding="utf-8")
            else:
                (root / "selection.yaml").write_text(
                    yaml.safe_dump(policy, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
        if sources is not None:
            (root / "sources.yaml").write_text(
                yaml.safe_dump(sources, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        self._roots.append(root)
        return root

    def run_select(self, root: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PY, str(root / "scripts" / "subscription_selection.py"), *args],
            capture_output=True, text=True, cwd=str(root), timeout=120,
        )

    def select_json(self, root: Path, *args: str) -> dict:
        res = self.run_select(root, *args)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        return json.loads(res.stdout)

    def ledger(self, root: Path) -> dict:
        path = root / ".state" / "selection" / "ledger.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def write_run(
        self,
        root: Path,
        candidates: list[dict],
        *,
        generated_at: str | None = None,
        name: str = "latest-fetch.json",
    ) -> Path:
        runs = root / ".state" / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        data = {
            "generated_at": generated_at or stamp(),
            "candidates": candidates,
            "sources": [],
        }
        path = runs / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def decide(
        self,
        root: Path,
        key: str,
        outcome: str,
        *,
        rule: str | None = None,
        reason: str = "synthetic editorial reason",
        quotes: tuple[str, ...] = (),
        run: Path | None = None,
        gap: str = "",
        follow_up: str = "",
    ) -> subprocess.CompletedProcess:
        args = ["decide", "--key", key, "--outcome", outcome, "--reason", reason]
        if rule:
            args += ["--rule", rule]
        for quote in quotes:
            args += ["--evidence", quote]
        if run is not None:
            args += ["--run", str(run)]
        if gap:
            args += ["--gap", gap]
        if follow_up:
            args += ["--follow-up", follow_up]
        return self.run_select(root, *args)

    def decide_ok(self, root: Path, key: str, outcome: str, **kwargs) -> dict:
        res = self.decide(root, key, outcome, **kwargs)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        return json.loads(res.stdout)

    def register(
        self,
        root: Path,
        candidates: list[dict],
        *,
        run: Path | None = None,
        only: list[dict] | None = None,
    ) -> dict:
        if run is None:
            run = self.write_run(root, candidates)
        return sel.register_discovered(
            root, sel.load_policy(root), run_path=run, candidates=only
        )


class PolicyContractTests(ProjectCase):
    """Strict schema 1: unknown keys, wrong types and bad limits fail closed."""

    def test_missing_policy_keeps_legacy_path(self):
        root = self.project(policy=None)
        self.assertIsNone(sel.load_policy(root))
        out = self.select_json(root, "status")
        self.assertFalse(out["enabled"])
        self.assertEqual(out["policy"]["status"], "missing")

    def test_disabled_example_is_loaded_but_inactive(self):
        root = self.project(policy=base_policy(enabled=False))
        self.assertIsNone(sel.load_policy(root))
        out = self.select_json(root, "status")
        self.assertFalse(out["enabled"])
        self.assertEqual(out["policy"]["status"], "disabled")
        self.assertEqual(out["policy"]["version"], 1)
        self.assertTrue(out["policy"]["digest"].startswith("sha256:"))

    def test_enabled_policy_applies_default_budgets_and_retention(self):
        root = self.project(policy=base_policy())
        policy = sel.load_policy(root)
        self.assertIsNotNone(policy)
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.mode, "enforce")
        self.assertEqual(policy.budgets["discovered"], 40)
        self.assertEqual(policy.budgets["per_source"], 20)
        self.assertEqual(policy.budgets["judgments"], 20)
        self.assertEqual(policy.budgets["archives"], 10)
        self.assertEqual(policy.retention["preview_days"], 30)
        self.assertEqual(policy.retention["identity_days"], 180)
        self.assertEqual(policy.retention["review_days"], 14)
        self.assertEqual(policy.retention["max_review_attempts"], 2)
        self.assertEqual(policy.retention["capacity"], 20000)

    def test_example_selection_yaml_is_valid_and_disabled(self):
        example = ROOT / "examples" / "selection.yaml"
        self.assertTrue(example.is_file(), "examples/selection.yaml is part of the change")
        root = self.project(policy=example.read_text(encoding="utf-8"))
        self.assertIsNone(sel.load_policy(root))
        out = self.select_json(root, "status")
        self.assertFalse(out["enabled"])

    def test_unknown_top_level_key_fails_closed(self):
        root = self.project(policy=base_policy(extra="nope"))
        with self.assertRaises(sel.SelectionConfigError):
            sel.load_policy(root)

    def test_missing_enabled_fails_closed(self):
        data = base_policy()
        data.pop("enabled")
        root = self.project(policy=data)
        with self.assertRaises(sel.SelectionConfigError):
            sel.load_policy(root)

    def test_bool_is_not_an_integer(self):
        data = base_policy(schema=True)
        root = self.project(policy=data)
        with self.assertRaises(sel.SelectionConfigError):
            sel.load_policy(root)

    def test_shadow_mode_is_rejected(self):
        root = self.project(policy=base_policy(mode="shadow"))
        with self.assertRaises(sel.SelectionConfigError) as ctx:
            sel.load_policy(root)
        self.assertIn("shadow", str(ctx.exception))

    def test_rule_requires_supported_evidence_kind(self):
        data = base_policy()
        data["rules"][0]["evidence"] = "vibes"
        root = self.project(policy=data)
        with self.assertRaises(sel.SelectionConfigError):
            sel.load_policy(root)

    def test_rule_cannot_declare_defer_outcome(self):
        data = base_policy()
        data["rules"][0]["outcome"] = "defer"
        root = self.project(policy=data)
        with self.assertRaises(sel.SelectionConfigError):
            sel.load_policy(root)

    def test_duplicate_rule_ids_are_rejected(self):
        data = base_policy()
        data["rules"][1]["id"] = data["rules"][0]["id"]
        root = self.project(policy=data)
        with self.assertRaises(sel.SelectionConfigError):
            sel.load_policy(root)

    def test_budget_must_be_positive_integer(self):
        root = self.project(policy=base_policy(budgets={"discovered": 0}))
        with self.assertRaises(sel.SelectionConfigError):
            sel.load_policy(root)

    def test_unknown_budget_key_is_rejected(self):
        root = self.project(policy=base_policy(budgets={"discovered": 5, "magic": 3}))
        with self.assertRaises(sel.SelectionConfigError):
            sel.load_policy(root)

    def test_identity_retention_must_outlive_discovery_lookback(self):
        root = self.project(policy=base_policy(retention={"identity_days": 30}))
        with self.assertRaises(sel.SelectionConfigError):
            sel.load_policy(root)

    def test_policy_digest_changes_with_content(self):
        root = self.project(policy=base_policy())
        first = sel.load_policy(root).digest
        root2 = self.project(policy=base_policy(version=2))
        second = sel.load_policy(root2).digest
        self.assertNotEqual(first, second)


class SelectStatusCliTests(ProjectCase):
    def test_status_reports_ledger_counts_without_writing(self):
        root = self.project(policy=base_policy())
        before = list((root / ".state").rglob("*"))
        out = self.select_json(root, "status")
        self.assertTrue(out["enabled"])
        self.assertEqual(out["counts"], {})
        self.assertEqual(out["records"], 0)
        self.assertFalse((root / ".state" / "selection").exists())
        self.assertEqual(before, list((root / ".state").rglob("*")))

    def test_mutations_refuse_a_disabled_policy(self):
        root = self.project(policy=base_policy(enabled=False))
        res = self.run_select(root, "decide", "--key", "https://example.com/a",
                              "--outcome", "reject", "--rule", "routine-roundup",
                              "--reason", "routine", "--evidence", "routine")
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("disabled", (res.stderr + res.stdout).lower())

    def test_invalid_policy_is_a_visible_error(self):
        root = self.project(policy=base_policy(mode="shadow"))
        res = self.run_select(root, "status")
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("shadow", res.stderr + res.stdout)


class PreviewTests(ProjectCase):
    """Preview is bounded, non-archiving and identity/hash bound."""

    def content_file(self, root: Path, text: str, name: str = "section.md") -> Path:
        path = root / ".state" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_preview_uses_existing_section_content_and_keeps_fragment_identity(self):
        root = self.project(policy=base_policy())
        url = "https://example.com/docs#release-2"
        body = "## Release 2\n\nMaterial platform release with original detail."
        content = self.content_file(root, body)
        cand = candidate(url, content_file=str(content), cache_name="docs.md")
        run = self.write_run(root, [cand])
        self.register(root, [cand], run=run)

        out = self.select_json(root, "preview", "--url", url, "--run", str(run))
        self.assertEqual(out["key"], url)
        self.assertEqual(out["scope"], "section")
        self.assertTrue(out["body_hash"].startswith("sha256:"))
        self.assertGreater(out["chars"], 10)
        self.assertEqual(out["policy_digest"], sel.load_policy(root).digest)
        self.assertEqual(out["completeness"], "unknown")
        self.assertTrue(Path(out["preview_path"]).is_file())

    def test_preview_dry_run_writes_nothing(self):
        root = self.project(policy=base_policy())
        url = "https://example.com/docs#release-3"
        content = self.content_file(root, "Section body for a dry run.")
        cand = candidate(url, content_file=str(content))
        run = self.write_run(root, [cand])
        before = sorted(str(p) for p in (root / ".state").rglob("*"))

        out = self.select_json(root, "preview", "--url", url, "--run", str(run), "--dry-run")
        self.assertGreater(out["chars"], 0)
        self.assertFalse(out["cache_hit"])
        self.assertFalse((root / ".state" / "selection").exists())
        self.assertEqual(before, sorted(str(p) for p in (root / ".state").rglob("*")))

    def test_preview_cache_is_body_hash_bound(self):
        root = self.project(policy=base_policy())
        url = "https://example.com/docs#release-4"
        content = self.content_file(root, "First recorded section body.")
        cand = candidate(url, content_file=str(content))
        run = self.write_run(root, [cand])
        first = self.select_json(root, "preview", "--url", url, "--run", str(run))
        self.assertFalse(first["cache_hit"])

        second = self.select_json(root, "preview", "--url", url, "--run", str(run))
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["body_hash"], second["body_hash"])

        content.write_text("Changed section body with different bytes.", encoding="utf-8")
        third = self.select_json(root, "preview", "--url", url, "--run", str(run))
        self.assertFalse(third["cache_hit"])
        self.assertNotEqual(first["body_hash"], third["body_hash"])

    def test_preview_never_archives_or_localizes_media(self):
        root = self.project(policy=base_policy())
        url = "https://example.com/a#part"
        content = self.content_file(root, "![img](https://example.com/x.png) Body text.")
        cand = candidate(url, content_file=str(content))
        run = self.write_run(root, [cand])
        self.select_json(root, "preview", "--url", url, "--run", str(run))
        self.assertEqual(list((root / "library").rglob("entry.md")), [])
        self.assertFalse((root / ".state" / "pending.json").exists())
        self.assertFalse((root / "trash").exists())

    def test_preview_blocks_private_network_targets(self):
        root = self.project(policy=base_policy())
        res = self.run_select(root, "preview", "--url", "http://10.0.0.1/private")
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertIn("prohibited", (res.stderr + res.stdout).lower())
        self.assertFalse((root / ".state" / "selection").exists())


class _PreviewHandler(BaseHTTPRequestHandler):
    body = b""

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


class PreviewFetchTests(ProjectCase):
    def test_preview_fetches_a_page_without_archiving(self):
        _PreviewHandler.body = (
            b"<html><body><article><h1>Fixture release</h1>"
            b"<p>Material platform release with original detail.</p>"
            b"</article></body></html>"
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), _PreviewHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            root = self.project(policy=base_policy())
            url = f"http://127.0.0.1:{server.server_address[1]}/release"
            out = self.select_json(root, "preview", "--url", url)
            self.assertEqual(out["scope"], "page")
            self.assertIn("Material platform release", out["text"])
            self.assertEqual(list((root / "library").rglob("entry.md")), [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class DecideTests(ProjectCase):
    def prepare(self, root: Path, url: str, *, generated_at: str | None = None, **cand_over):
        cand = candidate(url, **cand_over)
        run = self.write_run(root, [cand], generated_at=generated_at)
        self.register(root, [cand], run=run)
        return cand, run

    def test_reject_is_terminal_and_writes_no_entry_or_seen(self):
        root = self.project(policy=base_policy())
        url = "https://example.com/a"
        _, run = self.prepare(root, url)

        out = self.decide_ok(
            root, url, "reject", rule="routine-roundup",
            quotes=("material platform release",), run=run,
        )
        self.assertEqual(out["state"], "rejected")
        record = self.ledger(root)["records"][url]
        self.assertEqual(record["outcome"], "reject")
        self.assertEqual(record["rule_id"], "routine-roundup")
        self.assertTrue(record["evidence"]["fingerprint"].startswith("sha256:"))
        self.assertEqual(json.loads((root / ".state" / "seen.json").read_text())["entries"], {})
        self.assertEqual(list((root / "library").rglob("entry.md")), [])
        self.assertFalse((root / ".state" / "pending.json").exists())

    def test_decide_rejects_unknown_and_unregistered_candidates(self):
        root = self.project(policy=base_policy())
        url = "https://example.com/a"
        cand = candidate(url)
        run = self.write_run(root, [cand])  # present in run, absent from ledger
        res = self.decide(root, url, "reject", rule="routine-roundup",
                          quotes=("material platform release",), run=run)
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertIn("not registered", res.stderr)

        res = self.decide(root, "https://example.com/foreign", "reject",
                          rule="routine-roundup", quotes=("material platform release",), run=run)
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertIn("current run", res.stderr)

    def test_decide_requires_the_current_run(self):
        root = self.project(policy=base_policy())
        url = "https://example.com/a"
        self.prepare(root, url, generated_at=stamp(hours=-2))
        newer = self.write_run(root, [candidate(url)], generated_at=stamp(minutes=5))
        res = self.decide(root, url, "reject", rule="routine-roundup",
                          quotes=("material platform release",), run=newer)
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertIn("stale", res.stderr.lower())

    def test_quotes_must_occur_in_the_recorded_metadata(self):
        root = self.project(policy=base_policy())
        url = "https://example.com/a"
        _, run = self.prepare(root, url)
        res = self.decide(root, url, "reject", rule="routine-roundup",
                          quotes=("a claim nobody wrote",), run=run)
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertIn("evidence", res.stderr.lower())
        self.assertEqual(self.ledger(root)["records"][url]["outcome"], "")

    def test_rule_outcome_must_match_decision(self):
        root = self.project(policy=base_policy())
        url = "https://example.com/a"
        _, run = self.prepare(root, url)
        res = self.decide(root, url, "accept", rule="routine-roundup",
                          quotes=("material platform release",), run=run)
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertIn("outcome", res.stderr.lower())

    def test_defer_requires_an_explicit_gap_and_follow_up(self):
        root = self.project(policy=base_policy())
        url = "https://example.com/a"
        _, run = self.prepare(root, url)
        res = self.decide(root, url, "defer", quotes=("material platform release",), run=run)
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertIn("gap", res.stderr.lower())

        out = self.decide_ok(
            root, url, "defer", quotes=("material platform release",), run=run,
            gap="Pricing and availability are not in the announcement.",
            follow_up="Re-check the vendor pricing page next week.",
        )
        self.assertEqual(out["state"], "unresolved")
        record = self.ledger(root)["records"][url]
        self.assertEqual(record["outcome"], "defer")
        self.assertIn("Pricing", record["gap"])

    def test_repeat_is_idempotent_and_contradiction_needs_reassessment(self):
        root = self.project(policy=base_policy())
        url = "https://example.com/a"
        _, run = self.prepare(root, url)
        kwargs = dict(rule="routine-roundup", quotes=("material platform release",), run=run)
        self.decide_ok(root, url, "reject", **kwargs)
        again = self.decide_ok(root, url, "reject", **kwargs)
        self.assertEqual(again["status"], "idempotent")
        self.assertEqual(len(self.ledger(root)["records"][url].get("history") or []), 1)

        res = self.decide(root, url, "accept", rule="material-release",
                          quotes=("material platform release",), run=run)
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertIn("reassess", res.stderr.lower())

    def test_judgment_budget_is_per_run(self):
        root = self.project(policy=base_policy(budgets={"judgments": 1, "archives": 5}))
        url_a, url_b = "https://example.com/a", "https://example.com/b"
        cands = [candidate(url_a), candidate(url_b)]
        run = self.write_run(root, cands)
        self.register(root, cands, run=run)
        self.decide_ok(root, url_a, "reject", rule="routine-roundup",
                       quotes=("material platform release",), run=run)
        res = self.decide(root, url_b, "reject", rule="routine-roundup",
                          quotes=("material platform release",), run=run)
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertIn("judgment budget", res.stderr)

    def test_accept_beyond_archive_budget_waits_visibly(self):
        root = self.project(policy=base_policy(budgets={"archives": 1, "judgments": 5}))
        url_a, url_b = "https://example.com/a", "https://example.com/b"
        cands = [candidate(url_a), candidate(url_b)]
        run = self.write_run(root, cands)
        self.register(root, cands, run=run)
        first = self.decide_ok(root, url_a, "accept", rule="material-release",
                               quotes=("material platform release",), run=run)
        second = self.decide_ok(root, url_b, "accept", rule="material-release",
                                quotes=("material platform release",), run=run)
        self.assertEqual(first["state"], "ready")
        self.assertEqual(second["state"], "budget_wait")

        later = self.write_run(root, cands, generated_at=stamp(hours=1))
        out = self.select_json(root, "ready", "--run", str(later))
        self.assertEqual(out["budget_wait"], [])
        states = {rec["key"]: rec["state"] for rec in out["admitted"]}
        self.assertEqual(states[url_b], "ready")
        self.assertEqual(self.ledger(root)["records"][url_b]["state"], "ready")

    def test_body_evidence_requires_a_preview(self):
        root = self.project(policy=base_policy())
        url = "https://example.com/a"
        cand, run = self.prepare(root, url)
        res = self.decide(root, url, "accept", rule="original-research",
                          quotes=("first-hand measurements",), run=run)
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertIn("preview", res.stderr.lower())

        content = root / ".state" / "a.md"
        content.write_text("Report with first-hand measurements from the lab.", encoding="utf-8")
        cand["content_file"] = str(content)
        run = self.write_run(root, [cand])
        self.register(root, [cand], run=run)
        self.select_json(root, "preview", "--url", url, "--run", str(run))
        out = self.decide_ok(root, url, "accept", rule="original-research",
                             quotes=("first-hand measurements",), run=run)
        self.assertEqual(out["state"], "ready")
        self.assertEqual(self.ledger(root)["records"][url]["evidence"]["kind"], "body")

    def test_corrupt_ledger_never_looks_like_empty_state(self):
        root = self.project(policy=base_policy())
        path = root / ".state" / "selection" / "ledger.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        res = self.run_select(root, "status")
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertIn("ledger", res.stderr)

        url = "https://example.com/a"
        run = self.write_run(root, [candidate(url)])
        res = self.decide(root, url, "reject", rule="routine-roundup",
                          quotes=("material platform release",), run=run)
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")


class _FeedHandler(BaseHTTPRequestHandler):
    routes: dict[str, bytes] = {}

    def do_GET(self):  # noqa: N802
        body = type(self).routes.get(self.path)
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def rss_feed(title: str, items: list[tuple[str, str, str]]) -> bytes:
    """items: (title, link, pubDate). Synthetic vendor feed."""
    body = "".join(
        "<item>"
        f"<title>{t}</title><link>{link}</link><pubDate>{pub}</pubDate>"
        f"<description>{t} summary for synthetic coverage.</description>"
        "</item>"
        for t, link, pub in items
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<rss version=\"2.0\"><channel><title>{title}</title>{body}</channel></rss>"
    ).encode("utf-8")


def rfc822(**delta) -> str:
    dt = datetime.now(TZ) + timedelta(**delta)
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0800")


class FeedCase(ProjectCase):
    """Isolated project + loopback synthetic RSS feeds."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FeedHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        super().setUp()
        _FeedHandler.routes = {}

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def set_feed(self, name: str, items: list[tuple[str, str, str]]) -> None:
        _FeedHandler.routes[f"/{name}.xml"] = rss_feed(name, items)

    def project_with_feeds(
        self,
        *,
        policy: dict | None = None,
        per_feed: int = 5,
        title_override: dict[int, str] | None = None,
        link_base: str | None = None,
    ) -> Path:
        base = link_base or self.url("")
        for sid in ("alpha", "beta"):
            items = [
                (
                    (title_override or {}).get(i) if (title_override or {}).get(i) and sid == "alpha"
                    else f"{sid} whitepaper {i}",
                    f"{base}/{sid}/{i}",
                    rfc822(days=-i, hours=-1),
                )
                for i in range(1, per_feed + 1)
            ]
            self.set_feed(sid, items)
        cfg = {
            "version": 1,
            "defaults": {"window_hours": 720, "max_items_per_source": 8, "backfill_items": 2},
            "sources": [
                {
                    "id": sid,
                    "label": sid.title(),
                    "type": "rss",
                    "adapter": "rss_feed",
                    "feed": self.url(f"/{sid}.xml"),
                    "enabled": True,
                }
                for sid in ("alpha", "beta")
            ],
        }
        root = self.project(policy=policy, sources=cfg)
        (root / ".state" / "sources.json").write_text(
            json.dumps({"sources": {"alpha": {"initialized": True}, "beta": {"initialized": True}}}),
            encoding="utf-8",
        )
        return root

    def run_fetch(self, root: Path, *args: str) -> dict:
        out_path = root / ".state" / "runs" / "latest-fetch.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(
            [PY, str(root / "scripts" / "fetch_sources.py"), "--json-out", str(out_path), *args],
            capture_output=True, text=True, cwd=str(root), timeout=180,
        )
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        return json.loads(out_path.read_text(encoding="utf-8"))

    def pending(self, root: Path) -> dict:
        path = root / ".state" / "pending.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"entries": {}}

    def mark_seen(self, root: Path, url: str) -> None:
        path = root / ".state" / "seen.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["entries"][url] = {"id": "fixture", "path": "library/tech/fixture/entry.md"}
        path.write_text(json.dumps(data), encoding="utf-8")


class FetchIntegrationTests(FeedCase):
    """Discovery budgets, rotation, suppression and commit recovery."""

    def test_disabled_policy_keeps_legacy_fetch(self):
        root = self.project_with_feeds(policy=None)
        out = self.run_fetch(root, "--max", "4")
        self.assertEqual(len(out["candidates"]), 4)
        self.assertNotIn("selection", out)
        self.assertFalse((root / ".state" / "selection").exists())
        entries = self.pending(root)["entries"]
        self.assertTrue(entries)
        self.assertTrue(any(rec.get("reason") == "awaiting_archive" for rec in entries.values()))

    def test_discovery_and_per_source_budgets(self):
        root = self.project_with_feeds(
            policy=base_policy(budgets={"discovered": 3, "per_source": 2})
        )
        out = self.run_fetch(root)
        candidates = out["candidates"]
        self.assertEqual(len(candidates), 3)
        per_source: dict[str, int] = {}
        for cand in candidates:
            per_source[cand["source_id"]] = per_source.get(cand["source_id"], 0) + 1
        self.assertTrue(all(count <= 2 for count in per_source.values()), per_source)
        self.assertEqual(out["selection"]["enabled"], True)
        self.assertEqual(out["selection"]["registered"], 3)
        self.assertEqual(len(self.ledger(root)["records"]), 3)
        self.assertTrue(
            {sel.identity_for_candidate(c) for c in candidates}
            <= set(self.pending(root)["entries"])
        )

    def test_rotating_start_source_shares_a_scarce_budget(self):
        root = self.project_with_feeds(policy=base_policy(budgets={"discovered": 1, "per_source": 5}))
        first = self.run_fetch(root)
        self.assertEqual(len(first["candidates"]), 1)
        first_src = first["candidates"][0]["source_id"]
        self.mark_seen(root, first["candidates"][0]["url"])

        second = self.run_fetch(root)
        self.assertEqual(len(second["candidates"]), 1)
        self.assertNotEqual(second["candidates"][0]["source_id"], first_src)

        third = self.run_fetch(root)
        self.assertEqual(third["candidates"][0]["source_id"], first_src)
        self.assertNotEqual(third["candidates"][0]["url"], first["candidates"][0]["url"])

    def test_rejected_candidate_is_suppressed_and_never_parked_again(self):
        root = self.project_with_feeds(policy=base_policy())
        first = self.run_fetch(root, "--max", "6")
        rejected = first["candidates"][0]
        run = root / ".state" / "runs" / "latest-fetch.json"
        self.decide_ok(root, rejected["url"], "reject", rule="routine-roundup",
                       quotes=("summary for synthetic coverage",), run=run)

        second = self.run_fetch(root, "--max", "6")
        urls = {c["url"] for c in second["candidates"]}
        self.assertNotIn(rejected["url"], urls)
        self.assertGreaterEqual(second["selection"]["suppressed"], 1)
        self.assertNotIn(rejected["url"], self.pending(root)["entries"])
        self.assertEqual(self.ledger(root)["records"][rejected["url"]]["outcome"], "reject")

    def test_stale_pending_cannot_resurrect_rejected_work(self):
        root = self.project_with_feeds(policy=base_policy())
        first = self.run_fetch(root, "--max", "6")
        rejected = first["candidates"][0]
        run = root / ".state" / "runs" / "latest-fetch.json"
        self.decide_ok(root, rejected["url"], "reject", rule="routine-roundup",
                       quotes=("summary for synthetic coverage",), run=run)

        pending_path = root / ".state" / "pending.json"
        data = json.loads(pending_path.read_text(encoding="utf-8"))
        key = sel.identity_for_candidate(rejected)
        data["entries"][key] = dict(rejected, reason="awaiting_archive")
        pending_path.write_text(json.dumps(data), encoding="utf-8")

        second = self.run_fetch(root, "--max", "6")
        self.assertNotIn(rejected["url"], {c["url"] for c in second["candidates"]})
        self.assertNotIn(key, self.pending(root)["entries"])

    def test_dry_run_writes_no_ledger_pending_or_run_state(self):
        root = self.project_with_feeds(policy=base_policy())
        out_path = root / ".state" / "runs" / "dry.json"
        before = _support.state_digest(root / ".state" / "sources.json")
        res = subprocess.run(
            [PY, str(root / "scripts" / "fetch_sources.py"), "--max", "4",
             "--dry-run", "--json-out", str(out_path)],
            capture_output=True, text=True, cwd=str(root), timeout=180,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual(len(out["candidates"]), 4)
        self.assertFalse((root / ".state" / "selection").exists())
        self.assertFalse((root / ".state" / "pending.json").exists())
        self.assertEqual(before, _support.state_digest(root / ".state" / "sources.json"))

    def test_commit_recovers_ledger_work_missing_from_the_queue(self):
        root = self.project_with_feeds(policy=base_policy())
        first = self.run_fetch(root, "--max", "4")
        accepted = first["candidates"][0]
        run = root / ".state" / "runs" / "latest-fetch.json"
        self.decide_ok(root, accepted["url"], "accept", rule="material-release",
                       quotes=("summary for synthetic coverage",), run=run)
        key = sel.identity_for_candidate(accepted)
        # Accepted work must stay retryable even if it was first seen long ago.
        ledger_path = root / ".state" / "selection" / "ledger.json"
        ledger_data = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger_data["records"][key]["first_seen"] = iso_ago(days=60)
        ledger_path.write_text(json.dumps(ledger_data), encoding="utf-8")

        # Simulate a crash between the ledger write and the queue write: the
        # accepted record survives but the pending queue is empty, and the item
        # rolls out of the feed.
        (root / ".state" / "pending.json").unlink()
        self.set_feed("alpha", [("alpha newer item", self.url("/alpha/new"), rfc822(hours=-1))])
        self.set_feed("beta", [("beta newer item", self.url("/beta/new"), rfc822(hours=-1))])

        self.run_fetch(root, "--max", "2")
        entries = self.pending(root)["entries"]
        self.assertIn(key, entries)
        self.assertEqual(entries[key].get("reason"), "ledger_recovery")

        # The recovered record is genuinely retryable: a later run re-parks it
        # with a live queue life instead of expiring it from the pending TTL.
        self.run_fetch(root, "--max", "4")
        entries = self.pending(root)["entries"]
        self.assertIn(key, entries)
        self.assertIn(entries[key].get("reason"), {"awaiting_archive", "global_max"})
        self.assertEqual(
            sel.load_ledger(root)["records"][key]["state"], "ready"
        )

    def test_changed_metadata_requires_reassessment_without_requeue(self):
        root = self.project_with_feeds(policy=base_policy())
        first = self.run_fetch(root, "--max", "8")
        rejected = first["candidates"][0]
        run = root / ".state" / "runs" / "latest-fetch.json"
        self.decide_ok(root, rejected["url"], "reject", rule="routine-roundup",
                       quotes=("summary for synthetic coverage",), run=run)

        for sid in ("alpha", "beta"):
            items = [
                (
                    "edited vendor headline" if i == 1 and sid == "alpha" else f"{sid} whitepaper {i}",
                    self.url(f"/{sid}/{i}"),
                    rfc822(days=-i, hours=-1),
                )
                for i in range(1, 6)
            ]
            self.set_feed(sid, items)

        second = self.run_fetch(root, "--max", "8")
        self.assertGreaterEqual(second["selection"]["changed_observed"], 1)
        record = self.ledger(root)["records"][rejected["url"]]
        self.assertEqual(record["outcome"], "reject")
        self.assertTrue(record.get("reassessment_required"))
        self.assertNotIn(rejected["url"], {c["url"] for c in second["candidates"]})

    def test_capacity_aborts_commit_without_evicting_state(self):
        root = self.project_with_feeds(policy=base_policy(retention={"capacity": 2}))
        out_path = root / ".state" / "runs" / "capacity.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(
            [PY, str(root / "scripts" / "fetch_sources.py"), "--max", "5",
             "--json-out", str(out_path)],
            capture_output=True, text=True, cwd=str(root), timeout=180,
        )
        self.assertEqual(res.returncode, 4, res.stderr)
        self.assertIn("capacity", res.stderr.lower())
        self.assertFalse((root / ".state" / "selection" / "ledger.json").exists())
        self.assertFalse((root / ".state" / "pending.json").exists())


PUBLIC_IP = "93.184.216.34"  # routable literal: no DNS/HTTP dependency


class AutoAdmissionTests(ProjectCase):
    """Automatic add needs a current accepted admission; manual add does not."""

    def setUp(self):
        super().setUp()
        self._n = 0

    def admission_project(self, **policy_over):
        root = self.project(policy=base_policy(**policy_over))
        self._n += 1
        self.url = f"https://{PUBLIC_IP}/release/{self._n}"
        self.content = root / ".state" / "content.md"
        self.content.write_text(
            "AI 平台发布：这是用于验证自动准入的合成正文，内容完全虚构。\n\n"
            "This synthetic body only exists for the admission test.\n",
            encoding="utf-8",
        )
        cand = candidate(
            self.url,
            title="AI platform release",
            summary="AI platform release",
            content_file=str(self.content),
        )
        run = self.write_run(root, [cand])
        self.register(root, [cand], run=run)
        return root, cand, run

    def run_add(self, root: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PY, str(root / "scripts" / "archive_url.py"), *args],
            capture_output=True, text=True, cwd=str(root), timeout=120,
        )

    def add(self, root: Path, *args: str) -> subprocess.CompletedProcess:
        return self.run_add(
            root, self.url, "--content-file", str(self.content),
            "--original-title", "AI platform release",
            "--published", "2026-11-10", "--language", "en", *args,
        )

    def test_automatic_add_without_admission_is_blocked(self):
        root, _, _ = self.admission_project()
        res = self.add(root, "--collection", "auto")
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("admission", (res.stderr + res.stdout).lower())
        self.assertEqual(list((root / "library").rglob("entry.md")), [])
        self.assertEqual(json.loads((root / ".state" / "seen.json").read_text())["entries"], {})

    def test_automatic_add_after_accept_archives_and_marks_the_ledger(self):
        root, cand, run = self.admission_project()
        self.decide_ok(root, self.url, "accept", rule="material-release",
                       quotes=("AI platform release",), run=run)
        res = self.add(root, "--collection", "auto")
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["status"], "ok")
        record = self.ledger(root)["records"][sel.identity_for_candidate(cand)]
        self.assertEqual(record["state"], "archived")
        self.assertTrue(record.get("archived_at"))
        self.assertEqual(record["entry"], payload["id"])
        self.assertEqual(list((root / "library").rglob("entry.md")), [Path(payload["path"])])

    def test_budget_wait_cannot_be_archived_until_admitted(self):
        root, _, run = self.admission_project(budgets={"archives": 1, "judgments": 5})
        second_url = f"https://{PUBLIC_IP}/release/second"
        cand2 = candidate(second_url, title="AI platform release two", summary="AI platform release")
        cand1 = candidate(self.url, title="AI platform release", summary="AI platform release",
                          content_file=str(self.content))
        run2 = self.write_run(root, [cand1, cand2])
        self.register(root, [cand1, cand2], run=run2)
        self.decide_ok(root, self.url, "accept", rule="material-release",
                       quotes=("AI platform release",), run=run2)
        out = self.decide_ok(root, second_url, "accept", rule="material-release",
                             quotes=("AI platform release",), run=run2)
        self.assertEqual(out["state"], "budget_wait")

        res = self.run_add(
            root, second_url, "--content-file", str(self.content),
            "--original-title", "AI platform release two",
            "--published", "2026-11-10", "--language", "en", "--collection", "auto",
        )
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("budget", (res.stderr + res.stdout).lower())
        self.assertEqual(self.ledger(root)["records"][sel.identity_for_url(second_url)]["state"],
                         "budget_wait")

    def test_manual_add_overrides_rejection_without_deleting_history(self):
        root, cand, run = self.admission_project()
        self.decide_ok(root, self.url, "reject", rule="routine-roundup",
                       quotes=("AI platform release",), run=run)
        res = self.add(root)  # no --collection: a deliberate user save
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        record = self.ledger(root)["records"][sel.identity_for_candidate(cand)]
        self.assertEqual(record["outcome"], "reject")
        actions = [h.get("action") for h in record.get("history") or []]
        self.assertIn("manual_override", actions)
        self.assertEqual(len(list((root / "library").rglob("entry.md"))), 1)

    def test_reused_preview_must_match_its_recorded_hash(self):
        root, cand, run = self.admission_project()
        self.decide_ok(root, self.url, "accept", rule="material-release",
                       quotes=("AI platform release",), run=run)
        preview = self.select_json(root, "preview", "--url", self.url, "--run", str(run))
        preview_path = Path(preview["preview_path"])

        preview_path.write_text("tampered preview bytes", encoding="utf-8")
        res = self.run_add(
            root, self.url, "--content-file", str(preview_path),
            "--original-title", "AI platform release",
            "--published", "2026-11-10", "--language", "en", "--collection", "auto",
        )
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("hash", (res.stderr + res.stdout).lower())
        self.assertEqual(list((root / "library").rglob("entry.md")), [])

        fresh = self.select_json(root, "preview", "--url", self.url, "--run", str(run))
        res = self.run_add(
            root, self.url, "--content-file", fresh["preview_path"],
            "--original-title", "AI platform release",
            "--published", "2026-11-10", "--language", "en", "--collection", "auto",
        )
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)

    def test_failure_after_admission_marks_archive_failure(self):
        root, cand, run = self.admission_project()
        self.decide_ok(root, self.url, "accept", rule="material-release",
                       quotes=("AI platform release",), run=run)
        forbidden = root / "not-allowed.md"
        forbidden.write_text("outside .state", encoding="utf-8")
        res = self.run_add(
            root, self.url, "--content-file", str(forbidden),
            "--original-title", "AI platform release",
            "--published", "2026-11-10", "--language", "en", "--collection", "auto",
        )
        self.assertEqual(res.returncode, 1, res.stderr)
        record = self.ledger(root)["records"][sel.identity_for_candidate(cand)]
        self.assertEqual(record["state"], "archive_failure")
        self.assertTrue(record.get("failure_reason"))
        self.assertEqual(list((root / "library").rglob("entry.md")), [])


class DailySelectionTests(FeedCase):
    """Daily gate accounting and digest visibility for selection outcomes."""

    def run_daily(self, root: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PY, str(root / "scripts" / "daily_gate.py"), *args],
            capture_output=True, text=True, cwd=str(root), timeout=180,
        )

    def gate(self, root: Path) -> dict:
        return json.loads((root / ".state" / "runs" / "daily-gate.json").read_text(encoding="utf-8"))

    def latest_candidates(self, root: Path) -> list[dict]:
        path = root / ".state" / "runs" / "latest-fetch.json"
        return json.loads(path.read_text(encoding="utf-8"))["candidates"]

    def test_daily_start_uses_policy_discovery_budget(self):
        root = self.project_with_feeds(
            policy=base_policy(budgets={"discovered": 3, "per_source": 2})
        )
        res = self.run_daily(root, "start")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(self.latest_candidates(root)), 3)
        self.assertTrue(self.gate(root).get("selection"))

    def test_all_rejected_run_is_accounted_not_zero_news(self):
        root = self.project_with_feeds(policy=base_policy(budgets={"discovered": 4}))
        self.assertEqual(self.run_daily(root, "start").returncode, 0)
        run = root / ".state" / "runs" / "latest-fetch.json"
        candidates = self.latest_candidates(root)
        for cand in candidates:
            self.decide_ok(root, cand["url"], "reject", rule="routine-roundup",
                           quotes=("summary for synthetic coverage",), run=run)

        res = self.run_daily(root, "finish")
        self.assertEqual(res.returncode, 0, res.stderr)
        gate = self.gate(root)
        self.assertEqual(gate["status"], "ok")
        self.assertEqual(gate["selection"]["counts"].get("rejected"), len(candidates))
        self.assertIn("选题", res.stdout)
        self.assertIn("排除", res.stdout)
        self.assertNotIn("今日无新增", res.stdout)
        self.assertEqual(json.loads((root / ".state" / "seen.json").read_text())["entries"], {})
        self.assertEqual(list((root / "library").rglob("entry.md")), [])
        pending = self.pending(root)["entries"]
        self.assertFalse({sel.identity_for_candidate(c) for c in candidates} & set(pending))

    def test_omitted_judgment_fails_finish(self):
        root = self.project_with_feeds(policy=base_policy(budgets={"discovered": 4}))
        self.assertEqual(self.run_daily(root, "start").returncode, 0)
        run = root / ".state" / "runs" / "latest-fetch.json"
        candidates = self.latest_candidates(root)
        self.decide_ok(root, candidates[0]["url"], "reject", rule="routine-roundup",
                       quotes=("summary for synthetic coverage",), run=run)

        res = self.run_daily(root, "finish")
        self.assertEqual(res.returncode, 1, res.stdout)
        gate = self.gate(root)
        self.assertEqual(gate["status"], "failed")
        self.assertIn("unjudged", (gate.get("error") or "").lower())
        self.assertTrue(gate["selection"]["unjudged"])
        pending = self.pending(root)["entries"]
        key = sel.identity_for_candidate(candidates[1])
        self.assertIn(key, pending)

    def test_accepted_but_unarchived_fails_finish(self):
        root = self.project_with_feeds(policy=base_policy(budgets={"discovered": 2}))
        self.assertEqual(self.run_daily(root, "start").returncode, 0)
        run = root / ".state" / "runs" / "latest-fetch.json"
        for cand in self.latest_candidates(root):
            self.decide_ok(root, cand["url"], "accept", rule="material-release",
                           quotes=("summary for synthetic coverage",), run=run)

        res = self.run_daily(root, "finish")
        self.assertEqual(res.returncode, 1, res.stdout)
        gate = self.gate(root)
        self.assertEqual(gate["status"], "failed")
        self.assertIn("pending_archive", (gate.get("error") or ""))
        self.assertTrue(gate["selection"]["pending_archive"])

    def test_technical_failure_is_accounted_but_kept_visible(self):
        root = self.project_with_feeds(policy=base_policy(budgets={"discovered": 2}))
        self.assertEqual(self.run_daily(root, "start").returncode, 0)
        run = root / ".state" / "runs" / "latest-fetch.json"
        candidates = self.latest_candidates(root)
        for cand in candidates[:-1]:
            self.decide_ok(root, cand["url"], "reject", rule="routine-roundup",
                           quotes=("summary for synthetic coverage",), run=run)
        failed = candidates[-1]
        res = self.run_daily(root, "fail-candidate", failed["url"],
                            "--reason", "web_extract 502")
        self.assertEqual(res.returncode, 0, res.stderr)

        res = self.run_daily(root, "finish")
        self.assertEqual(res.returncode, 0, res.stderr)
        gate = self.gate(root)
        self.assertEqual(gate["selection"]["counts"].get("technical_failure"), 1)
        self.assertIn("抓取失败", res.stdout)
        self.assertNotIn("今日无新增", res.stdout)

    def test_accepted_item_passes_add_finalize_index_audit_digest(self):
        root = self.project_with_feeds(
            policy=base_policy(budgets={"discovered": 1}), link_base=f"https://{PUBLIC_IP}"
        )
        self.assertEqual(self.run_daily(root, "start").returncode, 0)
        run = root / ".state" / "runs" / "latest-fetch.json"
        cand = self.latest_candidates(root)[0]
        out = self.decide_ok(root, cand["url"], "accept", rule="material-release",
                             quotes=("summary for synthetic coverage",), run=run)
        self.assertEqual(out["state"], "ready")

        content = root / ".state" / "article.md"
        content.write_text(
            "AI 平台发布合成正文：这是一段完全虚构的合成文章正文，用于验证订阅选题"
            "端到端归档流程。文中没有任何真实公司、产品或个人信息。\n",
            encoding="utf-8",
        )
        add = subprocess.run(
            [PY, str(root / "scripts" / "archive_url.py"), cand["url"],
             "--content-file", str(content), "--collection", "auto",
             "--original-title", "Synthetic subscription release",
             "--published", "2026-11-10", "--language", "en", "--category", "tech"],
            capture_output=True, text=True, cwd=str(root), timeout=120,
        )
        self.assertEqual(add.returncode, 0, add.stderr + add.stdout)
        added = json.loads(add.stdout)

        summary = "合成摘要验证" * 20  # 120 CJK characters: short-article range
        fin = subprocess.run(
            [PY, str(root / "scripts" / "finalize_entry.py"), added["id"],
             "--title-zh", "合成订阅选题标题", "--summary", summary,
             "--category", "tech", "--context", "work", "--status", "archived"],
            capture_output=True, text=True, cwd=str(root), timeout=120,
        )
        self.assertEqual(fin.returncode, 0, fin.stderr + fin.stdout)
        self.assertEqual(
            self.ledger(root)["records"][sel.identity_for_candidate(cand)]["state"], "archived"
        )

        res = self.run_daily(root, "finish")
        self.assertEqual(res.returncode, 0, res.stderr)
        gate = self.gate(root)
        self.assertEqual(gate["status"], "ok")
        self.assertEqual(gate["selection"]["counts"].get("archived"), 1)
        self.assertIn("合成订阅选题标题", res.stdout)
        self.assertFalse((root / "publish.yaml").exists())


def iso_ago(**delta) -> str:
    return (datetime.now(TZ) - timedelta(**delta)).isoformat(timespec="seconds")


class LifecycleTests(ProjectCase):
    """Reassessment, retention/capacity and cleanup are explicit and bounded."""

    def decided(self, root: Path, outcome: str = "reject", **cand_over):
        url = "https://example.com/a"
        cand = candidate(url, **cand_over)
        run = self.write_run(root, [cand])
        self.register(root, [cand], run=run)
        rule = "routine-roundup" if outcome == "reject" else "material-release"
        self.decide_ok(root, url, outcome, rule=rule,
                       quotes=("material platform release",), run=run)
        return url, cand, run

    def backdate(self, root: Path, key: str, **delta) -> None:
        path = root / ".state" / "selection" / "ledger.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        stamp = iso_ago(**delta)
        data["records"][key]["decided_at"] = stamp
        data["records"][key]["updated_at"] = stamp
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_reassess_plan_is_read_only_then_apply_rebinds(self):
        root = self.project(policy=base_policy())
        url, cand, run = self.decided(root)
        before = (root / ".state" / "selection" / "ledger.json").read_bytes()

        plan = self.select_json(root, "reassess", "--key", url)
        self.assertEqual(plan["status"], "plan")
        self.assertEqual(plan["current"]["outcome"], "reject")
        self.assertFalse(plan["applied"])
        self.assertEqual(before, (root / ".state" / "selection" / "ledger.json").read_bytes())

        newer = self.write_run(root, [cand], generated_at=stamp(minutes=10))
        out = self.select_json(root, "reassess", "--key", url, "--apply", "--run", str(newer))
        self.assertEqual(out["status"], "ok")
        record = self.ledger(root)["records"][url]
        self.assertEqual(record["outcome"], "")
        self.assertEqual(record["state"], "awaiting_judgment")
        self.assertEqual(record["review_attempts"], 1)
        self.assertEqual(record["run_generated_at"], json.loads(
            newer.read_text(encoding="utf-8"))["generated_at"])
        self.assertTrue(any(h.get("action") == "reassess" for h in record["history"]))
        self.assertEqual(record["history"][-1]["previous_reason"], "synthetic editorial reason")

        accepted = self.decide_ok(root, url, "accept", rule="material-release",
                                  quotes=("material platform release",), run=newer)
        self.assertEqual(accepted["state"], "ready")

    def test_reassess_is_limited_to_two_explicit_attempts(self):
        root = self.project(policy=base_policy())
        url, cand, _ = self.decided(root)
        run = self.write_run(root, [cand], generated_at=stamp(minutes=10))
        self.select_json(root, "reassess", "--key", url, "--apply", "--run", str(run))
        self.decide_ok(root, url, "reject", rule="routine-roundup",
                       quotes=("material platform release",), run=run)
        self.select_json(root, "reassess", "--key", url, "--apply", "--run", str(run))
        self.decide_ok(root, url, "reject", rule="routine-roundup",
                       quotes=("material platform release",), run=run)
        res = self.run_select(root, "reassess", "--key", url, "--apply", "--run", str(run))
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertIn("attempt", res.stderr.lower())

    def test_reassess_can_reopen_after_the_identity_leaves_the_run(self):
        root = self.project(policy=base_policy())
        url, _, _ = self.decided(root)
        other = self.write_run(root, [candidate("https://example.com/other")],
                               generated_at=stamp(minutes=10))
        out = self.select_json(root, "reassess", "--key", url, "--apply",
                               "--run", str(other))
        self.assertTrue(out["reopen_pending_discovery"])
        record = self.ledger(root)["records"][url]
        self.assertEqual(record["outcome"], "")
        self.assertEqual(record["state"], "awaiting_judgment")
        self.assertEqual(record["run_generated_at"], "")
        # A fresh discovery/preview/decision is still required; the retained
        # metadata alone cannot authorize a decision in the newer run.
        res = self.decide(root, url, "reject", rule="routine-roundup",
                          quotes=("material platform release",), run=other)
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertEqual(self.ledger(root)["records"][url]["outcome"], "")


    def test_cleanup_plans_read_only_and_applies_retention(self):
        root = self.project(policy=base_policy())
        old_url, _, _ = self.decided(root)
        self.backdate(root, old_url, days=40)
        ancient_url = "https://example.com/ancient"
        cand = candidate(ancient_url)
        run = self.write_run(root, [cand], generated_at=stamp(minutes=5))
        self.register(root, [cand], run=run)
        self.decide_ok(root, ancient_url, "reject", rule="routine-roundup",
                       quotes=("material platform release",), run=run)
        self.backdate(root, ancient_url, days=200)

        before = (root / ".state" / "selection" / "ledger.json").read_bytes()
        plan = self.select_json(root, "cleanup")
        self.assertEqual(plan["status"], "plan")
        self.assertEqual(plan["expire_evidence"], 1)
        self.assertEqual(plan["drop_identities"], 1)
        self.assertFalse(plan["applied"])
        self.assertEqual(before, (root / ".state" / "selection" / "ledger.json").read_bytes())

        applied = self.select_json(root, "cleanup", "--apply")
        self.assertTrue(applied["applied"])
        ledger = self.ledger(root)
        self.assertNotIn(ancient_url, ledger["records"])
        rec = ledger["records"][old_url]
        self.assertEqual(rec["outcome"], "reject")
        self.assertTrue(rec.get("detail_expired"))
        self.assertEqual(rec["evidence"]["quotes"], [])
        self.assertTrue(rec["evidence"]["fingerprint"].startswith("sha256:"))

    def test_cleanup_protects_active_identities_and_preview_cache(self):
        root = self.project(policy=base_policy())
        url = "https://example.com/a"
        content = root / ".state" / "a.md"
        content.write_text("Material platform release with original detail.", encoding="utf-8")
        cand = candidate(url, content_file=str(content))
        run = self.write_run(root, [cand])
        self.register(root, [cand], run=run)
        preview = self.select_json(root, "preview", "--url", url, "--run", str(run))
        self.decide_ok(root, url, "accept", rule="material-release",
                       quotes=("material platform release",), run=run)
        self.backdate(root, url, days=90)

        old = (datetime.now(TZ) - timedelta(days=90)).timestamp()
        preview_path = Path(preview["preview_path"])
        os.utime(preview_path, (old, old))
        stray = root / ".state" / "selection" / "previews" / "deadbeefdeadbeef-0000000000000000.md"
        stray.write_text("stale", encoding="utf-8")
        os.utime(stray, (old, old))

        plan = self.select_json(root, "cleanup")
        self.assertNotIn(str(preview_path.name), plan["drop_previews"])
        self.assertIn(str(stray.name), plan["drop_previews"])
        self.assertEqual(plan["protected"], 1)
        self.assertTrue(preview_path.exists())

        self.select_json(root, "cleanup", "--apply")
        self.assertTrue(preview_path.exists(), "active admission must protect its cache")
        self.assertFalse(stray.exists())
        self.assertEqual(self.ledger(root)["records"][url]["state"], "ready")

    def test_capacity_refuses_new_state_without_evicting(self):
        root = self.project(policy=base_policy(retention={"capacity": 2}))
        urls = [f"https://example.com/{i}" for i in range(3)]
        cands = [candidate(u) for u in urls]
        run = self.write_run(root, cands)
        self.register(root, cands, run=run, only=cands[:2])
        before = (root / ".state" / "selection" / "ledger.json").read_bytes()

        with self.assertRaises(sel.SelectionCapacityError):
            sel.register_discovered(root, sel.load_policy(root), run_path=run, candidates=cands)
        self.assertEqual(before, (root / ".state" / "selection" / "ledger.json").read_bytes())

    def test_policy_change_blocks_reuse_but_rebinds_undecided_work(self):
        root = self.project(policy=base_policy())
        url = "https://example.com/a"
        awaiting = candidate("https://example.com/b")
        run = self.write_run(root, [candidate(url), awaiting])
        self.register(root, [candidate(url), awaiting], run=run)
        self.decide_ok(root, url, "accept", rule="material-release",
                       quotes=("material platform release",), run=run)

        (root / "selection.yaml").write_text(
            yaml.safe_dump(base_policy(version=2), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        new_policy = sel.load_policy(root)
        self.assertNotEqual(new_policy.digest, sel.parse_policy(base_policy()).digest)
        self.register(root, [candidate(url), awaiting], run=run)

        decided = self.ledger(root)["records"][url]
        self.assertEqual(decided["policy_digest"], sel.parse_policy(base_policy()).digest)
        with self.assertRaises(sel.SelectionStateError) as ctx:
            sel.admit(root, new_policy, url=url)
        self.assertIn("policy changed", str(ctx.exception))
        self.assertEqual(self.select_json(root, "status")["needs_reassessment"], 1)

        fresh = self.ledger(root)["records"][sel.identity_for_candidate(awaiting)]
        self.assertEqual(fresh["policy_digest"], new_policy.digest)


class StatusReportTests(ProjectCase):
    def test_status_exposes_unresolved_and_reassessment(self):
        root = self.project(policy=base_policy())
        url = "https://example.com/a"
        cand = candidate(url)
        run = self.write_run(root, [cand])
        self.register(root, [cand], run=run)
        self.decide_ok(root, url, "defer", quotes=("material platform release",),
                       gap="Pricing is missing.", follow_up="Check the pricing page.", run=run)

        out = self.select_json(root, "status")
        self.assertEqual(out["unresolved"], 1)
        self.assertEqual(out["needs_reassessment"], 0)

        path = root / ".state" / "selection" / "ledger.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["records"][url]["reassessment_required"] = True
        path.write_text(json.dumps(data), encoding="utf-8")
        out = self.select_json(root, "status")
        self.assertEqual(out["needs_reassessment"], 1)

    def test_digest_warns_when_the_selection_ledger_is_unreadable(self):
        root = self.project(policy=base_policy())
        (root / "index.csv").write_text("id,title\n", encoding="utf-8")
        ledger = root / ".state" / "selection" / "ledger.json"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text("{broken", encoding="utf-8")
        run = self.write_run(root, [])
        res = subprocess.run(
            [PY, str(root / "scripts" / "build_digest.py"), "--run", str(run)],
            capture_output=True, text=True, cwd=str(root), timeout=120,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("选题账本不可读", res.stdout)
        self.assertNotIn("\n今日无新增。", res.stdout)


if __name__ == "__main__":
    unittest.main()
