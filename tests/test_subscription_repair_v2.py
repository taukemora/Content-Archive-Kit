#!/usr/bin/env python3
"""Bounded repair round 2: current-run membership, authoritative state.

These are the healthy-behaviour regressions derived from the parent's second
acceptance probes. Every test builds a throwaway synthetic project; no live
network, private archive, credential or publication path is touched.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_subscription_selection as fx  # noqa: E402

sel = fx.sel


class CurrentRunMembershipTests(fx.ProjectCase):
    """A matching ``generated_at`` is a timestamp, not proof of membership."""

    def accepted(self, root: Path) -> dict:
        cand = fx.candidate("https://example.com/member")
        self.register(root, [cand])
        self.decide_ok(root, cand["url"], "accept", rule="material-release",
                       quotes=("material platform release",))
        return cand

    def drop_member(self, root: Path) -> None:
        path = root / ".state/runs/latest-fetch.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["candidates"] = []
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_ready_reports_membership_not_just_a_matching_timestamp(self):
        root = self.project(fx.base_policy())
        cand = self.accepted(root)
        self.drop_member(root)

        out = self.select_json(root, "ready")
        self.assertEqual(out["admitted"], [], out)
        self.assertTrue(
            any(w["key"] == cand["url"] and w["reason"] == "not_in_current_run"
                for w in out["not_readmitted"]),
            out,
        )

    def test_ready_refuses_a_run_without_a_generated_at(self):
        root = self.project(fx.base_policy())
        self.accepted(root)
        with self.assertRaises(sel.SelectionStateError):
            sel.ready(root, sel.load_policy(root), run_data={})

    def test_auto_admission_requires_membership_on_the_fresh_branch(self):
        root = self.project(fx.base_policy())
        cand = self.accepted(root)
        self.drop_member(root)
        before = (root / ".state/selection/ledger.json").read_bytes()

        with self.assertRaises(sel.SelectionStateError):
            sel.admit(root, sel.load_policy(root), url=cand["url"])
        self.assertEqual(before, (root / ".state/selection/ledger.json").read_bytes())
        self.assertEqual(self.ledger(root)["records"][cand["url"]]["state"], "ready")

    def test_auto_admission_refuses_a_run_without_a_generated_at(self):
        root = self.project(fx.base_policy())
        cand = self.accepted(root)
        with self.assertRaises(sel.SelectionStateError):
            sel.admit(root, sel.load_policy(root), url=cand["url"], run_data={})

    def test_unchanged_retry_in_the_current_run_still_passes(self):
        root = self.project(fx.base_policy())
        cand = self.accepted(root)
        out = sel.admit(root, sel.load_policy(root), url=cand["url"])
        self.assertEqual(out["status"], "ok", out)


class AutoCliMembershipTests(fx.ProjectCase):
    """The real automatic ``add`` CLI refuses before any library write."""

    def project_with_accepted_candidate(self):
        root = self.project(fx.base_policy())
        url = f"https://{fx.PUBLIC_IP}/release/membership"
        content = root / ".state" / "body.md"
        content.write_text(
            "Synthetic body used only for the automatic admission boundary.\n",
            encoding="utf-8",
        )
        cand = fx.candidate(url, title="AI platform release",
                            summary="AI platform release",
                            content_file=str(content))
        run = self.write_run(root, [cand])
        self.register(root, [cand], run=run)
        self.decide_ok(root, url, "accept", rule="material-release",
                       quotes=("AI platform release",), run=run)
        return root, url, content, run

    def test_automatic_add_refuses_a_candidate_missing_from_the_current_run(self):
        root, url, content, run = self.project_with_accepted_candidate()
        data = json.loads(run.read_text(encoding="utf-8"))
        data["candidates"] = []
        run.write_text(json.dumps(data), encoding="utf-8")
        before = (root / ".state/selection/ledger.json").read_bytes()

        res = subprocess.run(
            [fx.PY, str(root / "scripts" / "archive_url.py"), url,
             "--content-file", str(content),
             "--original-title", "AI platform release",
             "--published", "2026-11-10", "--language", "en",
             "--collection", "auto"],
            capture_output=True, text=True, cwd=str(root), timeout=120,
        )
        self.assertEqual(res.returncode, 1, res.stderr + res.stdout)
        self.assertIn("blocked", res.stdout.lower())
        self.assertEqual(list((root / "library").rglob("entry.md")), [])
        seen = json.loads((root / ".state" / "seen.json").read_text(encoding="utf-8"))
        self.assertEqual(seen["entries"], {})
        self.assertEqual(before, (root / ".state/selection/ledger.json").read_bytes())


class AuthoritativeStateValidationTests(fx.ProjectCase):
    """Corrupt counters and contradictory identities fail visibly, unwritten."""

    def ledger_file(self, root: Path) -> Path:
        return root / ".state" / "selection" / "ledger.json"

    def accepted(self, root: Path, url: str = "https://example.com/validated") -> dict:
        cand = fx.candidate(url)
        self.register(root, [cand])
        self.decide_ok(root, cand["url"], "accept", rule="material-release",
                       quotes=("material platform release",))
        return cand

    def edit(self, root: Path, mutate) -> None:
        path = self.ledger_file(root)
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_boolean_judgment_counter_cannot_refund_model_work(self):
        root = self.project(fx.base_policy(budgets={"judgments": 1}))
        cand = fx.candidate("https://example.com/counter")
        self.register(root, [cand])
        self.decide_ok(root, cand["url"], "reject", rule="routine-roundup",
                       quotes=("material platform release",))
        self.select_json(root, "reassess", "--key", cand["url"], "--apply")
        self.edit(root, lambda data: [
            run.update({"judgments": True}) for run in data["runs"].values()
        ])
        before = self.ledger_file(root).read_bytes()

        out = self.decide(root, cand["url"], "accept", rule="material-release",
                          quotes=("material platform release",))
        self.assertNotEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(before, self.ledger_file(root).read_bytes(),
                         "a corrupt counter must not be coerced, refunded or repaired")

    def test_each_malformed_per_run_counter_fails_closed(self):
        for name in ("judgments", "archives"):
            for value in (True, -1, "3", 1.5, [1]):
                with self.subTest(counter=name, value=value):
                    root = self.project(fx.base_policy())
                    self.accepted(root)
                    self.edit(root, lambda data, n=name, v=value: [
                        run.__setitem__(n, v) for run in data["runs"].values()
                    ])
                    with self.assertRaises(sel.SelectionStateError):
                        sel.load_ledger(root)

    def test_run_entry_must_be_a_mapping(self):
        root = self.project(fx.base_policy())
        self.accepted(root)
        self.edit(root, lambda data: [
            data["runs"].__setitem__(gen, []) for gen in list(data["runs"])
        ])
        with self.assertRaises(sel.SelectionStateError):
            sel.load_ledger(root)

    def test_ledger_schema_is_an_exact_integer(self):
        root = self.project(fx.base_policy())
        self.accepted(root)
        self.edit(root, lambda data: data.__setitem__("schema", True))
        with self.assertRaises(sel.SelectionStateError):
            sel.load_ledger(root)

    def test_embedded_key_and_url_identity_must_agree_with_the_record_key(self):
        root = self.project(fx.base_policy())
        cand = self.accepted(root)
        self.edit(root, lambda data: data["records"][cand["url"]].__setitem__(
            "key", "https://example.com/foreign"))
        with self.assertRaises(sel.SelectionStateError):
            sel.load_ledger(root)

    def test_record_url_must_match_its_canonical_identity(self):
        root = self.project(fx.base_policy())
        cand = self.accepted(root)
        self.edit(root, lambda data: data["records"][cand["url"]].__setitem__(
            "url", "https://example.com/different"))
        with self.assertRaises(sel.SelectionStateError):
            sel.load_ledger(root)

    def test_fragment_identity_is_preserved_not_collapsed(self):
        root = self.project(fx.base_policy())
        url = "https://example.com/docs#materials"
        self.accepted(root, url)
        record = self.ledger(root)["records"][url]
        self.assertEqual(record["key"], url)
        self.assertEqual(record["url"], url)
        self.assertEqual(sel.load_ledger(root)["records"][url]["key"], url)

    def test_corrupt_counter_fails_status_ready_and_the_auto_path_visibly(self):
        root = self.project(fx.base_policy())
        cand = self.accepted(root)
        self.edit(root, lambda data: [
            run.update({"archives": True}) for run in data["runs"].values()
        ])
        before = self.ledger_file(root).read_bytes()

        for args in (("status",), ("ready",), ("reassess", "--key", cand["url"])):
            res = self.run_select(root, *args)
            self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
        with self.assertRaises(sel.SelectionStateError):
            sel.admit(root, sel.load_policy(root), url=cand["url"])
        self.assertEqual(before, self.ledger_file(root).read_bytes())

    def test_manual_save_warns_and_leaves_a_corrupt_counter_untouched(self):
        root = self.project(fx.base_policy())
        self.accepted(root)
        self.edit(root, lambda data: [
            run.update({"judgments": True}) for run in data["runs"].values()
        ])
        before = self.ledger_file(root).read_bytes()
        content = root / ".state" / "manual-body.md"
        content.write_text("Synthetic manual body for the corrupt-counter case.\n",
                           encoding="utf-8")
        res = subprocess.run(
            [fx.PY, str(root / "scripts" / "archive_url.py"),
             "https://example.com/manual-corrupt-counter",
             "--collection", "manual", "--content-file", str(content),
             "--original-title", "Manual synthetic item",
             "--published", "2026-11-10", "--language", "en"],
            capture_output=True, text=True, cwd=str(root), timeout=120,
        )
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn("automation", (res.stderr + res.stdout).lower())
        self.assertEqual(before, self.ledger_file(root).read_bytes())


class RunHistoryPresenceTests(fx.ProjectCase):
    """Absent legacy history is supported; a present non-mapping is corruption."""

    def ledger_file(self, root: Path) -> Path:
        return root / ".state" / "selection" / "ledger.json"

    def accepted(self, root: Path) -> dict:
        cand = fx.candidate("https://example.com/run-history")
        self.register(root, [cand])
        self.decide_ok(root, cand["url"], "accept", rule="material-release",
                       quotes=("material platform release",))
        return cand

    def set_runs(self, root: Path, value) -> None:
        path = self.ledger_file(root)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["runs"] = value
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_present_null_run_history_is_rejected_without_rewriting(self):
        root = self.project(fx.base_policy())
        self.accepted(root)
        self.set_runs(root, None)
        before = self.ledger_file(root).read_bytes()

        with self.assertRaises(sel.SelectionStateError):
            sel.load_ledger(root)
        self.assertEqual(before, self.ledger_file(root).read_bytes(),
                         "a present null run history must not be rewritten")

    def test_present_non_mapping_run_history_is_rejected(self):
        for value in ([], "runs", 7):
            with self.subTest(value=value):
                root = self.project(fx.base_policy())
                self.accepted(root)
                self.set_runs(root, value)
                with self.assertRaises(sel.SelectionStateError):
                    sel.load_ledger(root)

    def test_absent_legacy_run_history_is_still_supported(self):
        root = self.project(fx.base_policy())
        self.accepted(root)
        path = self.ledger_file(root)
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("runs", None)
        path.write_text(json.dumps(data), encoding="utf-8")
        before = path.read_bytes()

        self.assertEqual(sel.load_ledger(root)["runs"], {})
        self.assertEqual(before, path.read_bytes())

    def test_valid_empty_run_history_is_still_supported(self):
        root = self.project(fx.base_policy())
        self.accepted(root)
        self.set_runs(root, {})

        self.assertEqual(sel.load_ledger(root)["runs"], {})

    def test_present_null_history_fails_cli_status_and_decide_visibly(self):
        root = self.project(fx.base_policy())
        cand = fx.candidate("https://example.com/run-history")
        self.register(root, [cand])
        self.set_runs(root, None)
        before = self.ledger_file(root).read_bytes()

        for args in (
            ("status",),
            ("decide", "--key", cand["url"], "--outcome", "reject",
             "--reason", "synthetic editorial reason",
             "--rule", "routine-roundup",
             "--evidence", "material platform release"),
        ):
            with self.subTest(action=args[0]):
                res = self.run_select(root, *args)
                self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
                self.assertIn("runs is not a mapping", res.stderr)
                self.assertNotIn("Traceback", res.stderr)
                self.assertNotIn("AttributeError", res.stderr)
                self.assertEqual(before, self.ledger_file(root).read_bytes())


class BoundedReassessmentTests(fx.ProjectCase):
    """A full pending queue refuses a reopen atomically, without an attempt."""

    def rejected(self, root: Path) -> dict:
        cand = fx.candidate("https://example.com/reopen-full")
        self.register(root, [cand])
        self.decide_ok(root, cand["url"], "reject", rule="routine-roundup",
                       quotes=("material platform release",))
        self.write_run(root, [], generated_at=fx.stamp(minutes=1))
        return cand

    def fill_queue(self, root: Path, *, leave_room: int = 0) -> dict:
        limit = sel.pending_queue_limit()
        entries = {
            f"https://example.com/fill-{n}": {
                "url": f"https://example.com/fill-{n}",
                "source_id": "alpha",
                "title": "Synthetic retained work",
                "queued_at": fx.stamp(),
                "reason": "awaiting_archive",
            }
            for n in range(limit - leave_room)
        }
        path = root / ".state" / "pending.json"
        path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
        return entries

    def test_full_queue_refuses_the_reopen_before_any_durable_change(self):
        root = self.project(fx.base_policy())
        cand = self.rejected(root)
        entries = self.fill_queue(root)
        before_ledger = (root / ".state/selection/ledger.json").read_bytes()
        before_pending = (root / ".state/pending.json").read_bytes()

        res = self.run_select(root, "reassess", "--key", cand["url"], "--apply")
        self.assertEqual(res.returncode, 4, res.stdout + res.stderr)
        self.assertIn("capacity", (res.stdout + res.stderr).lower())
        self.assertEqual(before_ledger, (root / ".state/selection/ledger.json").read_bytes())
        self.assertEqual(before_pending, (root / ".state/pending.json").read_bytes())
        record = self.ledger(root)["records"][cand["url"]]
        self.assertEqual(record["outcome"], "reject")
        self.assertEqual(record["review_attempts"], 0,
                         "a capacity refusal must not consume a reassessment attempt")
        self.assertEqual(set(json.loads((root / ".state/pending.json").read_text())["entries"]),
                         set(entries))

    def test_plan_without_apply_is_still_read_only_on_a_full_queue(self):
        root = self.project(fx.base_policy())
        cand = self.rejected(root)
        self.fill_queue(root)
        before = (root / ".state/selection/ledger.json").read_bytes()
        plan = self.select_json(root, "reassess", "--key", cand["url"])
        self.assertEqual(plan["status"], "plan")
        self.assertTrue(plan["reopen_pending_discovery"])
        self.assertEqual(before, (root / ".state/selection/ledger.json").read_bytes())

    def test_an_already_queued_key_may_be_updated_without_enlarging_the_queue(self):
        root = self.project(fx.base_policy())
        cand = self.rejected(root)
        entries = self.fill_queue(root)
        path = root / ".state/pending.json"
        entries[cand["url"]] = {"url": cand["url"], "source_id": "alpha",
                                "queued_at": fx.stamp(), "reason": "retry"}
        entries.pop(next(iter(entries)))
        path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
        size_before = len(entries)

        res = self.run_select(root, "reassess", "--key", cand["url"], "--apply")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        after = json.loads(path.read_text())["entries"]
        self.assertEqual(len(after), size_before)
        self.assertEqual(after[cand["url"]]["reason"], "reassess_reopen")
        record = self.ledger(root)["records"][cand["url"]]
        self.assertEqual(record["state"], "awaiting_judgment")
        self.assertEqual(record["review_attempts"], 1)

    def test_the_same_operation_succeeds_once_room_exists(self):
        root = self.project(fx.base_policy())
        cand = self.rejected(root)
        entries = self.fill_queue(root)
        refused = self.run_select(root, "reassess", "--key", cand["url"], "--apply")
        self.assertEqual(refused.returncode, 4, refused.stdout + refused.stderr)

        path = root / ".state/pending.json"
        entries.pop(next(iter(entries)))
        path.write_text(json.dumps({"entries": entries}), encoding="utf-8")

        res = self.run_select(root, "reassess", "--key", cand["url"], "--apply")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        record = self.ledger(root)["records"][cand["url"]]
        self.assertEqual(record["state"], "awaiting_judgment")
        self.assertEqual(record["review_attempts"], 1,
                         "only the successful reopen consumes an attempt")
        self.assertIn(cand["url"], json.loads(path.read_text())["entries"])

        # The next discovery/decision path stays usable.
        run = self.write_run(root, [cand], generated_at=fx.stamp(minutes=2))
        self.register(root, [cand], run=run)
        out = self.decide_ok(root, cand["url"], "reject", rule="routine-roundup",
                             quotes=("material platform release",), run=run)
        self.assertEqual(out["outcome"], "reject")


class OutstandingAgeTests(fx.ProjectCase):
    """Age is measured from the enqueue/open origin, not from observation."""

    def aged_awaiting(self, root: Path, url: str = "https://example.com/old-open") -> dict:
        cand = fx.candidate(url)
        self.register(root, [cand])
        data = self.ledger(root)
        data["records"][cand["url"]]["queued_at"] = fx.stamp(days=-40)
        data["records"][cand["url"]]["first_seen"] = fx.stamp(days=-40)
        sel.save_ledger(root, data)
        return cand

    def test_observation_refresh_does_not_reset_the_open_age(self):
        root = self.project(fx.base_policy())
        cand = self.aged_awaiting(root)
        data = self.ledger(root)
        data["records"][cand["url"]]["updated_at"] = fx.stamp(days=-1)
        sel.save_ledger(root, data)
        self.register(root, [cand])  # a later discovery observes it again
        self.assertNotEqual(
            self.ledger(root)["records"][cand["url"]]["updated_at"], fx.stamp(days=-1),
            "the observation refresh must actually be recorded",
        )
        overview = sel.ledger_overview(self.ledger(root), sel.load_policy(root))
        self.assertGreaterEqual(overview["oldest_open_days"], 40, overview)
        self.assertGreaterEqual(overview["stalled"], 1, overview)

    def test_status_reports_the_outstanding_age_after_refresh(self):
        root = self.project(fx.base_policy())
        cand = self.aged_awaiting(root)
        self.register(root, [cand])
        payload = self.select_json(root, "status")
        self.assertGreaterEqual(payload["open_detail"]["oldest_open_days"], 40, payload)
        self.assertGreaterEqual(payload["open_detail"]["stalled"], 1, payload)

    def test_accepted_waiting_work_also_uses_its_enqueue_origin(self):
        root = self.project(fx.base_policy())
        cand = fx.candidate("https://example.com/old-wait")
        self.register(root, [cand])
        data = self.ledger(root)
        data["records"][cand["url"]]["state"] = sel.STATE_BUDGET_WAIT
        data["records"][cand["url"]]["outcome"] = "accept"
        data["records"][cand["url"]]["admitted_run"] = ""
        data["records"][cand["url"]]["queued_at"] = fx.stamp(days=-31)
        data["records"][cand["url"]]["first_seen"] = fx.stamp(days=-31)
        sel.save_ledger(root, data)
        self.register(root, [cand])
        overview = sel.ledger_overview(self.ledger(root), sel.load_policy(root))
        self.assertGreaterEqual(overview["oldest_open_days"], 31, overview)

    def test_reassessment_keeps_the_original_open_origin(self):
        root = self.project(fx.base_policy())
        cand = fx.candidate("https://example.com/reassess-age")
        self.register(root, [cand])
        data = self.ledger(root)
        data["records"][cand["url"]]["queued_at"] = fx.stamp(days=-40)
        data["records"][cand["url"]]["first_seen"] = fx.stamp(days=-40)
        sel.save_ledger(root, data)
        self.decide_ok(root, cand["url"], "reject", rule="routine-roundup",
                       quotes=("material platform release",))
        self.write_run(root, [], generated_at=fx.stamp(minutes=1))
        self.select_json(root, "reassess", "--key", cand["url"], "--apply")
        overview = sel.ledger_overview(self.ledger(root), sel.load_policy(root))
        self.assertGreaterEqual(overview["oldest_open_days"], 40, overview)

    def test_digest_reports_outstanding_age_not_activity_age(self):
        root = self.project(fx.base_policy())
        cand = self.aged_awaiting(root)
        run = self.write_run(root, [cand], generated_at=fx.stamp(minutes=1))
        self.register(root, [cand], run=run)
        (root / "index.csv").write_text("id,title\n", encoding="utf-8")
        res = subprocess.run(
            [fx.PY, str(root / "scripts" / "build_digest.py"),
             "--run", str(run), "--gate", str(root / ".state/runs/none-gate.json")],
            capture_output=True, text=True, cwd=str(root), timeout=120,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("历史未结选题", res.stdout)
        match = re.search(r"最久 (\d+) 天", res.stdout)
        self.assertIsNotNone(match, res.stdout)
        self.assertGreaterEqual(int(match.group(1)), 40, res.stdout)


class EvictedWorkOverflowTests(fx.ProjectCase):
    """Work evicted before the merge stays explicit and later recoverable."""

    def accepted(self, root: Path) -> dict:
        cand = fx.candidate("https://example.com/owed")
        self.register(root, [cand])
        self.decide_ok(root, cand["url"], "accept", rule="material-release",
                       quotes=("material platform release",))
        return cand

    def fillers(self):
        limit = sel.pending_queue_limit()
        return {
            f"https://example.com/new-{i}": {
                "url": f"https://example.com/new-{i}",
                "source_id": "alpha",
                "queued_at": fx.stamp(),
                "reason": "global_max",
            }
            for i in range(limit)
        }

    def test_premerge_eviction_of_accepted_work_is_marked_and_reported(self):
        import fetch_sources as fs

        root = self.project(fx.base_policy())
        cand = self.accepted(root)
        old = {cand["url"]: {"url": cand["url"], "source_id": "alpha",
                             "queued_at": fx.stamp(days=-5)}}
        merged, _applied, evicted = fs.merge_pending_entries(
            disk_entries=old, baseline_entries=old, upserts=self.fillers(),
            remove_keys=set(), limit=sel.pending_queue_limit(),
        )
        self.assertTrue(any(e[0] == cand["url"] and e[2] == "over_limit"
                            for e in evicted), evicted)
        ledger = self.ledger(root)
        queued_before = ledger["records"][cand["url"]]["queued_at"]
        out = sel.apply_commit(ledger, sel.load_policy(root), run_gen=fx.stamp(minutes=1),
                               candidates=[], observed=[], merged=merged, evicted=evicted)
        record = ledger["records"][cand["url"]]
        self.assertTrue(record.get("queue_overflow"), out)
        self.assertEqual(record["queued_at"], queued_before,
                         "an eviction must keep the original enqueue age")
        self.assertLessEqual(len(merged), sel.pending_queue_limit())
        self.assertEqual(out["overflow"], 1, out)
        overview = sel.ledger_overview(ledger, sel.load_policy(root))
        self.assertEqual(overview["overflow"], [cand["url"]])
        sel.save_ledger(root, ledger)
        status = self.select_json(root, "status")
        self.assertIn(cand["url"], status["open_detail"]["overflow"])

    def test_evicted_work_recovers_when_a_later_run_has_room(self):
        root = self.project(fx.base_policy())
        cand = self.accepted(root)
        ledger = self.ledger(root)
        ledger["records"][cand["url"]]["queue_overflow"] = True
        ledger["records"][cand["url"]]["queue_overflow_at"] = fx.stamp(days=-1)
        sel.save_ledger(root, ledger)
        queued_at = self.ledger(root)["records"][cand["url"]]["queued_at"]

        merged: dict = {}
        out = sel.apply_commit(ledger, sel.load_policy(root), run_gen=fx.stamp(minutes=1),
                               candidates=[], observed=[], merged=merged, evicted=[])
        self.assertIn(cand["url"], merged, out)
        self.assertEqual(merged[cand["url"]]["queued_at"], queued_at)
        self.assertFalse(ledger["records"][cand["url"]].get("queue_overflow"), out)
        self.assertEqual(sel.ledger_overview(ledger, sel.load_policy(root))["overflow"], [])

    def test_digest_reports_overflow_marked_work(self):
        root = self.project(fx.base_policy())
        cand = self.accepted(root)
        ledger = self.ledger(root)
        ledger["records"][cand["url"]]["queue_overflow"] = True
        sel.save_ledger(root, ledger)
        run = self.write_run(root, [], generated_at=fx.stamp(minutes=1))
        (root / "index.csv").write_text("id,title\n", encoding="utf-8")
        res = subprocess.run(
            [fx.PY, str(root / "scripts" / "build_digest.py"),
             "--run", str(run), "--gate", str(root / ".state/runs/none-gate.json")],
            capture_output=True, text=True, cwd=str(root), timeout=120,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("待补队列溢出", res.stdout)
        self.assertIn(cand["url"], res.stdout)

    def test_an_evicted_pending_entry_without_a_ledger_record_is_coverage_loss(self):
        root = self.project(fx.base_policy())
        self.register(root, [fx.candidate("https://example.com/registered")])
        ledger = self.ledger(root)
        url = "https://example.com/cap-parked-unregistered"
        evicted = [(url, {"url": url, "source_id": "alpha",
                          "queued_at": fx.stamp(days=-2)}, "over_limit")]
        before = (root / ".state/selection/ledger.json").read_bytes()
        out = sel.apply_commit(ledger, sel.load_policy(root), run_gen=fx.stamp(minutes=1),
                               candidates=[], observed=[], merged={}, evicted=evicted)
        self.assertEqual(out["overflow"], 0, out)
        self.assertNotIn(url, ledger["records"],
                         "a capacity exit must not be registered as durable work")
        self.assertEqual(before, (root / ".state/selection/ledger.json").read_bytes())


class SeenProofRecoveryTests(fx.ProjectCase):
    """Exact seen proof closes every eligible open state, keeping history."""

    def accepted(self, root: Path, url: str = "https://example.com/owed") -> dict:
        cand = fx.candidate(url)
        self.register(root, [cand])
        self.decide_ok(root, cand["url"], "accept", rule="material-release",
                       quotes=("material platform release",))
        return cand

    def commit(self, root: Path, ledger: dict, *, merged: dict, seen: set[str]):
        return sel.apply_commit(ledger, sel.load_policy(root), run_gen=fx.stamp(minutes=1),
                                candidates=[], observed=[], merged=merged, evicted=[],
                                seen_keys=seen)

    def test_seen_proof_closes_accepted_ready_work_after_an_interrupted_mark(self):
        root = self.project(fx.base_policy())
        cand = self.accepted(root)
        ledger = self.ledger(root)
        merged = {cand["url"]: {"url": cand["url"], "source_id": "alpha",
                                "queued_at": fx.stamp()}}
        history_before = len(ledger["records"][cand["url"]]["history"])
        out = self.commit(root, ledger, merged=merged, seen={cand["url"]})

        record = ledger["records"][cand["url"]]
        self.assertEqual(record["state"], "archived", out)
        self.assertEqual(record["outcome"], "accept",
                         "the editorial outcome must survive recovery")
        self.assertEqual(record["archive_evidence"], "seen")
        self.assertNotIn(cand["url"], merged, "recovered work must leave the pending queue")
        self.assertEqual(len(record["history"]), history_before + 1)
        self.assertEqual(record["history"][-1]["action"], "seen_archive")
        overview = sel.ledger_overview(ledger, sel.load_policy(root))
        self.assertEqual(overview["open_total"], 0, overview)

    def test_seen_proof_closes_budget_wait_and_archive_failure_too(self):
        for seed_state in (sel.STATE_BUDGET_WAIT, sel.STATE_ARCHIVE_FAILURE):
            with self.subTest(state=seed_state):
                root = self.project(fx.base_policy())
                cand = self.accepted(root)
                ledger = self.ledger(root)
                rec = ledger["records"][cand["url"]]
                rec["state"] = seed_state
                rec["failure_reason"] = "synthetic technical failure"
                rec["queue_overflow"] = True
                rec["queue_overflow_at"] = fx.stamp(days=-1)
                merged = {cand["url"]: {"url": cand["url"], "source_id": "alpha"}}
                out = self.commit(root, ledger, merged=merged, seen={cand["url"]})
                self.assertEqual(rec["state"], "archived", out)
                self.assertNotIn("failure_reason", rec)
                self.assertFalse(rec.get("queue_overflow"))
                self.assertNotIn("queue_overflow_at", rec)
                self.assertEqual(rec["outcome"], "accept")
                self.assertNotIn(cand["url"], merged)

    def test_a_generic_url_cannot_close_a_fragment_candidate(self):
        root = self.project(fx.base_policy())
        url = "https://example.com/docs#materials"
        cand = self.accepted(root, url)
        ledger = self.ledger(root)
        merged = {url: {"url": url, "source_id": "alpha"}}
        out = self.commit(root, ledger, merged=merged,
                          seen={sel.dedupe_key("https://example.com/docs")})
        self.assertEqual(ledger["records"][url]["state"], "ready", out)
        self.assertIn(url, merged)

        exact = self.commit(root, ledger, merged=merged, seen={url})
        self.assertEqual(ledger["records"][url]["state"], "archived", exact)

    def test_recovery_is_idempotent_across_later_fetches(self):
        root = self.project(fx.base_policy())
        cand = self.accepted(root)
        ledger = self.ledger(root)
        merged: dict = {}
        first = self.commit(root, ledger, merged=merged, seen={cand["url"]})
        self.assertEqual(first["seen_closed"], 1, first)
        history_len = len(ledger["records"][cand["url"]]["history"])

        second = self.commit(root, ledger, merged=merged, seen={cand["url"]})
        self.assertEqual(second["seen_closed"], 0, second)
        self.assertEqual(len(ledger["records"][cand["url"]]["history"]), history_len,
                         "a repeated seen observation must not append history")
        self.assertEqual(merged, {})
        self.assertEqual(sel.ledger_overview(ledger, sel.load_policy(root))["open_total"], 0)

    def test_seen_proof_leaves_terminal_rejects_and_manual_history_alone(self):
        root = self.project(fx.base_policy())
        cand = fx.candidate("https://example.com/excluded")
        self.register(root, [cand])
        self.decide_ok(root, cand["url"], "reject", rule="routine-roundup",
                       quotes=("material platform release",))
        ledger = self.ledger(root)
        record = ledger["records"][cand["url"]]
        record["history"].append({"action": "manual_override", "at": fx.stamp()})
        ledger["records"][cand["url"]]["manual_override_at"] = fx.stamp()
        merged: dict = {}
        out = self.commit(root, ledger, merged=merged, seen={cand["url"]})
        self.assertEqual(record["state"], "rejected", out)
        self.assertEqual(record["outcome"], "reject")
        self.assertEqual(record["history"][-1]["action"], "manual_override")
        self.assertEqual(out["seen_closed"], 0, out)


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
