#!/usr/bin/env python3
"""Honest archived accounting and digest attention (repair contract C12/C13).

Synthetic scratch projects only; no delivery or publication path is exercised.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_subscription_selection as fx  # noqa: E402

sel = fx.sel

# The two mutually exclusive zero-new explanations this file pins down.
ALL_EDITORIAL = "本轮候选均已判定排除或暂缓"
UNSETTLED = "不能据此判断今天真的没有更新"


class ArchivedEvidenceTests(fx.ProjectCase):
    def test_archived_marker_without_seen_proof_is_incomplete(self):
        root = self.project(fx.base_policy())
        cand = fx.candidate("https://example.com/no-seen-proof")
        self.register(root, [cand])
        self.decide_ok(root, cand["url"], "accept", rule="material-release",
                       quotes=("material platform release",))
        policy = sel.load_policy(root)
        sel.mark_archived(root, policy, url=cand["url"],
                          entry_id="synthetic-missing-entry")
        result = sel.classify_candidates(
            sel.load_ledger(root), policy, [cand],
            seen_keys=set(), failed_keys=set(),
        )
        self.assertFalse(result["accounted"], str(result))
        self.assertEqual(result["counts"].get("archive_unproven"), 1, result["counts"])
        self.assertEqual(result["rows"][0]["kind"], "archive_unproven")
        self.assertEqual(result["coverage"], "accounting-incomplete")

    def test_seen_proof_closes_archived_accounting(self):
        root = self.project(fx.base_policy())
        cand = fx.candidate("https://example.com/has-seen-proof")
        self.register(root, [cand])
        self.decide_ok(root, cand["url"], "accept", rule="material-release",
                       quotes=("material platform release",))
        policy = sel.load_policy(root)
        sel.mark_archived(root, policy, url=cand["url"], entry_id="synthetic-entry",
                          path="library/tech/x/entry.md")
        seen_key = sel.dedupe_key(cand["url"])
        result = sel.classify_candidates(
            sel.load_ledger(root), policy, [cand],
            seen_keys={seen_key}, failed_keys=set(),
        )
        self.assertTrue(result["accounted"], str(result))
        self.assertEqual(result["counts"].get("archived"), 1)

    def test_generic_url_proof_cannot_close_a_section_candidate(self):
        root = self.project(fx.base_policy())
        url = "https://example.com/docs#materials"
        cand = fx.candidate(url)
        self.register(root, [cand])
        self.decide_ok(root, url, "accept", rule="material-release",
                       quotes=("material platform release",))
        policy = sel.load_policy(root)
        sel.mark_archived(root, policy, url=url, entry_id="synthetic-section")
        result = sel.classify_candidates(
            sel.load_ledger(root), policy, [cand],
            seen_keys={sel.dedupe_key("https://example.com/docs")}, failed_keys=set(),
        )
        self.assertFalse(result["accounted"],
                         "another section's generic URL satisfied a section candidate")
        self.assertEqual(result["rows"][0]["kind"], "archive_unproven")


class DigestAttentionTests(fx.ProjectCase):
    def digest(self, root: Path, run: Path, gate: Path | None = None):
        args = [fx.PY, str(root / "scripts" / "build_digest.py"), "--run", str(run)]
        if gate is not None:
            args += ["--gate", str(gate)]
        return subprocess.run(args, capture_output=True, text=True,
                              cwd=str(root), timeout=120)

    def test_historical_open_work_is_never_reported_as_a_healthy_zero_new(self):
        root = self.project(fx.base_policy(budgets={"archives": 1}))
        cands = [fx.candidate("https://example.com/w1"),
                 fx.candidate("https://example.com/w2")]
        run = self.write_run(root, cands)
        self.register(root, cands, run=run)
        for cand in cands:
            self.decide_ok(root, cand["url"], "accept", rule="material-release",
                           quotes=("material platform release",), run=run)
        stale = self.ledger(root)
        stale["records"]["https://example.com/w2"]["decided_at"] = fx.iso_ago(days=20)
        (root / ".state/selection/ledger.json").write_text(json.dumps(stale),
                                                          encoding="utf-8")
        (root / "index.csv").write_text("id,title\n", encoding="utf-8")
        empty = self.write_run(root, [], generated_at=fx.stamp(minutes=1),
                               name="empty.json")
        data = json.loads(empty.read_text(encoding="utf-8"))
        data["sources"] = [{"id": "alpha", "status": "ok"}]
        empty.write_text(json.dumps(data), encoding="utf-8")

        res = self.digest(root, empty, root / ".state/runs/none-gate.json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("今日无新增", res.stdout)
        self.assertIn("预算等待", res.stdout)
        self.assertIn("最久", res.stdout)
        self.assertIn("select status", res.stdout)

    def test_digest_uses_the_policy_review_limit(self):
        root = self.project(fx.base_policy(retention={"max_review_attempts": 3}))
        cand = fx.candidate("https://example.com/deferred")
        run = self.write_run(root, [cand])
        self.register(root, [cand], run=run)
        self.decide_ok(root, cand["url"], "defer",
                       quotes=("material platform release",), run=run,
                       gap="Pricing is missing.",
                       follow_up="Check the pricing page.")
        data = self.ledger(root)
        data["records"][cand["url"]]["review_attempts"] = 1
        (root / ".state/selection/ledger.json").write_text(json.dumps(data),
                                                          encoding="utf-8")
        (root / "index.csv").write_text("id,title\n", encoding="utf-8")
        res = self.digest(root, run)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("复核 1/3", res.stdout)
        self.assertNotIn("复核 1/2", res.stdout)

    def pair_digest(self, first_kind: str, second_kind: str) -> subprocess.CompletedProcess:
        """Real renderer over two current-run candidates left in given states.

        Each kind is one of ``rejected``/``deferred``/``pending_archive``/
        ``technical_failure``/``unjudged``; the digest is rendered by the
        project's own copied ``build_digest.py``, never simulated.
        """
        root = self.project(fx.base_policy())
        roundup = "Routine promotional roundup without original reporting."
        first = fx.candidate("https://example.com/synthetic-first")
        second = fx.candidate("https://example.com/synthetic-second")
        kinds = {first["url"]: first_kind, second["url"]: second_kind}
        for cand in (first, second):
            if kinds[cand["url"]] == "rejected":
                cand["summary"] = roundup
        run = self.write_run(root, [first, second])
        data = json.loads(run.read_text(encoding="utf-8"))
        data["sources"] = [{"id": "alpha", "status": "ok", "skipped": 0}]
        run.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.register(root, [first, second], run=run)
        for cand in (first, second):
            kind = kinds[cand["url"]]
            if kind == "rejected":
                self.decide_ok(root, cand["url"], "reject", rule="routine-roundup",
                               quotes=(roundup,), run=run)
            elif kind == "pending_archive":
                self.decide_ok(root, cand["url"], "accept", rule="material-release",
                               quotes=("material platform release",), run=run)
            elif kind == "deferred":
                self.decide_ok(root, cand["url"], "defer",
                               quotes=("material platform release",), run=run,
                               gap="Pricing is missing.",
                               follow_up="Check the pricing page.")
        gate = root / ".state" / "runs" / "synthetic-gate.json"
        failed = [{"url": url, "reason": "synthetic HTTP 503"}
                  for url, kind in kinds.items() if kind == "technical_failure"]
        gate.write_text(json.dumps({"failed_candidates": failed}), encoding="utf-8")
        (root / "index.csv").write_text("id,title\n", encoding="utf-8")
        res = self.digest(root, run, gate)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res

    def test_reject_plus_technical_failure_is_not_all_editorial(self):
        res = self.pair_digest("rejected", "technical_failure")
        self.assertNotIn(ALL_EDITORIAL, res.stdout)
        self.assertIn(UNSETTLED, res.stdout)
        self.assertIn("本轮候选 2 个：归档 0 · 排除 1", res.stdout)
        self.assertIn("技术失败 1 个（不是选题拒绝，仍会重试）", res.stdout)
        self.assertIn("https://example.com/synthetic-second：synthetic HTTP 503",
                      res.stdout)

    def test_reject_plus_unjudged_is_not_all_editorial(self):
        res = self.pair_digest("rejected", "unjudged")
        self.assertNotIn(ALL_EDITORIAL, res.stdout)
        self.assertIn(UNSETTLED, res.stdout)
        self.assertIn("本轮候选 2 个：归档 0 · 排除 1", res.stdout)
        self.assertIn("未判断 1 · 待归档 0", res.stdout)

    def test_reject_plus_pending_archive_is_not_all_editorial(self):
        res = self.pair_digest("rejected", "pending_archive")
        self.assertNotIn(ALL_EDITORIAL, res.stdout)
        self.assertIn(UNSETTLED, res.stdout)
        self.assertIn("本轮候选 2 个：归档 0 · 排除 1", res.stdout)
        self.assertIn("未判断 0 · 待归档 1", res.stdout)
        self.assertIn("历史未结选题 1 个", res.stdout)

    def test_all_rejected_control_keeps_all_editorial_claim(self):
        res = self.pair_digest("rejected", "rejected")
        self.assertIn(ALL_EDITORIAL, res.stdout)
        self.assertNotIn(UNSETTLED, res.stdout)
        self.assertIn("本轮候选 2 个：归档 0 · 排除 2", res.stdout)

    def test_all_deferred_control_keeps_all_editorial_claim(self):
        res = self.pair_digest("deferred", "deferred")
        self.assertIn(ALL_EDITORIAL, res.stdout)
        self.assertNotIn(UNSETTLED, res.stdout)
        self.assertIn("本轮候选 2 个：归档 0 · 排除 0 · 暂缓 2", res.stdout)
        self.assertIn("Pricing is missing.", res.stdout)

    def test_current_all_rejected_keeps_historical_open_work_visible(self):
        root = self.project(fx.base_policy())
        earlier = fx.candidate("https://example.com/historical-open")
        early_run = self.write_run(root, [earlier], name="historical.json")
        self.register(root, [earlier], run=early_run)
        self.decide_ok(root, earlier["url"], "accept", rule="material-release",
                       quotes=("material platform release",), run=early_run)
        roundup = "Routine promotional roundup without original reporting."
        rejected = [fx.candidate("https://example.com/new-r1", summary=roundup),
                    fx.candidate("https://example.com/new-r2", summary=roundup)]
        run = self.write_run(root, rejected, name="current.json")
        data = json.loads(run.read_text(encoding="utf-8"))
        data["sources"] = [{"id": "alpha", "status": "ok", "skipped": 0}]
        run.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.register(root, rejected, run=run)
        for cand in rejected:
            self.decide_ok(root, cand["url"], "reject", rule="routine-roundup",
                           quotes=(roundup,), run=run)
        (root / "index.csv").write_text("id,title\n", encoding="utf-8")
        res = self.digest(root, run)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(ALL_EDITORIAL, res.stdout)
        self.assertNotIn(UNSETTLED, res.stdout)
        self.assertIn("历史未结选题 1 个", res.stdout)
        self.assertIn("select status", res.stdout)

    def test_empty_current_run_never_claims_all_rejected(self):
        root = self.project(fx.base_policy())
        run = self.write_run(root, [], name="empty.json")
        data = json.loads(run.read_text(encoding="utf-8"))
        data["sources"] = [{"id": "alpha", "status": "ok", "skipped": 0}]
        run.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        (root / "index.csv").write_text("id,title\n", encoding="utf-8")
        res = self.digest(root, run)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn(ALL_EDITORIAL, res.stdout)
        self.assertNotIn(UNSETTLED, res.stdout)
        self.assertIn("本轮候选 0 个", res.stdout)
        self.assertIn("今日无新增", res.stdout)


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
