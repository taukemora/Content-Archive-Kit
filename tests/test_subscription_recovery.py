#!/usr/bin/env python3
"""Recoverable, bounded execution lifecycle (repair contract B).

Healthy-behaviour regressions derived from the parent acceptance probes and the
independent reviewer's mixed-polarity probes (R1, R2, R3). Synthetic scratch
projects and loopback feeds only.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_subscription_selection as fx  # noqa: E402
import fetch_sources as fs  # noqa: E402

sel = fx.sel
TZ = timezone(timedelta(hours=8))


class ReassessmentReopenTests(fx.ProjectCase):
    def test_terminal_identity_can_be_reopened_after_it_leaves_latest_fetch(self):
        root = self.project(fx.base_policy())
        cand = fx.candidate("https://example.com/reassess-later")
        self.register(root, [cand])
        self.decide_ok(root, cand["url"], "reject", rule="routine-roundup",
                       quotes=("material platform release",))
        self.write_run(root, [], generated_at=fx.stamp(minutes=1))

        plan = self.select_json(root, "reassess", "--key", cand["url"])
        self.assertEqual(plan["status"], "plan")
        self.assertTrue(plan["attempts_left"] >= 1)

        result = self.run_select(root, "reassess", "--key", cand["url"], "--apply",
                                 "--reason", "Owner requests a new evidence review")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = self.ledger(root)["records"][cand["url"]]
        self.assertEqual(record["outcome"], "")
        self.assertEqual(record["state"], "awaiting_judgment")
        self.assertEqual(record["review_attempts"], 1)
        self.assertTrue(any(h.get("action") == "reassess"
                            for h in record.get("history") or []))
        # The reopened identity is requeued without resurrecting anything else.
        entries = json.loads((root / ".state/pending.json").read_text())["entries"]
        self.assertIn(cand["url"], entries)
        self.assertEqual(list(entries), [cand["url"]])

    def test_reopen_still_honours_the_attempt_limit(self):
        root = self.project(fx.base_policy())
        cand = fx.candidate("https://example.com/reassess-bound")
        self.register(root, [cand])
        self.decide_ok(root, cand["url"], "reject", rule="routine-roundup",
                       quotes=("material platform release",))
        run = self.write_run(root, [cand], generated_at=fx.stamp(minutes=1))
        for _ in range(2):
            self.select_json(root, "reassess", "--key", cand["url"], "--apply",
                             "--run", str(run))
            self.decide_ok(root, cand["url"], "reject", rule="routine-roundup",
                           quotes=("material platform release",), run=run)
        res = self.run_select(root, "reassess", "--key", cand["url"], "--apply",
                              "--run", str(run))
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertIn("attempt", res.stderr.lower())


class RecoveryPayloadTests(fx.ProjectCase):
    def test_accepted_budget_wait_recovers_with_its_payload_after_queue_loss(self):
        root = self.project(fx.base_policy(budgets={"archives": 1}))
        content = root / ".state" / "payload.md"
        content.write_text("Material platform release body.", encoding="utf-8")
        first = fx.candidate("https://example.com/recover-first")
        second = fx.candidate(
            "https://example.com/recover-wait",
            summary="Recovered accepted work summary",
            extractor="generic",
            content_file=str(content),
            cache_name="payload.md",
        )
        self.register(root, [first, second])
        for cand in (first, second):
            self.decide_ok(root, cand["url"], "accept", rule="material-release",
                           quotes=("platform release",))
        ledger = self.ledger(root)
        self.assertEqual(ledger["records"][second["url"]]["state"], sel.STATE_BUDGET_WAIT)
        queued_at = ledger["records"][second["url"]]["queued_at"]

        merged: dict = {}
        evicted: list = []
        result = sel.apply_commit(
            ledger, sel.load_policy(root), run_gen=fx.stamp(minutes=1),
            candidates=[], observed=[], merged=merged, evicted=evicted,
        )
        self.assertIn(second["url"], merged, str(result))
        recovered = merged[second["url"]]
        self.assertEqual(recovered["reason"], "ledger_recovery")
        self.assertEqual(recovered["summary"], "Recovered accepted work summary")
        self.assertEqual(recovered["extractor"], "generic")
        self.assertEqual(recovered["content_file"], str(content))
        self.assertEqual(recovered["cache_name"], "payload.md")
        self.assertEqual(recovered["source_id"], "alpha")
        self.assertEqual(recovered["queued_at"], queued_at,
                         "crash recovery must not reset the first enqueue age")


class SeenAndExpiryTests(fx.FeedCase):
    def test_manual_save_while_undecided_closes_queue_without_churn(self):
        root = self.project_with_feeds(policy=fx.base_policy())
        out = self.run_fetch(root, "--max", "2")
        url = out["candidates"][0]["url"]
        self.mark_seen(root, url)

        seen_exits = 0
        for index in range(3):
            run = self.run_fetch(root, "--max", "2")
            exits = [e for e in run["pending_exits"] if e.get("url") == url]
            if index:
                self.assertEqual(exits, [], "repeated seen-exit/recovery churn")
            seen_exits += sum(1 for e in exits if e.get("reason") == "seen")
        self.assertLessEqual(seen_exits, 1)
        self.assertNotIn(url, self.pending(root)["entries"],
                         "closed queue work must not be recovered again")
        record = self.ledger(root)["records"][sel.identity_for_url(url)]
        self.assertEqual(record["state"], sel.STATE_ARCHIVED)
        self.assertEqual(record["outcome"], "")

    def test_expired_undecided_identity_leaves_queue_and_is_not_resurrected(self):
        root = self.project_with_feeds(policy=fx.base_policy())
        self.run_fetch(root, "--max", "2")
        key = next(iter(self.ledger(root)["records"]))
        pending_path = root / ".state" / "pending.json"
        data = json.loads(pending_path.read_text())
        old = (datetime.now(TZ) - timedelta(days=45)).isoformat(timespec="seconds")
        data["entries"][key]["queued_at"] = old
        pending_path.write_text(json.dumps(data))

        out = self.run_fetch(root, "--max", "2")
        exits = [e for e in out["pending_exits"] if e.get("key") == key]
        self.assertTrue(exits and exits[0]["reason"] == "expired", out["pending_exits"])
        self.assertNotIn(key, self.pending(root)["entries"],
                         "an expired identity must not be resurrected")
        record = self.ledger(root)["records"][key]
        self.assertEqual(record["state"], "execution_expired")
        self.assertEqual(record["outcome"], "",
                         "execution expiry is not an editorial rejection")

        again = self.run_fetch(root, "--max", "2")
        self.assertNotIn(key, self.pending(root)["entries"])
        self.assertEqual([e for e in again["pending_exits"] if e.get("key") == key], [],
                         "expiry must not repeat every run")

    def test_overflow_after_recovery_is_bounded_and_explicit(self):
        root = self.project_with_feeds(
            policy=fx.base_policy(budgets={"discovered": 1, "per_source": 1}),
            per_feed=1,
        )
        self.run_fetch(root, "--max", "1")
        key = next(iter(self.ledger(root)["records"]))
        pending_path = root / ".state" / "pending.json"
        data = json.loads(pending_path.read_text())
        data["entries"].pop(key, None)  # simulate queue loss for one open record
        limit = fs.PENDING_LIMIT
        while len(data["entries"]) < limit:
            filler = f"https://example.com/filler-{len(data['entries'])}"
            data["entries"][sel.identity_for_url(filler)] = {
                "url": filler, "title": "filler", "source_id": "alpha",
                "reason": "awaiting_archive", "queued_at": fx.stamp(hours=-2),
            }
        pending_path.write_text(json.dumps(data))
        # The source goes empty, so nothing is upserted: only recovery can add.
        self.set_feed("alpha", [])

        out = self.run_fetch(root, "--max", "1")
        size = len(self.pending(root)["entries"])
        self.assertLessEqual(size, limit, f"pending.json holds {size} > {limit}")
        self.assertEqual(size, limit)
        record = self.ledger(root)["records"][key]
        self.assertTrue(record.get("queue_overflow"),
                        "stranded accepted work must stay explicit in the ledger")
        self.assertEqual(out["selection"].get("overflow", 0), 1)

        stable = self.run_fetch(root, "--max", "1")
        self.assertEqual(len(self.pending(root)["entries"]), limit)
        self.assertTrue(self.ledger(root)["records"][key].get("queue_overflow"),
                        "the explicit overflow marker must persist across runs")
        self.assertLessEqual(stable["selection"].get("overflow", 0), 1)


class CleanupInterleavingTests(fx.ProjectCase):
    def test_cleanup_rechecks_a_reopened_record_inside_the_lock(self):
        root = self.project(fx.base_policy())
        cand = fx.candidate("https://example.com/cleanup-race")
        self.register(root, [cand])
        self.decide_ok(root, cand["url"], "reject", rule="routine-roundup",
                       quotes=("material platform release",))
        data = self.ledger(root)
        data["records"][cand["url"]]["decided_at"] = fx.stamp(days=-200)
        sel.save_ledger(root, data)

        original = sel._with_ledger

        def interleaved(target, mutate):
            # A second legitimate writer reopened the record after planning but
            # before cleanup acquires the existing state lock.
            with patch.object(sel, "_with_ledger", original):
                sel.reassess(root, sel.load_policy(root), key=cand["url"],
                             apply=True, reason="Explicit concurrent reassessment")
            return original(target, mutate)

        with patch.object(sel, "_with_ledger", side_effect=interleaved):
            sel.cleanup(root, sel.load_policy(root), apply=True)
        self.assertIn(cand["url"], self.ledger(root)["records"],
                      "cleanup deleted newly reopened work from a stale plan")


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
