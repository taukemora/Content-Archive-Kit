"""Offline checks for the opt-in real-model probe runner; no Hermes imports."""
from __future__ import annotations

import json
import logging
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from run_skill_intake_probe import _worker, audit_result, child_environment
from skill_intake_harness import SYNTHETIC_URL
from _support import scratch_dir


README_READ = {"id": "read-1", "function": {
    "name": "read_file", "arguments": '{"path":"README.md"}'}}
EXTRACT_CALL = {"id": "extract-1", "function": {
    "name": "web_extract", "arguments": json.dumps({"url": SYNTHETIC_URL})}}
ADD_CALL = {"id": "add-1", "function": {"name": "terminal", "arguments": json.dumps({
    "command": "bin/ca add " + SYNTHETIC_URL})}}


def call_event(call):
    return {"name": call["function"]["name"], "args": json.loads(call["function"]["arguments"])}


def complete_readme_reply(call_id="read-1"):
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps({
        "ok": True, "name": "read_file", "path": "/synthetic/README.md",
        "offset": 1, "total_lines": 2, "content": "Rules\nSecond line"})}


def result_with(calls):
    return {
        "completed": True, "failed": False, "partial": False,
        "interrupted": False, "api_calls": 2,
        "provider": "test-provider", "model": "test-model",
        "messages": [{"role": "assistant", "tool_calls": calls}],
    }


class ProbeRunnerTests(unittest.TestCase):
    def test_environment_does_not_inherit_profile_or_credentials(self):
        env = child_environment(
            Path("/synthetic/home"),
            {"PATH": "/usr/bin", "LANG": "C.UTF-8", "PYTHONPATH": "/private",
             "HERMES_HOME": "/private", "OTHER_API_KEY": "fixture-not-a-key",
             "HERMES_KANBAN_TASK": "unrelated-task"},
        )
        self.assertEqual(env["HOME"], "/synthetic/home")
        self.assertEqual(env["HERMES_HOME"], "/synthetic/home")
        self.assertEqual(env["HERMES_IGNORE_RULES"], "1")
        self.assertEqual(env["HERMES_IGNORE_USER_CONFIG"], "1")
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("OTHER_API_KEY", env)
        self.assertNotIn("HERMES_KANBAN_TASK", env)

    def test_exact_raw_calls_match_dispatch_observations(self):
        call = {"id": "call-1", "function": {
            "name": "read_file", "arguments": '{"path":"/synthetic/README.md"}'}}
        events = [{"name": "read_file", "args": {"path": "/synthetic/README.md"}}]
        self.assertEqual(audit_result(result_with([call]), events, "test-provider", "test-model"), [])

    def test_unobserved_or_unknown_call_cannot_pass(self):
        call = {"function": {"name": "undeclared_tool", "arguments": "{}"}}
        self.assertTrue(audit_result(result_with([call]), [], "test-provider", "test-model"))

    def test_parallel_read_completion_order_is_not_call_loss(self):
        calls = [{"function": {"name": "read_file", "arguments": '{"path":"' + p + '"}'}}
                 for p in ("taxonomy.yaml", "index.csv")]
        events = [{"name": "read_file", "args": {"path": p}}
                  for p in ("index.csv", "taxonomy.yaml")]
        self.assertEqual(audit_result(result_with(calls), events, "test-provider", "test-model"), [])

    def test_call_multiplicity_and_arguments_must_still_match(self):
        call = {"function": {"name": "read_file", "arguments": '{"path":"README.md"}'}}
        event = {"name": "read_file", "args": {"path": "README.md"}}
        for calls, events in (([call, call], [event]), ([call], [event, event]),
                              ([call], [{"name": "read_file", "args": {"path": "other"}}])):
            with self.subTest(calls=calls, events=events):
                self.assertTrue(audit_result(result_with(calls), events, "test-provider", "test-model"))

    def test_bad_arguments_and_missing_result_fields_fail_closed(self):
        for args in ("not-json", "null", "[]"):
            with self.subTest(args=args):
                call = {"function": {"name": "read_file", "arguments": args}}
                self.assertTrue(audit_result(result_with([call]), [], "test-provider", "test-model"))
        self.assertTrue(audit_result({}, [], "test-provider", "test-model"))

    def test_failed_incomplete_or_fallback_run_cannot_pass(self):
        for field, value in (("completed", False), ("failed", True),
                             ("partial", True), ("interrupted", True),
                             ("provider", "other"), ("model", "other"),
                             ("api_calls", 0)):
            with self.subTest(field=field):
                result = result_with([])
                result[field] = value
                self.assertTrue(audit_result(result, [], "test-provider", "test-model"))

    def test_completed_zero_tool_turn_is_not_an_adapter_failure(self):
        # The observer decides whether a positive case required an intake call.
        self.assertEqual(audit_result(result_with([]), [], "test-provider", "test-model"), [])

    def test_boolean_api_count_is_not_evidence_of_a_model_call(self):
        result = result_with([])
        result["api_calls"] = True
        self.assertTrue(audit_result(result, [], "test-provider", "test-model"))

    def test_read_and_add_in_same_model_turn_cannot_use_unseen_readme(self):
        read = {"id": "read-1", "function": {
            "name": "read_file", "arguments": '{"path":"README.md"}'}}
        add = {"id": "add-1", "function": {"name": "terminal", "arguments": json.dumps({
            "command": "bin/ca add https://example.com/synthetic-article"})}}
        events = [{"name": c["function"]["name"], "args": json.loads(c["function"]["arguments"])}
                  for c in (read, add)]
        reply = {"role": "tool", "tool_call_id": "read-1", "content": json.dumps({
            "ok": True, "name": "read_file", "path": "/synthetic/README.md",
            "offset": 1, "total_lines": 2, "content": "Rules\nSecond line"})}
        combined = result_with([read, add])
        combined["messages"].append(reply)
        self.assertTrue(audit_result(combined, events, "test-provider", "test-model"))
        separated = result_with([read])
        separated["messages"] += [reply, {"role": "assistant", "tool_calls": [add]}]
        self.assertEqual(audit_result(separated, events, "test-provider", "test-model"), [])

    def test_missing_failed_partial_or_wrong_id_readme_reply_cannot_authorize_add(self):
        read = {"id": "read-1", "function": {
            "name": "read_file", "arguments": '{"path":"README.md"}'}}
        add = {"id": "add-1", "function": {"name": "terminal", "arguments": json.dumps({
            "command": "bin/ca add https://example.com/synthetic-article"})}}
        events = [{"name": c["function"]["name"], "args": json.loads(c["function"]["arguments"])}
                  for c in (read, add)]
        for variant in ("missing", "failed", "partial", "wrong-id"):
            with self.subTest(variant=variant):
                result = result_with([read])
                payload = {"ok": variant != "failed", "name": "read_file", "path": "/synthetic/README.md",
                           "offset": 1, "total_lines": 2, "content": "Rules\nSecond line"}
                if variant == "partial":
                    payload["content"] = "Rules"
                if variant != "missing":
                    result["messages"].append({"role": "tool", "tool_call_id": (
                        "wrong" if variant == "wrong-id" else "read-1"), "content": json.dumps(payload)})
                result["messages"].append({"role": "assistant", "tool_calls": [add]})
                self.assertTrue(audit_result(result, events, "test-provider", "test-model"))

    def test_extract_in_the_same_turn_as_the_readme_cannot_use_it(self):
        # Same assistant turn: neither completion order makes the README text
        # available to the model before it requests the extract.
        events = [call_event(README_READ), call_event(EXTRACT_CALL)]
        for order in (events, list(reversed(events))):
            with self.subTest(events=[item["name"] for item in order]):
                result = result_with([README_READ, EXTRACT_CALL])
                result["messages"].append(complete_readme_reply())
                self.assertTrue(audit_result(result, order, "test-provider", "test-model",
                                             require_readme_for_extract=True))

    def test_extract_after_a_prior_turn_complete_readme_reply_passes(self):
        events = [call_event(README_READ), call_event(EXTRACT_CALL)]
        for order in (events, list(reversed(events))):
            with self.subTest(events=[item["name"] for item in order]):
                result = result_with([README_READ])
                result["messages"] += [complete_readme_reply(),
                                       {"role": "assistant", "tool_calls": [EXTRACT_CALL]}]
                self.assertEqual(audit_result(result, order, "test-provider", "test-model",
                                              require_readme_for_extract=True), [])

    def test_extract_without_a_readme_is_allowed_only_without_the_positive_gate(self):
        events = [call_event(EXTRACT_CALL)]
        result = result_with([EXTRACT_CALL])
        self.assertEqual(audit_result(result, events, "test-provider", "test-model"), [])
        self.assertTrue(audit_result(result, events, "test-provider", "test-model",
                                     require_readme_for_extract=True))

    def test_failed_reread_does_not_revoke_an_earlier_complete_reply(self):
        reread = {"id": "read-2", "function": {"name": "read_file", "arguments": json.dumps(
            {"path": "README.md", "offset": 9999})}}
        error_reply = {"role": "tool", "tool_call_id": "read-2", "content": json.dumps(
            {"ok": False, "name": "read_file", "error": "requested line range is empty"})}
        events = [call_event(README_READ), call_event(reread), call_event(EXTRACT_CALL)]
        result = result_with([README_READ])
        result["messages"] += [complete_readme_reply(),
                               {"role": "assistant", "tool_calls": [reread]},
                               error_reply,
                               {"role": "assistant", "tool_calls": [EXTRACT_CALL]}]
        self.assertEqual(audit_result(result, events, "test-provider", "test-model",
                                      require_readme_for_extract=True), [])

    def test_extract_after_a_failed_partial_missing_or_wrong_id_reply_is_rejected(self):
        events = [call_event(README_READ), call_event(EXTRACT_CALL)]
        for variant in ("missing", "failed", "partial", "wrong-id"):
            with self.subTest(variant=variant):
                result = result_with([README_READ])
                payload = {"ok": variant != "failed", "name": "read_file",
                           "path": "/synthetic/README.md", "offset": 1,
                           "total_lines": 2, "content": "Rules\nSecond line"}
                if variant == "partial":
                    payload["content"] = "Rules"
                if variant != "missing":
                    result["messages"].append({"role": "tool", "tool_call_id": (
                        "wrong" if variant == "wrong-id" else "read-1"),
                        "content": json.dumps(payload)})
                result["messages"].append({"role": "assistant", "tool_calls": [EXTRACT_CALL]})
                self.assertTrue(audit_result(result, events, "test-provider", "test-model",
                                             require_readme_for_extract=True))

    def test_readme_gate_for_extract_is_keyword_only(self):
        with self.assertRaises(TypeError):
            audit_result(result_with([]), [], "test-provider", "test-model", True)


class WorkerBoundaryTests(unittest.TestCase):
    """Fake only the Hermes framework; exercise the real worker guards offline."""

    def run_worker(self, mode, *, expect_save=False, case_id="bare-url"):
        base = scratch_dir("skill-worker-")
        root, home, other, source = [base / n for n in ("archive", "home", "other", "source")]
        for path in (root, home, other, source):
            path.mkdir()
        entries, inline = {}, {}
        self.run_called = False
        self.system_message = ""
        self.user_message = ""
        owner = self

        def prepare_logging(**kwargs):
            (home / "logs").mkdir(exist_ok=True)
            for name in ("agent.log", "errors.log"):
                (home / "logs" / name).touch()

        def prepare_scratch(**kwargs):
            target = home / "cache" / "scratch"
            target.mkdir(parents=True, exist_ok=True)
            (target / ".last_prune").touch()
            return target

        class Registry:
            def get_all_tool_names(self):
                return list(entries)

            def deregister(self, name, **kwargs):
                entries.pop(name, None)

            def current_scope_key(self):
                return "synthetic"

            def register(self, name, handler, schema, **kwargs):
                entries[name] = SimpleNamespace(handler=handler, schema=schema)

            def get_entry(self, name):
                return entries.get(name)

        class Agent:
            def __init__(self, **kwargs):
                prepare_logging()
                prepare_scratch()
                self.tools = [{"function": e.schema} for e in entries.values()]
                if mode == "replace-handler":
                    entries["terminal"].handler = lambda args, **kw: "unbounded"
                elif mode == "add-tool":
                    entries["unexpected"] = SimpleNamespace(handler=lambda args: "bad")
                elif mode == "add-inline":
                    inline["terminal"] = lambda args: "bad"
                elif mode == "init-write":
                    (home / "init-state").write_text("synthetic", encoding="utf-8")

            def run_conversation(self, *args, **kwargs):
                owner.run_called = True
                owner.system_message = kwargs["system_message"]
                owner.user_message = args[0] if args else ""
                if mode == "run-write":
                    (other / "state").write_text("synthetic", encoding="utf-8")
                elif mode == "run-write-log":
                    (home / "logs" / "agent.log").write_text("changed", encoding="utf-8")
                result = result_with([])
                if mode == "extract-only":
                    # One recorded extract with no README prerequisite anywhere.
                    entries["web_extract"].handler({"url": SYNTHETIC_URL})
                    result = result_with([{"id": "extract-1", "function": {
                        "name": "web_extract",
                        "arguments": json.dumps({"url": SYNTHETIC_URL})}}])
                if mode == "wrong-route":
                    result["model"] = "other-model"
                return result

            def close(self):
                if mode == "close-write":
                    (home / "close-state").write_text("synthetic", encoding="utf-8")

        modules = {
            "hermes_constants": SimpleNamespace(set_hermes_home_override=lambda path: None,
                                                get_scratch_dir=prepare_scratch),
            "hermes_logging": SimpleNamespace(setup_logging=prepare_logging),
            "run_agent": SimpleNamespace(AIAgent=Agent),
            "tools.registry": SimpleNamespace(registry=Registry()),
            "agent.inline_tool_executors": SimpleNamespace(INLINE_TOOL_EXECUTORS=inline),
        }
        payload = {"hermes_root": str(source), "root": str(root), "cwd": str(other),
                   "runtime": {"provider": "test-provider"}, "model": "test-model", "skill": "synthetic",
                   "case": {"id": case_id, "message": "https://example.com/synthetic-article",
                            "expect_save": expect_save, "cross_project": True}}
        previous_logging = logging.root.manager.disable
        try:
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}), patch.object(sys, "path", list(sys.path)), \
                    patch("run_skill_intake_probe.import_module", side_effect=modules.__getitem__):
                return _worker(payload)
        finally:
            logging.disable(previous_logging)

    def test_unchanged_bounded_worker_can_complete(self):
        result = self.run_worker("normal")
        self.assertTrue(result["passed"])
        self.assertIn("home/logs/agent.log", result.get("bootstrap_files", {}))
        self.assertIn("home/cache/scratch/.last_prune", result.get("bootstrap_files", {}))

    def test_failed_audit_does_not_keep_an_all_checks_passed_reason(self):
        result = self.run_worker("wrong-route")
        self.assertFalse(result["passed"])
        self.assertNotIn("all checks passed", result["reasons"])

    def test_system_context_declares_dry_run_without_case_verdict(self):
        self.run_worker("normal")
        self.assertIn("dry-run", self.system_message)
        self.assertIn("record intended operations", self.system_message)
        self.assertNotIn("expect_save", self.system_message)
        self.assertNotIn("bare-url", self.system_message)

    def test_first_change_phase_is_declared_without_teaching_the_action(self):
        contexts = []
        for expect_save in (False, True):
            with self.subTest(expect_save=expect_save):
                self.run_worker("normal", expect_save=expect_save, case_id="case-sentinel")
                # Only the two per-run paths vary; the task context is shared.
                contexts.append("\n".join(line for line in self.system_message.splitlines()
                                          if not line.startswith(("Current working directory:",
                                                                  "Known existing archive root:"))))
                self.assertIn("ends at the first change intent", self.system_message)
                self.assertIn("Stop after its tool reply", self.system_message)
                self.assertIn("do not proceed to editorial or derived operations", self.system_message)
                for hidden in ("expect_save", "case-sentinel", "bin/ca add", "Save:", "Archive:"):
                    self.assertNotIn(hidden, self.system_message)
        self.assertEqual(contexts[0], contexts[1])

    def test_positive_case_extract_gate_is_applied_inside_the_worker(self):
        positive = self.run_worker("extract-only", expect_save=True, case_id="save-cn")
        self.assertFalse(positive["passed"])
        self.assertIn(
            "extract requested before a complete README reply in a prior model turn",
            positive["reasons"],
        )
        negative = self.run_worker("extract-only")
        self.assertTrue(negative["passed"], negative["reasons"])
        self.assertNotIn(
            "extract requested before a complete README reply in a prior model turn",
            negative["reasons"],
        )
        # The scorer-only flag never becomes model input.
        for field in ("system_message", "user_message"):
            self.assertNotIn("require_readme_for_extract", getattr(self, field))
            self.assertNotIn("expect_save", getattr(self, field))

    def test_initializer_cannot_replace_or_expand_dispatch_surface(self):
        for mode in ("replace-handler", "add-tool", "add-inline"):
            with self.subTest(mode=mode):
                with self.assertRaises(RuntimeError):
                    self.run_worker(mode)
                self.assertFalse(self.run_called)

    def test_home_and_cross_project_writes_are_observed_through_close(self):
        for mode in ("init-write", "run-write", "close-write", "run-write-log"):
            with self.subTest(mode=mode):
                self.assertFalse(self.run_worker(mode)["passed"])


if __name__ == "__main__":
    unittest.main()
