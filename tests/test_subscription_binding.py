#!/usr/bin/env python3
"""Decision/acquisition bindings, preview currency and policy status.

These are the healthy-behaviour regressions for repair contract A. They extend
``test_subscription_selection`` fixtures (synthetic scratch projects only).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_subscription_selection as fx  # noqa: E402

sel = fx.sel


class DecisionBindingTests(fx.ProjectCase):
    def test_changed_run_source_and_metadata_cannot_authorize_a_registered_key(self):
        root = self.project(fx.base_policy())
        cand = fx.candidate("https://example.com/bound-candidate",
                            summary="Original discovery evidence")
        self.register(root, [cand])
        run_path = root / ".state/runs/latest-fetch.json"
        altered = json.loads(run_path.read_text(encoding="utf-8"))
        altered["candidates"][0].update(
            source_id="different-source", summary="Forged decision evidence"
        )
        run_path.write_text(json.dumps(altered), encoding="utf-8")
        ledger_before = (root / ".state/selection/ledger.json").read_bytes()

        result = self.decide(
            root, cand["url"], "accept", rule="material-release",
            quotes=("Forged decision evidence",),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.ledger(root)["records"][cand["url"]]["outcome"], "")
        self.assertEqual(ledger_before,
                         (root / ".state/selection/ledger.json").read_bytes())

    def test_changed_source_id_alone_is_refused(self):
        root = self.project(fx.base_policy())
        cand = fx.candidate("https://example.com/source-bound")
        self.register(root, [cand])
        run_path = root / ".state/runs/latest-fetch.json"
        altered = json.loads(run_path.read_text(encoding="utf-8"))
        altered["candidates"][0]["source_id"] = "beta-other"
        run_path.write_text(json.dumps(altered), encoding="utf-8")
        result = self.decide(root, cand["url"], "accept", rule="material-release",
                             quotes=("material platform release",))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("source", (result.stdout + result.stderr).lower())

    def test_changed_accepted_metadata_requires_reassessment_before_admission(self):
        root = self.project(fx.base_policy())
        cand = fx.candidate("https://example.com/changed-open")
        self.register(root, [cand])
        self.decide_ok(root, cand["url"], "accept", rule="material-release",
                       quotes=("material platform release",))
        before = self.ledger(root)["records"][cand["url"]]
        original_fp = before["observed_fingerprint"]
        original_title = before["title"]

        cand.update(summary="Correction: prior material-release claim withdrawn",
                    title="Retraction")
        self.register(root, [cand])

        record = self.ledger(root)["records"][cand["url"]]
        self.assertTrue(record.get("reassessment_required"),
                        "changed observation on a decided-open record must be flagged")
        self.assertEqual(record["observed_fingerprint"], original_fp,
                         "evidence binding must not be refreshed away")
        self.assertEqual(record["title"], original_title)
        with self.assertRaises(sel.SelectionStateError):
            sel.admit(root, sel.load_policy(root), url=cand["url"])
        ready_out = self.select_json(root, "ready")
        self.assertTrue(
            any(w["key"] == cand["url"] and w["reason"] == "needs_reassessment"
                for w in ready_out["not_readmitted"]),
            ready_out,
        )

    def test_unchanged_retry_after_observation_is_still_supported(self):
        root = self.project(fx.base_policy())
        cand = fx.candidate("https://example.com/unchanged-retry")
        self.register(root, [cand])
        self.decide_ok(root, cand["url"], "accept", rule="material-release",
                       quotes=("material platform release",))
        self.register(root, [cand])
        self.assertFalse(
            self.ledger(root)["records"][cand["url"]].get("reassessment_required")
        )
        out = sel.admit(root, sel.load_policy(root), url=cand["url"])
        self.assertEqual(out["status"], "ok")


class LedgerStructureTests(fx.ProjectCase):
    def _ledger_path(self, root: Path) -> Path:
        return root / ".state" / "selection" / "ledger.json"

    def test_record_without_required_bindings_is_not_an_editorial_outcome(self):
        root = self.project(fx.base_policy())
        cand = fx.candidate("https://example.com/malformed-record")
        self.register(root, [cand])
        path = self._ledger_path(root)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["records"][cand["url"]] = {"outcome": "reject"}
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(sel.SelectionStateError):
            sel.load_ledger(root)

    def test_non_mapping_record_is_rejected(self):
        root = self.project(fx.base_policy())
        cand = fx.candidate("https://example.com/list-record")
        self.register(root, [cand])
        path = self._ledger_path(root)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["records"][cand["url"]] = ["reject"]
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(sel.SelectionStateError):
            sel.load_ledger(root)

    def test_unknown_state_and_boolean_counter_are_rejected(self):
        root = self.project(fx.base_policy())
        cand = fx.candidate("https://example.com/bad-state")
        self.register(root, [cand])
        path = self._ledger_path(root)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["records"][cand["url"]]["state"] = "probably_fine"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(sel.SelectionStateError):
            sel.load_ledger(root)

        data["records"][cand["url"]]["state"] = sel.STATE_AWAITING
        data["records"][cand["url"]]["review_attempts"] = True
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(sel.SelectionStateError):
            sel.load_ledger(root)


class PreviewCurrencyTests(fx.ProjectCase):
    def _preview_root(self, url: str):
        root = self.project(fx.base_policy(rules=[
            {"id": "material-release", "outcome": "accept",
             "description": "Material release.", "evidence": "either"},
        ]))
        content = root / ".state" / "sec.md"
        content.write_text("v1 body mentions material release", encoding="utf-8")
        cand = fx.candidate(url, content_file=str(content))
        run = self.write_run(root, [cand])
        self.register(root, [cand], run=run)
        return root, content, run

    def test_newer_same_run_preview_replaces_the_decision_binding(self):
        url = "https://example.com/docs#sec"
        root, content, run = self._preview_root(url)
        first = self.select_json(root, "preview", "--url", url, "--run", str(run))
        content.write_text("totally rewritten body, no material claim at all",
                           encoding="utf-8")
        second = self.select_json(root, "preview", "--url", url, "--run", str(run))
        self.assertNotEqual(first["body_hash"], second["body_hash"])

        stale = self.decide(root, url, "accept", rule="material-release",
                            quotes=("v1 body mentions",), run=run)
        self.assertEqual(stale.returncode, 3, stale.stdout + stale.stderr)

        fresh = self.decide_ok(root, url, "accept", rule="material-release",
                               quotes=("rewritten body",), run=run)
        self.assertEqual(fresh["state"], "ready")
        binding = self.ledger(root)["previews"][url]
        self.assertEqual(binding["body_hash"], second["body_hash"])
        self.assertEqual(binding["run_generated_at"],
                         json.loads(run.read_text(encoding="utf-8"))["generated_at"])

    def test_decision_binding_must_stay_inside_the_preview_subtree(self):
        url = "https://example.com/docs#escape"
        root, _content, run = self._preview_root(url)
        outside = root / ".state" / "outside-preview.md"
        outside.write_text("material release smuggled from outside the cache",
                           encoding="utf-8")
        import hashlib
        body_hash = "sha256:" + hashlib.sha256(outside.read_bytes()).hexdigest()
        path = root / ".state" / "selection" / "ledger.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["previews"] = {
            url: {
                "key": url,
                "url": url,
                "path": str(outside),
                "body_hash": body_hash,
                "policy_digest": sel.load_policy(root).digest,
                "run_generated_at": json.loads(run.read_text(encoding="utf-8"))["generated_at"],
            }
        }
        path.write_text(json.dumps(data), encoding="utf-8")
        result = self.decide(root, url, "accept", rule="material-release",
                             quotes=("smuggled from outside",), run=run)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.ledger(root)["records"][url]["outcome"], "")


class StatusInvalidPolicyTests(fx.ProjectCase):
    def test_invalid_policy_status_is_machine_readable_and_unsuccessful(self):
        root = self.project("schema: malformed\nenabled: true\n")
        res = self.run_select(root, "status")
        self.assertEqual(res.returncode, 2, res.stderr + res.stdout)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["policy"]["status"], "invalid")
        self.assertFalse(payload["enabled"])
        self.assertTrue(payload["policy"].get("error"))
        self.assertFalse((root / ".state" / "selection").exists())

    def test_invalid_policy_still_fails_mutations_closed(self):
        root = self.project("schema: malformed\nenabled: true\n")
        res = self.run_select(root, "decide", "--key", "https://example.com/x",
                              "--outcome", "reject", "--rule", "routine-roundup",
                              "--reason", "routine", "--evidence", "routine")
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertFalse((root / ".state" / "selection").exists())

    def test_nonregular_policy_path_is_not_silently_disabled(self):
        root = self.project(None)
        (root / "selection.yaml").mkdir()
        with self.assertRaises(sel.SelectionConfigError):
            sel.load_policy(root)
        res = self.run_select(root, "status")
        self.assertEqual(res.returncode, 2, res.stderr + res.stdout)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["policy"]["status"], "invalid")


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
