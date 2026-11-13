#!/usr/bin/env python3
"""Honest archived accounting and digest attention (repair contract C12/C13).

Synthetic scratch projects only; no delivery or publication path is exercised.
"""
from __future__ import annotations

import hashlib
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


class PendingQueueCountTests(fx.ProjectCase):
    """The digest shows the live retry queue, not the fetch-time snapshot.

    ``daily_gate.finish`` prunes terminal selections from ``.state/pending.json``
    *after* the run JSON is written, so ``run.pending_total`` can be stale by the
    time the digest renders. Every case below drives the real CLI on a synthetic
    scratch archive and verifies rendering leaves state bytes untouched.
    """

    def snapshot(self, root: Path) -> dict[str, str]:
        return {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }

    def render(self, root: Path, run: Path | None = None,
               gate: Path | None = None) -> subprocess.CompletedProcess:
        args = [fx.PY, "-B", str(root / "scripts" / "build_digest.py")]
        if run is not None:
            args += ["--run", str(run)]
        if gate is not None:
            args += ["--gate", str(gate)]
        before = self.snapshot(root)
        res = subprocess.run(args, capture_output=True, text=True,
                             cwd=str(root), timeout=120)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(
            before, self.snapshot(root),
            "rendering the digest must not change pending/run/seen/ledger bytes",
        )
        return res

    def set_pending(self, root: Path, urls: list[str]) -> Path:
        path = root / ".state" / "pending.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        entries = {
            sel.identity_for_url(url): {"url": url, "queued_at": fx.stamp(days=-1)}
            for url in urls
        }
        path.write_text(json.dumps({"schema": 2, "entries": entries}),
                        encoding="utf-8")
        return path

    def run_with_snapshot(self, root: Path, snapshot: int, *,
                          name: str = "latest-fetch.json",
                          generated_at: str | None = None) -> Path:
        run = self.write_run(root, [], name=name, generated_at=generated_at)
        data = json.loads(run.read_text(encoding="utf-8"))
        data["pending_total"] = snapshot
        data["sources"] = [{"id": "alpha", "status": "ok", "skipped": 0}]
        run.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        (root / "index.csv").write_text("id,title\n", encoding="utf-8")
        return run

    def test_current_run_reports_live_queue_not_fetch_snapshot(self):
        root = self.project()
        self.set_pending(root, ["https://example.com/live-1"])
        run = self.run_with_snapshot(root, 4)
        out = self.render(root, run).stdout
        self.assertIn("待补队列 1 篇", out)
        self.assertNotIn("待补队列 4 篇", out)

    def test_current_empty_queue_never_falls_back_to_fetch_snapshot(self):
        root = self.project()
        self.set_pending(root, [])
        run = self.run_with_snapshot(root, 4)
        out = self.render(root, run).stdout
        self.assertNotRegex(out, r"待补队列 [1-9][0-9]* 篇")
        self.assertNotIn("待补队列 4 篇", out)

    def test_missing_live_queue_keeps_legacy_snapshot(self):
        root = self.project()
        run = self.run_with_snapshot(root, 4)
        out = self.render(root, run).stdout
        self.assertFalse((root / ".state" / "pending.json").exists())
        self.assertIn("待补队列 4 篇", out)

    def test_historical_run_does_not_read_the_current_queue(self):
        root = self.project()
        self.set_pending(root, ["https://example.com/live-1"])
        self.run_with_snapshot(root, 4)
        old = self.run_with_snapshot(root, 7, name="historical.json",
                                     generated_at=fx.stamp(days=-2))
        out = self.render(root, old).stdout
        self.assertIn("待补队列 7 篇", out)
        self.assertNotIn("待补队列 1 篇", out)

    def test_same_run_at_another_path_is_recognised_by_generated_at(self):
        root = self.project()
        self.set_pending(root, ["https://example.com/live-1"])
        latest = self.run_with_snapshot(root, 4)
        copy = root / ".state/runs/copied-latest-fetch.json"
        copy.write_text(latest.read_text(encoding="utf-8"), encoding="utf-8")
        out = self.render(root, copy).stdout
        self.assertIn("待补队列 1 篇", out)
        self.assertNotIn("待补队列 4 篇", out)

    def test_overview_without_run_reads_the_current_queue(self):
        root = self.project()
        self.set_pending(root, ["https://example.com/live-1",
                                "https://example.com/live-2"])
        (root / "index.csv").write_text("id,title\n", encoding="utf-8")
        out = self.render(root).stdout
        self.assertIn("待补队列 2 篇", out)

    def test_invalid_live_queue_is_unknown_never_zero_or_snapshot(self):
        payloads = {
            "broken_json": "{broken",
            "null_entries": json.dumps({"entries": None}),
            "list_entries": json.dumps({"entries": []}),
            "string_entries": json.dumps({"entries": "nope"}),
            "missing_entries": json.dumps({"updated_at": fx.stamp()}),
            "list_root": json.dumps([]),
        }
        for label, payload in payloads.items():
            with self.subTest(payload=label):
                root = self.project()
                path = root / ".state" / "pending.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(payload, encoding="utf-8")
                run = self.run_with_snapshot(root, 4)
                out = self.render(root, run).stdout
                self.assertRegex(out, r"待补队列[^\n]*未知")
                self.assertNotRegex(out, r"待补队列 \d+ 篇")
                self.assertNotIn("待补队列 4 篇", out)

    def test_prune_pending_leaves_only_the_technical_failure_queued(self):
        root = self.project(fx.base_policy())
        roundup = "Routine promotional roundup without original reporting."
        urls = [f"https://example.com/prune-{i}" for i in range(4)]
        cands = [fx.candidate(urls[0], summary=roundup),
                 fx.candidate(urls[1]),
                 fx.candidate(urls[2]),
                 fx.candidate(urls[3])]
        run = self.write_run(root, cands)
        data = json.loads(run.read_text(encoding="utf-8"))
        data["pending_total"] = 4
        data["sources"] = [{"id": "alpha", "status": "ok", "skipped": 0}]
        run.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        (root / "index.csv").write_text("id,title\n", encoding="utf-8")
        self.register(root, cands, run=run)
        self.decide_ok(root, urls[0], "reject", rule="routine-roundup",
                       quotes=(roundup,), run=run)
        self.decide_ok(root, urls[1], "defer",
                       quotes=("A material platform release with original detail.",),
                       run=run, gap="Pricing is missing.",
                       follow_up="Check the pricing page.")
        self.decide_ok(root, urls[2], "accept", rule="material-release",
                       quotes=("A material platform release with original detail.",),
                       run=run)
        sel.mark_archived(root, sel.load_policy(root), url=urls[2],
                          entry_id="synthetic-prune-entry",
                          path="library/tech/synthetic-prune-entry/entry.md")
        pending = self.set_pending(root, urls)
        queued_before = json.loads(pending.read_text(encoding="utf-8"))["entries"]
        self.assertEqual(len(queued_before), 4)

        removed = sel.prune_pending(root, sel.load_policy(root))
        self.assertEqual(len(removed), 3, removed)
        queued_after = json.loads(pending.read_text(encoding="utf-8"))["entries"]
        self.assertEqual(list(queued_after), [sel.identity_for_url(urls[3])])

        gate = root / ".state" / "runs" / "synthetic-prune-gate.json"
        gate.write_text(json.dumps({"failed_candidates": [
            {"url": urls[3], "reason": "synthetic HTTP 503"},
        ]}), encoding="utf-8")
        out = self.render(root, run, gate).stdout
        self.assertIn("待补队列 1 篇", out)
        self.assertNotIn("待补队列 4 篇", out)
        self.assertIn("技术失败 1 个", out)


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
