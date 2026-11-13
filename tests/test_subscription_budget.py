#!/usr/bin/env python3
"""Real per-run budgets and honest accounting (repair contract C).

Healthy-behaviour regressions for the current-run admission contract, monotonic
judgment usage and discovery-above-judgment accounting. Synthetic only.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_subscription_selection as fx  # noqa: E402

sel = fx.sel


class CurrentRunAdmissionTests(fx.ProjectCase):
    def test_ready_lists_only_current_run_admissions(self):
        root = self.project(fx.base_policy(budgets={"archives": 1}))
        first = fx.candidate("https://example.com/first")
        second = fx.candidate("https://example.com/second")
        run1 = self.write_run(root, [first], generated_at=fx.stamp(minutes=-5),
                              name="first.json")
        self.register(root, [first], run=run1)
        self.decide_ok(root, first["url"], "accept", rule="material-release",
                       quotes=("material platform release",), run=run1)
        run2 = self.write_run(root, [second])
        self.register(root, [second], run=run2)
        self.decide_ok(root, second["url"], "accept", rule="material-release",
                       quotes=("material platform release",), run=run2)

        result = self.select_json(root, "ready", "--run", str(run2))
        self.assertEqual([r["key"] for r in result["admitted"]], [second["url"]], result)
        self.assertLessEqual(result["count"], result["archives_limit"])
        self.assertLessEqual(result["archives_used"], result["archives_limit"])
        self.assertEqual(result["count"], result["archives_used"])
        self.assertTrue(any(w["key"] == first["url"]
                            for w in result.get("not_readmitted", [])), result)

    def test_budget_wait_promotion_takes_precedence_over_readmission(self):
        root = self.project(fx.base_policy(budgets={"archives": 1, "judgments": 5}))
        url_a, url_b = "https://example.com/a", "https://example.com/b"
        cands = [fx.candidate(url_a), fx.candidate(url_b)]
        run1 = self.write_run(root, cands, generated_at=fx.stamp(minutes=-5))
        self.register(root, cands, run=run1)
        self.decide_ok(root, url_a, "accept", rule="material-release",
                       quotes=("material platform release",), run=run1)
        self.decide_ok(root, url_b, "accept", rule="material-release",
                       quotes=("material platform release",), run=run1)
        later = self.write_run(root, cands, generated_at=fx.stamp(hours=1))
        out = self.select_json(root, "ready", "--run", str(later))
        self.assertEqual(out["budget_wait"], [])
        states = {rec["key"]: rec["state"] for rec in out["admitted"]}
        self.assertEqual(states[url_b], "ready")


    def test_prior_run_admission_is_renewed_in_a_current_run(self):
        root = self.project(fx.base_policy())
        cand = fx.candidate("https://example.com/renew-me")
        run1 = self.write_run(root, [cand], generated_at=fx.stamp(hours=-3),
                              name="old-run.json")
        self.register(root, [cand], run=run1)
        self.decide_ok(root, cand["url"], "accept", rule="material-release",
                       quotes=("material platform release",), run=run1)
        data = self.ledger(root)
        key = sel.identity_for_url(cand["url"])
        old_gen = json.loads(run1.read_text(encoding="utf-8"))["generated_at"]
        data["records"][key]["admitted_at"] = "2020-01-01T00:00:00+08:00"
        data["records"][key]["admitted_run"] = old_gen
        (root / ".state/selection/ledger.json").write_text(json.dumps(data),
                                                          encoding="utf-8")

        run2 = self.write_run(root, [cand], generated_at=fx.stamp(hours=1))
        self.register(root, [cand], run=run2)
        out = sel.admit(root, sel.load_policy(root), url=cand["url"])
        self.assertTrue(out["readmission"], out)
        record = self.ledger(root)["records"][key]
        self.assertEqual(record["admitted_run"],
                         json.loads(run2.read_text(encoding="utf-8"))["generated_at"])
        self.assertNotEqual(record["admitted_at"], "2020-01-01T00:00:00+08:00")

    def test_expired_admission_without_current_membership_still_blocks(self):
        root = self.project(fx.base_policy())
        cand = fx.candidate("https://example.com/no-renew")
        run1 = self.write_run(root, [cand], generated_at=fx.stamp(hours=-3),
                              name="old-run.json")
        self.register(root, [cand], run=run1)
        self.decide_ok(root, cand["url"], "accept", rule="material-release",
                       quotes=("material platform release",), run=run1)
        data = self.ledger(root)
        key = sel.identity_for_url(cand["url"])
        data["records"][key]["admitted_at"] = "2020-01-01T00:00:00+08:00"
        (root / ".state/selection/ledger.json").write_text(json.dumps(data),
                                                          encoding="utf-8")
        self.write_run(root, [fx.candidate("https://example.com/other")],
                       generated_at=fx.stamp(hours=1))
        with self.assertRaises(sel.SelectionStateError) as ctx:
            sel.admit(root, sel.load_policy(root), url=cand["url"])
        self.assertIn("expired", str(ctx.exception).lower())


class MonotonicJudgmentTests(fx.ProjectCase):
    def test_reassess_does_not_refund_a_judgment_slot(self):
        root = self.project(fx.base_policy(budgets={"judgments": 1, "archives": 9}))
        cands = [fx.candidate(f"https://example.com/j{i}") for i in range(3)]
        run = self.write_run(root, cands)
        self.register(root, cands, run=run)
        first = self.decide_ok(root, cands[0]["url"], "reject", rule="routine-roundup",
                               quotes=("material platform release",), run=run)
        self.assertEqual(first["budget"]["judgments_used"], 1)
        blocked = self.decide(root, cands[1]["url"], "reject", rule="routine-roundup",
                              quotes=("material platform release",), run=run)
        self.assertEqual(blocked.returncode, 3, blocked.stderr)

        reopened = self.select_json(root, "reassess", "--key", cands[0]["url"],
                                    "--apply", "--run", str(run),
                                    "--reason", "changed mind")
        self.assertEqual(reopened["status"], "ok")
        again = self.decide(root, cands[0]["url"], "accept", rule="material-release",
                            quotes=("material platform release",), run=run)
        self.assertEqual(again.returncode, 3, again.stdout + again.stderr)
        self.assertIn("judgment budget", again.stderr)
        gen = json.loads(run.read_text(encoding="utf-8"))["generated_at"]
        self.assertEqual(self.ledger(root)["runs"][gen]["judgments"], 1)

    def test_idempotent_repeat_stays_free(self):
        root = self.project(fx.base_policy(budgets={"judgments": 1}))
        url = "https://example.com/repeat"
        run = self.write_run(root, [fx.candidate(url)])
        self.register(root, [fx.candidate(url)], run=run)
        kwargs = dict(rule="routine-roundup",
                      quotes=("material platform release",), run=run)
        self.decide_ok(root, url, "reject", **kwargs)
        again = self.decide_ok(root, url, "reject", **kwargs)
        self.assertEqual(again["status"], "idempotent")
        gen = json.loads(run.read_text(encoding="utf-8"))["generated_at"]
        self.assertEqual(self.ledger(root)["runs"][gen]["judgments"], 1)

    def test_per_run_counters_are_bounded(self):
        root = self.project(fx.base_policy())
        data = sel.empty_ledger()
        data["runs"] = {
            f"2026-01-{day:02d}T00:00:00+08:00": {"judgments": 1, "archives": 0}
            for day in range(1, 32)
        }
        sel.save_ledger(root, data)
        runs = sel.load_ledger(root)["runs"]
        self.assertLessEqual(len(runs), sel.MAX_RUN_HISTORY)
        self.assertIn("2026-01-31T00:00:00+08:00", runs)


class JudgmentBudgetAccountingTests(fx.FeedCase):
    def daily(self, root: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [fx.PY, str(root / "scripts" / "daily_gate.py"), *args],
            capture_output=True, text=True, cwd=str(root), timeout=180,
        )

    def gate(self, root: Path) -> dict:
        return json.loads(
            (root / ".state" / "runs" / "daily-gate.json").read_text(encoding="utf-8")
        )

    def latest(self, root: Path) -> list[dict]:
        path = root / ".state" / "runs" / "latest-fetch.json"
        return json.loads(path.read_text(encoding="utf-8"))["candidates"]

    def test_discovery_above_judgment_budget_is_an_explicit_durable_wait(self):
        root = self.project_with_feeds(
            policy=fx.base_policy(
                budgets={"discovered": 3, "per_source": 3, "judgments": 2, "archives": 5}
            ),
            per_feed=3,
        )
        self.assertEqual(self.daily(root, "start").returncode, 0)
        candidates = self.latest(root)
        self.assertEqual(len(candidates), 3)
        run = root / ".state" / "runs" / "latest-fetch.json"
        for cand in candidates[:2]:
            self.decide_ok(root, cand["url"], "reject", rule="routine-roundup",
                           quotes=("summary for synthetic coverage",), run=run)

        res = self.daily(root, "finish")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        gate = self.gate(root)
        self.assertEqual(gate["status"], "ok")
        counts = gate["selection"]["counts"]
        self.assertEqual(counts.get("judgment_wait"), 1, gate["selection"])
        self.assertEqual(counts.get("unjudged"), 0)
        self.assertEqual(counts.get("budget_wait"), 0)
        waiting = sel.identity_for_candidate(candidates[2])
        self.assertEqual(self.ledger(root)["records"][waiting]["state"],
                         "judgment_wait")
        self.assertIn(waiting, self.pending(root)["entries"])

        # The durable wait is judged in a later run, without re-archiving the rest.
        self.assertEqual(self.daily(root, "start").returncode, 0)
        again = self.latest(root)
        target = [c for c in again if sel.identity_for_candidate(c) == waiting][0]
        out = self.decide_ok(root, target["url"], "accept", rule="material-release",
                             quotes=("summary for synthetic coverage",),
                             run=root / ".state" / "runs" / "latest-fetch.json")
        self.assertEqual(out["state"], "ready")

    def test_untouched_work_below_the_judgment_limit_still_fails_finish(self):
        root = self.project_with_feeds(
            policy=fx.base_policy(budgets={"discovered": 3, "per_source": 3,
                                           "judgments": 5}),
            per_feed=3,
        )
        self.assertEqual(self.daily(root, "start").returncode, 0)
        run = root / ".state" / "runs" / "latest-fetch.json"
        cand = self.latest(root)[0]
        self.decide_ok(root, cand["url"], "reject", rule="routine-roundup",
                       quotes=("summary for synthetic coverage",), run=run)
        res = self.daily(root, "finish")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertEqual(self.gate(root)["status"], "failed")
        self.assertIn("unjudged", (self.gate(root).get("error") or "").lower())

    def test_default_budgets_are_discover_forty_judge_twenty(self):
        root = self.project_with_feeds(policy=fx.base_policy())
        policy = sel.load_policy(root)
        self.assertEqual(policy.budgets["discovered"], 40)
        self.assertEqual(policy.budgets["judgments"], 20)


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
