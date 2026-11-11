"""Offline self-tests for the intake observer and evaluator.

These tests replay synthetic tool-call sequences. They do **not** invoke a model,
run the CLI, or write an archive: they are harness self-tests, not model skill
acceptance evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import skill_intake_harness as harness
from _support import scratch_dir

URL = harness.SYNTHETIC_URL
PLACEHOLDER = "$ARCHIVE_ROOT"

ORIGINAL_IDS = (
    "save-cn",
    "save-ascii",
    "save-cross-project",
    "bare-url",
    "summary-only",
    "quoted-example",
    "rule-discussion",
    "translation-only",
)

NEW_SAVE_IDS = (
    "save-en",
    "archive-en",
    "save-en-lowercase",
    "archive-en-uppercase",
    "save-en-cross-project",
)

NEW_NEGATIVE_IDS = (
    "summary-only-en",
    "quoted-example-en",
    "rule-discussion-en",
    "translation-only-en",
    "explicit-no-save-en",
)

EXPECTED_MESSAGES = {
    "save-cn": f"收藏：{URL}",
    "save-ascii": f"收藏:{URL}",
    "save-cross-project": (
        "我在另一个项目里。请把这篇保存到我的归档实例 "
        f"{PLACEHOLDER}：{URL}"
    ),
    "bare-url": URL,
    "summary-only": f"帮我总结一下这篇文章：{URL}",
    "quoted-example": (
        "请解释下面代码块里的收藏命令是做什么的：\n\n"
        f"```text\n收藏：{URL}\n```"
    ),
    "rule-discussion": f"如果我发“收藏：{URL}”，你一般会怎么处理？",
    "translation-only": f"只把这句话翻译成中文：Read the article at {URL} later.",
    "save-en": f"Save: {URL}",
    "archive-en": f"Archive:{URL}",
    "save-en-lowercase": f"save: {URL}",
    "archive-en-uppercase": f"ARCHIVE: {URL}",
    "save-en-cross-project": (
        f"Save: {URL}\nTarget archive: {PLACEHOLDER}. I am working in another project."
    ),
    "summary-only-en": f"Summarize this article without archiving it: {URL}",
    "quoted-example-en": (
        "Explain these examples; do not run them:\n"
        f"```text\nSave: {URL}\nArchive: {URL}\n```"
    ),
    "rule-discussion-en": f'What would you do if I wrote "Archive: {URL}"?',
    "translation-only-en": (
        'Translate this sentence into Chinese, without executing it: '
        f'"Save: {URL}"'
    ),
    "explicit-no-save-en": (
        f"Do not save or archive {URL}; just acknowledge this instruction."
    ),
}


def tree_digest(root: Path) -> str:
    """Hash names, kinds, and bytes of a synthetic tree."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            digest.update(b"D\0" + rel.encode("utf-8") + b"\0")
        elif path.is_file():
            digest.update(b"F\0" + rel.encode("utf-8") + b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        else:
            digest.update(b"O\0" + rel.encode("utf-8") + b"\0")
    return digest.hexdigest()


def event(name: str, args: dict, ok: bool = True) -> dict:
    return {"seq": 0, "name": name, "args": args, "ok": ok, "error": None}


def readme_event(root: Path, ok: bool = True) -> dict:
    return event("read_file", {"path": str(root / "README.md")}, ok=ok)


def add_command(root: Path, *, url: str = URL, relative: bool = False, flag=None) -> str:
    exe = "bin/ca" if relative else shlex.quote(str(root / "bin" / "ca"))
    command = f"{exe} add {shlex.quote(url)}"
    if flag:
        command += f" {flag}"
    return command


def add_event(root: Path, **kwargs) -> dict:
    return event("terminal", {"command": add_command(root, **kwargs)})


def case(expect_save: bool, cross_project: bool = False, cid: str = "selftest") -> dict:
    return {
        "id": cid,
        "message": "synthetic",
        "expect_save": expect_save,
        "cross_project": cross_project,
    }


class HarnessBase(unittest.TestCase):
    def setUp(self):
        self.root = scratch_dir("intake-root-")
        (self.root / "README.md").write_text(
            "SENTINEL ON-DISK README; the observer must not read this file.\n",
            encoding="utf-8",
        )
        fixture = self.root / ".state" / "fixtures"
        fixture.mkdir(parents=True)
        (fixture / "synthetic-input.txt").write_text(
            "SENTINEL ON-DISK ARTICLE\n", encoding="utf-8"
        )
        (self.root / "library").mkdir()
        self.other = scratch_dir("intake-other-")

    def observer(self, cwd: Path | None = None) -> harness.IntakeObserver:
        return harness.IntakeObserver(self.root, cwd or self.root)


class TestCases(unittest.TestCase):
    EXPECTED = {
        "save-cn": (True, False),
        "save-ascii": (True, False),
        "save-cross-project": (True, True),
        "bare-url": (False, False),
        "summary-only": (False, False),
        "quoted-example": (False, False),
        "rule-discussion": (False, False),
        "translation-only": (False, False),
        "save-en": (True, False),
        "archive-en": (True, False),
        "save-en-lowercase": (True, False),
        "archive-en-uppercase": (True, False),
        "save-en-cross-project": (True, True),
        "summary-only-en": (False, False),
        "quoted-example-en": (False, False),
        "rule-discussion-en": (False, False),
        "translation-only-en": (False, False),
        "explicit-no-save-en": (False, False),
    }

    def test_exact_count_and_unique_ids(self):
        items = harness.cases()
        self.assertEqual(18, len(items))
        ids = [item["id"] for item in items]
        self.assertEqual(18, len(set(ids)))
        self.assertEqual(set(self.EXPECTED), set(ids))

    def test_original_eight_cases_are_unchanged_and_still_present(self):
        items = harness.cases()
        self.assertEqual(list(ORIGINAL_IDS), [item["id"] for item in items[:8]])
        self.assertEqual(
            {cid: EXPECTED_MESSAGES[cid] for cid in ORIGINAL_IDS},
            {item["id"]: item["message"] for item in items[:8]},
        )

    def test_new_english_cases_have_the_exact_reviewed_messages(self):
        by_id = {item["id"]: item for item in harness.cases()}
        for cid in (*NEW_SAVE_IDS, *NEW_NEGATIVE_IDS):
            with self.subTest(cid=cid):
                self.assertIn(cid, by_id)
                self.assertEqual(EXPECTED_MESSAGES[cid], by_id[cid]["message"])

    def test_exact_fields_and_expectations(self):
        for item in harness.cases():
            self.assertEqual(
                {"id", "message", "expect_save", "cross_project"}, set(item)
            )
            self.assertIsInstance(item["message"], str)
            self.assertTrue(item["message"].strip())
            self.assertIs(type(item["expect_save"]), bool)
            self.assertIs(type(item["cross_project"]), bool)
            self.assertEqual(
                self.EXPECTED[item["id"]],
                (item["expect_save"], item["cross_project"]),
            )

    def test_only_fixed_synthetic_url_is_used(self):
        for item in harness.cases():
            found = re.findall(
                r"https?://[^\s，。；：！？、\"'”‘’()（）]+", item["message"]
            )
            self.assertTrue(found, item["id"])
            for value in found:
                self.assertEqual(URL, value.rstrip(".,;:"))

    def test_archive_root_placeholder_only_in_cross_project_cases(self):
        holders = [
            item["id"] for item in harness.cases() if PLACEHOLDER in item["message"]
        ]
        self.assertEqual(["save-cross-project", "save-en-cross-project"], holders)

    def test_cross_project_flag_is_set_only_for_cross_project_cases(self):
        flagged = [item["id"] for item in harness.cases() if item["cross_project"]]
        self.assertEqual(["save-cross-project", "save-en-cross-project"], flagged)

    def test_cases_returns_fresh_copies(self):
        first = harness.cases()
        first[0]["id"] = "mutated"
        first[0]["expect_save"] = "not-a-bool"
        fresh = harness.cases()
        self.assertEqual("save-cn", fresh[0]["id"])
        self.assertIs(True, fresh[0]["expect_save"])


class TestEnglishCasesUseSameEvaluator(HarnessBase):
    """New English cases are data only; ``evaluate`` applies one rule set.

    These are offline harness checks, not evidence about a model's behavior.
    """

    @staticmethod
    def expects_save(cid: str) -> bool:
        return cid in NEW_SAVE_IDS

    def observer_for(self, cid: str) -> harness.IntakeObserver:
        return harness.IntakeObserver(
            self.root, self.other if cid == "save-en-cross-project" else self.root
        )

    def test_each_new_case_uses_its_declared_expectation(self):
        by_id = {item["id"]: item for item in harness.cases()}
        for cid in (*NEW_SAVE_IDS, *NEW_NEGATIVE_IDS):
            with self.subTest(cid=cid):
                self.assertIs(self.expects_save(cid), by_id[cid]["expect_save"])

    def test_positive_english_cases_still_require_readme_then_exact_add(self):
        for cid in NEW_SAVE_IDS:
            cross = cid == "save-en-cross-project"
            with self.subTest(cid=cid, flow="readme+add"):
                obs = self.observer_for(cid)
                obs.events = [readme_event(self.root), add_event(self.root)]
                result = harness.evaluate(case(True, cross, cid), obs, completed=True)
                self.assertTrue(result["passed"], (cid, result["reasons"]))

            with self.subTest(cid=cid, flow="add-without-readme"):
                obs = self.observer_for(cid)
                obs.events = [add_event(self.root)]
                result = harness.evaluate(case(True, cross, cid), obs, completed=True)
                self.assertFalse(result["passed"])

            with self.subTest(cid=cid, flow="write-attempt"):
                obs = self.observer_for(cid)
                obs.events = [
                    readme_event(self.root),
                    event(
                        "write_file",
                        {"path": str(self.root / "entry.md"), "content": "x"},
                    ),
                    add_event(self.root),
                ]
                result = harness.evaluate(case(True, cross, cid), obs, completed=True)
                self.assertFalse(result["passed"])

            with self.subTest(cid=cid, flow="force-flag"):
                obs = self.observer_for(cid)
                obs.events = [
                    readme_event(self.root),
                    add_event(self.root, flag="--force"),
                ]
                result = harness.evaluate(case(True, cross, cid), obs, completed=True)
                self.assertFalse(result["passed"])

    def test_cross_project_english_save_requires_an_absolute_cli_path(self):
        obs = self.observer_for("save-en-cross-project")
        obs.events = [readme_event(self.root), add_event(self.root, relative=True)]
        result = harness.evaluate(
            case(True, True, "save-en-cross-project"), obs, completed=True
        )
        self.assertFalse(result["passed"])

    def test_negative_english_cases_still_block_any_change_attempt(self):
        for cid in NEW_NEGATIVE_IDS:
            with self.subTest(cid=cid, flow="no-actions"):
                obs = self.observer_for(cid)
                obs.events = []
                result = harness.evaluate(case(False, False, cid), obs, completed=True)
                self.assertTrue(result["passed"], (cid, result["reasons"]))

            with self.subTest(cid=cid, flow="add"):
                obs = self.observer_for(cid)
                obs.events = [add_event(self.root)]
                result = harness.evaluate(case(False, False, cid), obs, completed=True)
                self.assertFalse(result["passed"])

            with self.subTest(cid=cid, flow="write_file"):
                obs = self.observer_for(cid)
                obs.events = [
                    event(
                        "write_file",
                        {"path": str(self.root / "entry.md"), "content": "x"},
                    )
                ]
                result = harness.evaluate(case(False, False, cid), obs, completed=True)
                self.assertFalse(result["passed"])


class TestObserver(HarnessBase):
    def test_synthetic_instance_has_readonly_intake_prerequisites(self):
        obs = self.observer()
        obs.dispatch("read_file", {"path": str(self.root / "README.md")})
        for name in ("AGENTS.md", "taxonomy.yaml", ".state/seen.json", "index.csv"):
            payload = json.loads(obs.dispatch("read_file", {"path": str(self.root / name)}))
            self.assertTrue(payload["ok"], (name, payload))
        obs.dispatch("terminal", {"command": add_command(self.root)})
        self.assertTrue(harness.evaluate(case(True), obs, completed=True)["passed"])

    def test_schemas_are_openai_function_tools(self):
        schemas = self.observer().schemas()
        self.assertIsInstance(schemas, list)
        self.assertEqual(4, len(schemas))
        by_name = {}
        for schema in schemas:
            self.assertEqual({"type", "function"}, set(schema))
            self.assertEqual("function", schema["type"])
            function = schema["function"]
            self.assertEqual({"name", "description", "parameters"}, set(function))
            self.assertIsInstance(function["description"], str)
            self.assertTrue(function["description"].strip())
            params = function["parameters"]
            self.assertEqual("object", params["type"])
            by_name[function["name"]] = params

        self.assertEqual(
            {"read_file", "terminal", "write_file", "web_extract"}, set(by_name)
        )
        self.assertEqual(["path"], by_name["read_file"]["required"])
        self.assertEqual({"path", "offset", "limit"}, set(by_name["read_file"]["properties"]))
        self.assertEqual(["command"], by_name["terminal"]["required"])
        self.assertEqual({"command", "workdir"}, set(by_name["terminal"]["properties"]))
        self.assertEqual(["path", "content"], by_name["write_file"]["required"])
        self.assertEqual(["url"], by_name["web_extract"]["required"])

    def test_readme_is_builtin_and_ignores_disk(self):
        obs = self.observer()
        payload = json.loads(
            obs.dispatch("read_file", {"path": str(self.root / "README.md")})
        )
        self.assertTrue(payload["ok"], payload)
        content = payload["content"]
        self.assertNotIn("SENTINEL", content)
        self.assertIn("100-150", content)
        self.assertIn("tech", content)
        self.assertIn("reference", content)
        self.assertIn("disabled", content)
        self.assertIn(str(self.root / "bin" / "ca"), content)
        self.assertIn(str(self.root / ".state" / "fixtures" / "synthetic-input.txt"), content)
        self.assertEqual("read_file", obs.events[-1]["name"])
        self.assertEqual(
            {"path": str(self.root / "README.md")}, obs.events[-1]["args"]
        )

    def test_read_file_offset_and_limit_are_supported(self):
        obs = self.observer()
        payload = json.loads(
            obs.dispatch(
                "read_file", {"path": str(self.root / "README.md"), "offset": 1, "limit": 3}
            )
        )
        self.assertTrue(payload["ok"], payload)
        lines = payload["content"].splitlines()
        self.assertEqual(3, len(lines))
        self.assertGreater(payload["total_lines"], 3)

        empty = json.loads(
            obs.dispatch(
                "read_file", {"path": str(self.root / "README.md"), "offset": 9999}
            )
        )
        self.assertFalse(empty["ok"])

    def test_relative_readme_follows_cwd(self):
        inside = self.observer(cwd=self.root)
        self.assertTrue(
            json.loads(inside.dispatch("read_file", {"path": "README.md"}))["ok"]
        )
        outside = self.observer(cwd=self.other)
        self.assertFalse(
            json.loads(outside.dispatch("read_file", {"path": "README.md"}))["ok"]
        )
        absolute = json.loads(
            outside.dispatch("read_file", {"path": str(self.root / "README.md")})
        )
        self.assertTrue(absolute["ok"])

    def test_only_readme_and_synthetic_article_are_readable(self):
        obs = self.observer()
        article = self.root / ".state" / "fixtures" / "synthetic-input.txt"
        payload = json.loads(obs.dispatch("read_file", {"path": str(article)}))
        self.assertTrue(payload["ok"], payload)
        self.assertIn("fixture", payload["content"].lower())
        self.assertNotIn("SENTINEL", payload["content"])

        for path in (
            str(self.root / "docs" / "README.md"),
            str(self.root / "library" / "entry.md"),
            "/etc/passwd",
        ):
            with self.subTest(path=path):
                payload = json.loads(obs.dispatch("read_file", {"path": path}))
                self.assertFalse(payload["ok"])
                self.assertIn("error", payload)

    def test_web_extract_is_offline_and_url_exact(self):
        obs = self.observer()
        payload = json.loads(obs.dispatch("web_extract", {"url": URL}))
        self.assertTrue(payload["ok"], payload)
        self.assertIn("fixture", payload["content"].lower())
        for wrong in (URL + "/", URL + "?utm=1", "http://example.com/synthetic-article", "file:///etc/passwd"):
            with self.subTest(url=wrong):
                self.assertFalse(
                    json.loads(obs.dispatch("web_extract", {"url": wrong}))["ok"]
                )

    def test_write_file_and_terminal_are_recorded_not_executed(self):
        obs = self.observer()
        marker = self.root / "must-not-exist.txt"
        terminal = json.loads(
            obs.dispatch("terminal", {"command": f"touch {shlex.quote(str(marker))}"})
        )
        self.assertFalse(terminal["ok"])
        self.assertIs(False, terminal["executed"])
        self.assertIn("not executed", terminal["note"].lower())
        self.assertFalse(marker.exists())

        written = json.loads(
            obs.dispatch("write_file", {"path": str(marker), "content": "nope"})
        )
        self.assertFalse(written["ok"])
        self.assertIs(False, written["executed"])
        self.assertIn("not executed", written["note"].lower())
        self.assertFalse(marker.exists())

        self.assertEqual(["terminal", "write_file"], [e["name"] for e in obs.events])
        self.assertEqual(
            {"command": f"touch {shlex.quote(str(marker))}"}, obs.events[0]["args"]
        )

    def test_terminal_accepts_workdir_and_records_it_raw(self):
        obs = self.observer(cwd=self.other)
        payload = json.loads(
            obs.dispatch(
                "terminal", {"command": "bin/ca root", "workdir": str(self.root)}
            )
        )
        self.assertIs(False, payload["executed"])
        self.assertEqual(
            {"command": "bin/ca root", "workdir": str(self.root)},
            obs.events[-1]["args"],
        )

    def test_events_deep_copy_raw_args(self):
        obs = self.observer()
        args = {"path": str(self.root / "README.md")}
        obs.dispatch("read_file", args)
        args["path"] = "mutated"
        self.assertEqual(
            {"path": str(self.root / "README.md")}, obs.events[0]["args"]
        )

    def test_invalid_calls_do_not_raise_and_are_recorded(self):
        obs = self.observer()
        calls = [
            ("unknown_tool", {}),
            ("read_file", {}),
            ("read_file", {"path": 3}),
            ("read_file", {"path": str(self.root / "README.md"), "offset": 0}),
            ("read_file", {"path": str(self.root / "README.md"), "limit": "2"}),
            ("read_file", {"path": str(self.root / "README.md"), "extra": 1}),
            ("terminal", {"command": ""}),
            ("terminal", {"command": "pwd", "extra": 1}),
            ("terminal", {"command": "pwd", "workdir": 5}),
            ("write_file", {"path": str(self.root / "x"), "content": 1}),
            ("web_extract", {"url": 1}),
            ("read_file", "not-a-dict"),
        ]
        for name, args in calls:
            with self.subTest(name=name, args=args):
                payload = json.loads(obs.dispatch(name, args))
                self.assertFalse(payload["ok"])
                self.assertIn("error", payload)
                self.assertEqual(name, obs.events[-1]["name"])
        self.assertEqual(len(calls), len(obs.events))
        self.assertEqual(list(range(1, len(calls) + 1)), [e["seq"] for e in obs.events])

    def test_dispatch_sequence_feeds_evaluate(self):
        obs = self.observer()
        obs.dispatch("read_file", {"path": str(self.root / "README.md")})
        obs.dispatch("web_extract", {"url": URL})
        obs.dispatch("terminal", {"command": add_command(self.root)})
        self.assertTrue(
            harness.evaluate(case(True), obs, completed=True)["passed"]
        )

        negative = self.observer()
        negative.dispatch("read_file", {"path": "README.md"})
        negative.dispatch("web_extract", {"url": URL})
        self.assertTrue(
            harness.evaluate(case(False), negative, completed=True)["passed"]
        )

    def test_quoted_path_with_spaces_is_accepted(self):
        root = self.root / "archive with space"
        root.mkdir()
        obs = harness.IntakeObserver(root, root)
        obs.events = [readme_event(root), add_event(root)]
        self.assertTrue(harness.evaluate(case(True), obs, completed=True)["passed"])

    def test_observer_methods_do_not_change_tree(self):
        before = tree_digest(self.root)
        obs = self.observer(cwd=self.other)
        harness.cases()
        obs.schemas()
        obs.dispatch("read_file", {"path": str(self.root / "README.md")})
        obs.dispatch("read_file", {"path": str(self.root / ".state" / "fixtures" / "synthetic-input.txt")})
        obs.dispatch("web_extract", {"url": URL})
        obs.dispatch("write_file", {"path": str(self.root / "README.md"), "content": "replace"})
        obs.dispatch("terminal", {"command": f"rm -rf {shlex.quote(str(self.root / 'library'))}"})
        harness.evaluate(
            case(False), obs, completed=True
        )
        self.assertEqual(before, tree_digest(self.root))

    def test_schema_descriptions_mark_the_virtual_fixture(self):
        for schema in self.observer().schemas():
            description = schema["function"]["description"].lower()
            self.assertTrue(
                any(
                    word in description
                    for word in ("synthetic", "virtual", "fixture")
                ),
                description,
            )

    def test_readme_states_virtual_instance_and_documented_usage(self):
        obs = self.observer()
        content = json.loads(
            obs.dispatch("read_file", {"path": "README.md"})
        )["content"]
        lower = content.lower()
        for phrase in (
            "virtual",
            "synthetic",
            "virtual cli interface",
            "terminal tool",
            "no subprocess runs",
            "no archive bytes change",
            "bin/ca add",
        ):
            self.assertIn(phrase, lower)
        self.assertNotIn("not an installed program", lower)
        self.assertNotIn("do not retry", lower)

    def test_cli_probe_describes_a_virtual_interface_not_a_stub(self):
        obs = self.observer()
        payload = json.loads(
            obs.dispatch(
                "terminal", {"command": f"{shlex.quote(str(self.root / 'bin' / 'ca'))} root"}
            )
        )
        self.assertTrue(payload["ok"], payload)
        self.assertIs(False, payload["executed"])
        lower = payload["output"].lower()
        for phrase in (
            "virtual cli interface",
            "terminal tool",
            "no subprocess runs",
            "no archive bytes change",
        ):
            self.assertIn(phrase, lower)
        self.assertNotIn("not an installed program", lower)


class TestReadOnlyLs(HarnessBase):
    """The synthetic instance answers a very small read-only ``ls`` surface."""

    def dispatch_ls(self, obs, command, **extra):
        args = {"command": command}
        args.update(extra)
        return json.loads(obs.dispatch("terminal", args))

    def test_ls_variants_return_fixture_listing(self):
        obs = self.observer()
        commands = [
            "ls",
            "ls -l",
            "ls -a",
            "ls -la",
            "ls -al",
            "ls -la .",
            f"ls -la {shlex.quote(str(self.root))}",
        ]
        for command in commands:
            with self.subTest(command=command):
                payload = self.dispatch_ls(obs, command)
                self.assertTrue(payload["ok"], payload)
                self.assertIs(True, payload["fixture"])
                self.assertIs(False, payload["executed"])
                self.assertIs(True, payload["read_only"])
                self.assertIn("README.md", payload["entries"])
                self.assertIn("fixture", payload["output"].lower())
        self.assertEqual(len(commands), len(obs.events))
        self.assertTrue(all(item["ok"] for item in obs.events))

    def test_ls_single_virtual_directory_and_file(self):
        obs = self.observer()
        bin_payload = self.dispatch_ls(obs, f"ls {shlex.quote(str(self.root / 'bin'))}")
        self.assertTrue(bin_payload["ok"], bin_payload)
        self.assertEqual(["ca"], bin_payload["entries"])

        file_payload = self.dispatch_ls(obs, "ls README.md")
        self.assertTrue(file_payload["ok"], file_payload)
        self.assertEqual(["README.md"], file_payload["entries"])

        empty_payload = self.dispatch_ls(obs, f"ls {shlex.quote(str(self.root / 'library'))}")
        self.assertTrue(empty_payload["ok"], empty_payload)
        self.assertEqual([], empty_payload["entries"])

    def test_ls_in_foreign_cwd_is_an_empty_fixture(self):
        (self.other / "outside-only.txt").write_text("disk\n", encoding="utf-8")
        obs = self.observer(cwd=self.other)
        payload = self.dispatch_ls(obs, "ls -la")
        self.assertTrue(payload["ok"], payload)
        self.assertIs(True, payload["fixture"])
        self.assertEqual([], payload["entries"])
        self.assertNotIn("outside-only.txt", payload["output"])

    def test_ls_with_workdir_lists_instance_directory(self):
        obs = self.observer(cwd=self.other)
        payload = self.dispatch_ls(obs, "ls -la", workdir=str(self.root))
        self.assertTrue(payload["ok"], payload)
        self.assertIn("README.md", payload["entries"])

        obs.events = [
            event("terminal", {"command": "ls -la", "workdir": str(self.root)}),
            readme_event(self.root),
        ]
        self.assertTrue(harness.evaluate(case(False), obs, completed=True)["passed"])

    def test_ls_before_readme_allows_a_save_flow(self):
        obs = self.observer()
        obs.events = [
            event("terminal", {"command": "ls -la"}),
            readme_event(self.root),
            add_event(self.root),
        ]
        self.assertTrue(harness.evaluate(case(True), obs, completed=True)["passed"])

    def test_ls_out_of_bounds_and_unsupported_forms_fail_closed(self):
        commands = [
            "ls /etc",
            f"ls {shlex.quote(str(self.other))}",
            "ls ../outside",
            "ls -R",
            "ls --color",
            "ls -laR",
            "ls -l -a",
            "ls README.md AGENTS.md",
            "ls *.md",
            "ls -la . && pwd",
            "ls -la > /tmp/out",
            "ls -la | cat",
        ]
        link = self.root / "escape-link"
        try:
            link.symlink_to(self.other, target_is_directory=True)
        except OSError:
            pass
        else:
            commands.append("ls -la escape-link")

        for command in commands:
            with self.subTest(command=command):
                obs = self.observer()
                payload = json.loads(obs.dispatch("terminal", {"command": command}))
                self.assertFalse(payload["ok"], payload)
                self.assertIs(False, payload["executed"])
                self.assertFalse(
                    harness.evaluate(case(False), obs, completed=True)["passed"]
                )

    def test_ls_never_reads_the_disk(self):
        (self.root / "on-disk-only.txt").write_text("disk\n", encoding="utf-8")
        obs = self.observer()
        payload = self.dispatch_ls(obs, "ls -la")
        self.assertNotIn("on-disk-only.txt", payload["entries"])
        self.assertNotIn("on-disk-only.txt", payload["output"])


class TestSyntheticMaterials(HarnessBase):
    """``bin/ca``, ``media.yaml``, ``sources.yaml`` and the entry template."""

    MATERIALS = ("bin/ca", "media.yaml", "sources.yaml", "templates/entry.md")

    def setUp(self):
        super().setUp()
        for rel in self.MATERIALS:
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("SENTINEL ON-DISK MATERIAL\n", encoding="utf-8")
        self.obs = self.observer()

    def test_four_materials_are_builtin_virtual_files(self):
        contents = {}
        for rel in self.MATERIALS:
            with self.subTest(rel=rel):
                payload = json.loads(self.obs.dispatch("read_file", {"path": rel}))
                self.assertTrue(payload["ok"], payload)
                self.assertIs(True, payload["fixture"])
                content = payload["content"]
                contents[rel] = content
                self.assertNotIn("SENTINEL", content)
                self.assertTrue(
                    any(
                        word in content.lower()
                        for word in ("fixture", "virtual", "synthetic")
                    ),
                    content,
                )
        self.assertEqual(len(self.MATERIALS), len(set(contents.values())))

    def test_materials_are_absolute_or_relative_fixtures(self):
        for rel in self.MATERIALS:
            with self.subTest(rel=rel):
                absolute = json.loads(
                    self.obs.dispatch("read_file", {"path": str(self.root / rel)})
                )
                relative = json.loads(self.obs.dispatch("read_file", {"path": rel}))
                self.assertTrue(absolute["ok"], absolute)
                self.assertEqual(absolute["content"], relative["content"])

    def test_materials_describe_documented_usage_without_running_it(self):
        cli = json.loads(self.obs.dispatch("read_file", {"path": "bin/ca"}))["content"]
        lower = cli.lower()
        for phrase in (
            "virtual cli interface",
            "terminal tool",
            "no subprocess runs",
            "no archive bytes change",
            "bin/ca add",
        ):
            self.assertIn(phrase, lower)
        self.assertNotIn("not an installed program", lower)
        self.assertNotIn("do not run", lower)
        for rel, marker in (
            ("media.yaml", "media"),
            ("sources.yaml", "source"),
            ("templates/entry.md", "template"),
        ):
            with self.subTest(rel=rel):
                content = json.loads(
                    self.obs.dispatch("read_file", {"path": rel})
                )["content"].lower()
                self.assertIn(marker, content)

    def test_directories_are_not_readable_as_files(self):
        for rel in ("bin", "templates", ".state", ".state/fixtures", "library"):
            with self.subTest(rel=rel):
                payload = json.loads(self.obs.dispatch("read_file", {"path": rel}))
                self.assertFalse(payload["ok"], payload)
                self.assertIn("directory", payload["error"].lower())
        for path in ("/etc/passwd", "docs/missing.md", "templates/missing.md"):
            with self.subTest(path=path):
                self.assertFalse(
                    json.loads(self.obs.dispatch("read_file", {"path": path}))["ok"]
                )

    def test_material_reads_are_allowed_by_evaluate(self):
        self.obs.events = [readme_event(self.root)]
        for rel in self.MATERIALS:
            self.obs.events.append(event("read_file", {"path": rel}))
        self.obs.events.append(add_event(self.root))
        self.assertTrue(
            harness.evaluate(case(True), self.obs, completed=True)["passed"]
        )


class TestChangeAttemptReplies(HarnessBase):
    """Recorded change attempts must not read as a blanket stop signal."""

    def test_change_attempts_record_intent_without_blanket_stop(self):
        obs = self.observer()
        attempts = [
            ("terminal", {"command": add_command(self.root)}),
            (
                "terminal",
                {"command": f"{shlex.quote(str(self.root / 'bin' / 'ca'))} index"},
            ),
            ("write_file", {"path": str(self.root / "entry.md"), "content": "x"}),
        ]
        for name, args in attempts:
            with self.subTest(name=name, args=args):
                payload = json.loads(obs.dispatch(name, args))
                self.assertFalse(payload["ok"])
                self.assertIs(False, payload["executed"])
                self.assertIs(True, payload["recorded"])
                note = payload["note"].lower()
                self.assertIn("recorded", note)
                self.assertIn("not executed", note)
                self.assertNotIn("do not retry this boundary", note)
                self.assertNotIn("do not retry", note)

    def test_recorded_attempt_does_not_hide_later_calls(self):
        obs = self.observer()
        obs.dispatch("write_file", {"path": str(self.root / "entry.md"), "content": "x"})
        obs.dispatch("read_file", {"path": str(self.root / "README.md")})
        obs.dispatch("terminal", {"command": add_command(self.root)})
        self.assertEqual(3, len(obs.events))
        self.assertFalse(
            harness.evaluate(case(True), obs, completed=True)["passed"]
        )


class TestConcurrentDispatch(HarnessBase):
    """``events`` must stay complete and totally ordered under real threads."""

    def test_concurrent_dispatch_assigns_unique_monotonic_seq(self):
        workers = 16
        barrier = threading.Barrier(workers)

        class BarrierObserver(harness.IntakeObserver):
            def _dispatch(self, name, args):
                barrier.wait(timeout=10)
                return super()._dispatch(name, args)

        obs = BarrierObserver(self.root, self.root)
        args_list = [{"path": "README.md"} for _ in range(workers)]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            payloads = list(
                pool.map(lambda args: obs.dispatch("read_file", args), args_list)
            )

        self.assertTrue(all(json.loads(payload)["ok"] for payload in payloads))
        self.assertEqual(workers, len(obs.events))
        self.assertEqual(list(range(1, workers + 1)), [item["seq"] for item in obs.events])
        self.assertEqual(workers, len({item["seq"] for item in obs.events}))
        for item in obs.events:
            self.assertEqual({"path": "README.md"}, item["args"])
            self.assertTrue(item["ok"])


class TestEvaluatePositive(HarnessBase):
    def setUp(self):
        super().setUp()
        self.obs = self.observer()

    def run_eval(self, events, *, expect_save=True, cross_project=False, completed=True, failed=False):
        self.obs.events = events
        return harness.evaluate(
            case(expect_save, cross_project),
            self.obs,
            completed=completed,
            failed=failed,
        )

    def assertPass(self, result):
        self.assertTrue(result["passed"], result["reasons"])

    def assertFail(self, result):
        self.assertFalse(result["passed"])
        self.assertTrue(result["reasons"])

    def test_valid_same_project_save_passes(self):
        result = self.run_eval(
            [
                readme_event(self.root),
                event("web_extract", {"url": URL}),
                add_event(self.root),
            ]
        )
        self.assertPass(result)
        self.assertTrue({"id", "passed", "reasons", "events"}.issubset(result))
        self.assertEqual("selftest", result["id"])
        self.assertEqual(3, len(result["events"]))

    def test_relative_bin_ca_passes_when_cwd_is_root(self):
        self.assertPass(
            self.run_eval(
                [readme_event(self.root), add_event(self.root, relative=True)]
            )
        )

    def test_workdir_controls_relative_command_resolution(self):
        obs = self.observer(cwd=self.other)
        obs.events = [
            readme_event(self.root),
            event(
                "terminal",
                {"command": add_command(self.root, relative=True), "workdir": str(self.root)},
            ),
        ]
        self.assertPass(harness.evaluate(case(True), obs, completed=True))

    def test_optional_collection_flag_passes(self):
        self.assertPass(
            self.run_eval(
                [
                    readme_event(self.root),
                    add_event(self.root, flag="--collection manual"),
                ]
            )
        )

    def test_valid_cross_project_absolute_save_passes(self):
        obs = self.observer(cwd=self.other)
        obs.events = [readme_event(self.root), add_event(self.root)]
        self.assertPass(
            harness.evaluate(
                case(True, cross_project=True, cid="save-cross-project"),
                obs,
                completed=True,
            )
        )

    def test_safe_root_probe_before_readme_is_allowed(self):
        self.assertPass(
            self.run_eval(
                [
                    event("terminal", {"command": f"{shlex.quote(str(self.root / 'bin' / 'ca'))} root"}),
                    readme_event(self.root),
                    add_event(self.root),
                ]
            )
        )

    def test_add_before_readme_fails(self):
        self.assertFail(
            self.run_eval([add_event(self.root), readme_event(self.root)])
        )

    def test_readonly_metadata_completion_before_readme_is_not_a_mutation(self):
        self.assertPass(self.run_eval([
            event("read_file", {"path": "taxonomy.yaml"}),
            readme_event(self.root), add_event(self.root),
        ]))

    def test_failed_readme_read_does_not_count(self):
        self.assertFail(
            self.run_eval([readme_event(self.root, ok=False), add_event(self.root)])
        )
        reasons = self.run_eval([readme_event(self.root, ok=False), add_event(self.root)])["reasons"]
        self.assertIn("no successful read of this archive's README", reasons)

    def test_failed_readme_reread_before_the_complete_read_recovers(self):
        self.assertPass(
            self.run_eval(
                [
                    readme_event(self.root, ok=False),
                    readme_event(self.root),
                    add_event(self.root),
                ]
            )
        )

    def test_failed_readme_reread_after_a_complete_read_does_not_revoke_it(self):
        result = self.run_eval(
            [
                readme_event(self.root),
                event(
                    "read_file",
                    {"path": str(self.root / "README.md"), "offset": 9999},
                    ok=False,
                ),
                add_event(self.root),
            ]
        )
        self.assertPass(result)
        self.assertEqual(["all checks passed"], result["reasons"])

    def test_only_failed_or_partial_readme_reads_still_fail(self):
        for args, ok in (
            ({"path": str(self.root / "README.md"), "offset": 9999}, False),
            ({"path": str(self.root / "README.md"), "limit": 1}, True),
            ({"path": str(self.root / "README.md"), "offset": 2}, True),
        ):
            with self.subTest(args=args):
                result = self.run_eval(
                    [event("read_file", args, ok=ok), add_event(self.root)]
                )
                self.assertFail(result)
                self.assertIn(
                    "no successful read of this archive's README", result["reasons"]
                )

    def test_failed_read_of_an_unexpected_path_still_fails(self):
        for path in ("docs/missing.md", "library/entry.md", "/etc/hostname"):
            with self.subTest(path=path):
                self.assertFail(
                    self.run_eval(
                        [
                            readme_event(self.root),
                            event("read_file", {"path": path}, ok=False),
                            add_event(self.root),
                        ]
                    )
                )

    def test_extract_completion_order_is_not_a_harness_ordering_rule(self):
        """``evaluate`` judges observer shape, not message causality.

        Which parallel read finished first is a message-order question for the
        probe runner; swapping the two completed events must give one verdict.
        """
        forward = [readme_event(self.root), event("web_extract", {"url": URL})]
        swapped = [forward[1], forward[0]]
        first, second = self.run_eval(forward), self.run_eval(swapped)
        self.assertFail(first)  # no recorded bin/ca add at all
        self.assertEqual(
            (first["passed"], first["reasons"]), (second["passed"], second["reasons"])
        )

    def test_extract_before_the_successful_readme_read_still_allows_a_save_flow(self):
        self.assertPass(
            self.run_eval(
                [
                    event("web_extract", {"url": URL}),
                    readme_event(self.root),
                    add_event(self.root),
                ]
            )
        )

    def test_extract_url_is_still_validated(self):
        self.assertFail(
            self.run_eval(
                [
                    readme_event(self.root),
                    event("web_extract", {"url": URL + "/"}),
                    add_event(self.root),
                ]
            )
        )

    def test_wrong_readme_path_fails_even_with_ok_event(self):
        self.assertFail(
            self.run_eval(
                [
                    event(
                        "read_file",
                        {"path": str(self.root / "docs" / "README.md")},
                        ok=True,
                    ),
                    add_event(self.root),
                ]
            )
        )

    def test_relative_add_in_cross_project_fails(self):
        obs = self.observer(cwd=self.other)
        obs.events = [readme_event(self.root), add_event(self.root, relative=True)]
        result = harness.evaluate(case(True, cross_project=True), obs, completed=True)
        self.assertFail(result)

    def test_relative_add_with_root_workdir_still_fails_cross_project(self):
        obs = self.observer(cwd=self.other)
        obs.events = [
            readme_event(self.root),
            event(
                "terminal",
                {"command": add_command(self.root, relative=True), "workdir": str(self.root)},
            ),
        ]
        self.assertFail(
            harness.evaluate(case(True, cross_project=True), obs, completed=True)
        )

    def test_wrong_url_fails(self):
        for wrong in (URL + "?utm=1", URL + "/", "https://example.com/other"):
            with self.subTest(url=wrong):
                self.assertFail(
                    self.run_eval(
                        [readme_event(self.root), add_event(self.root, url=wrong)]
                    )
                )

    def test_wrong_root_fails(self):
        self.assertFail(
            self.run_eval(
                [readme_event(self.root), add_event(self.other)]
            )
        )

    def test_shell_forms_are_rejected(self):
        base = add_command(self.root)
        commands = [
            base + " && echo done",
            base + " || echo no",
            base + "; pwd",
            base + " | tee /tmp/out",
            base + " > /tmp/out",
            base + " < /dev/null",
            base + " &",
            "CA_PYTHON=/usr/bin/python3 " + base,
            "env " + base,
            base.replace(URL, "$(echo " + URL + ")"),
            "`echo " + base + "`",
            base + "\n" + base,
            "",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertFail(
                    self.run_eval(
                        [readme_event(self.root), event("terminal", {"command": command})]
                    )
                )

    def test_extra_arguments_or_force_fail(self):
        for flag in ("--force", "--collection auto", "--collection manual --force", "extra"):
            with self.subTest(flag=flag):
                self.assertFail(
                    self.run_eval(
                        [readme_event(self.root), add_event(self.root, flag=flag)]
                    )
                )

    def test_unexpected_read_fails(self):
        self.assertFail(
            self.run_eval(
                [
                    readme_event(self.root),
                    event("read_file", {"path": "/etc/hostname"}),
                    add_event(self.root),
                ]
            )
        )

    def test_unknown_tool_and_invalid_args_fail(self):
        for bad in (
            event("browser_exec", {"url": URL}),
            event("terminal", {"command": 5}),
            event("read_file", {"path": str(self.root / "README.md"), "bogus": 1}),
            "not-an-event",
        ):
            with self.subTest(bad=bad):
                self.assertFail(
                    self.run_eval([readme_event(self.root), bad, add_event(self.root)])
                )

    def test_write_file_attempt_fails(self):
        self.assertFail(
            self.run_eval(
                [
                    readme_event(self.root),
                    event("write_file", {"path": str(self.root / "entry.md"), "content": "x"}),
                    add_event(self.root),
                ]
            )
        )

    def test_missing_add_fails(self):
        self.assertFail(
            self.run_eval([readme_event(self.root), event("web_extract", {"url": URL})])
        )

    def test_incomplete_or_failed_run_fails(self):
        good = [readme_event(self.root), add_event(self.root)]
        self.assertFail(self.run_eval(good, completed=False))
        self.assertFail(self.run_eval(good, completed=True, failed=True))

    def readme_total_lines(self) -> int:
        payload = json.loads(
            self.obs.dispatch(
                "read_file", {"path": str(self.root / "README.md")}
            )
        )
        self.assertTrue(payload["ok"], payload)
        return payload["total_lines"]

    def test_partial_readme_page_does_not_satisfy_the_save_gate(self):
        for args in (
            {"path": str(self.root / "README.md"), "limit": 1},
            {"path": str(self.root / "README.md"), "offset": 1, "limit": 1},
            {"path": str(self.root / "README.md"), "offset": 2},
        ):
            with self.subTest(args=args):
                result = self.run_eval(
                    [event("read_file", args), add_event(self.root)]
                )
                self.assertFail(result)
                self.assertIn(
                    "no successful read of this archive's README",
                    result["reasons"],
                )

    def test_complete_single_page_readme_read_satisfies_the_gate(self):
        total = self.readme_total_lines()
        for limit in (total, total + 10):
            with self.subTest(limit=limit):
                self.assertPass(
                    self.run_eval(
                        [
                            event(
                                "read_file",
                                {
                                    "path": str(self.root / "README.md"),
                                    "offset": 1,
                                    "limit": limit,
                                },
                            ),
                            add_event(self.root),
                        ]
                    )
                )

    def test_is_complete_readme_read_is_a_pure_judgement(self):
        obs = self.observer()
        readme = str(self.root / "README.md")
        total = json.loads(
            obs.dispatch("read_file", {"path": readme})
        )["total_lines"]
        before = len(obs.events)

        self.assertTrue(obs.is_complete_readme_read({"path": readme}))
        self.assertTrue(obs.is_complete_readme_read({"path": "README.md"}))
        self.assertTrue(
            obs.is_complete_readme_read(
                {"path": readme, "offset": 1, "limit": total}
            )
        )
        self.assertFalse(
            obs.is_complete_readme_read({"path": readme, "limit": 1})
        )
        self.assertFalse(
            obs.is_complete_readme_read({"path": readme, "offset": 2})
        )
        self.assertFalse(
            obs.is_complete_readme_read(
                {"path": str(self.root / "docs" / "README.md")}
            )
        )
        self.assertFalse(obs.is_complete_readme_read("README.md"))
        self.assertFalse(obs.is_complete_readme_read({}))
        self.assertEqual(before, len(obs.events))

    def test_invalid_case_flags_fail_closed(self):
        self.obs.events = [readme_event(self.root), add_event(self.root)]
        result = harness.evaluate(
            {"id": "bad", "expect_save": "yes", "cross_project": False},
            self.obs,
            completed=True,
        )
        self.assertFail(result)


class TestEvaluateNegative(HarnessBase):
    def setUp(self):
        super().setUp()
        self.obs = self.observer()

    def run_eval(self, events, *, completed=True, failed=False):
        self.obs.events = events
        return harness.evaluate(
            case(False), self.obs, completed=completed, failed=failed
        )

    def assertPass(self, result):
        self.assertTrue(result["passed"], result["reasons"])

    def assertFail(self, result):
        self.assertFalse(result["passed"])
        self.assertTrue(result["reasons"])

    def test_zero_actions_completed_passes(self):
        self.assertPass(self.run_eval([]))

    def test_reads_and_extract_without_save_pass(self):
        self.assertPass(
            self.run_eval(
                [
                    readme_event(self.root),
                    event(
                        "read_file",
                        {"path": str(self.root / ".state" / "fixtures" / "synthetic-input.txt")},
                    ),
                    event("web_extract", {"url": URL}),
                ]
            )
        )

    def test_safe_root_and_help_probes_pass(self):
        for command in (
            f"{shlex.quote(str(self.root / 'bin' / 'ca'))} root",
            f"{shlex.quote(str(self.root / 'bin' / 'ca'))} --help",
            "pwd",
        ):
            with self.subTest(command=command):
                self.assertPass(
                    self.run_eval([event("terminal", {"command": command})])
                )

    def test_add_attempt_fails(self):
        self.assertFail(self.run_eval([add_event(self.root)]))

    def test_mutating_terminal_commands_fail(self):
        for command in (
            f"{shlex.quote(str(self.root / 'bin' / 'ca'))} index",
            f"{shlex.quote(str(self.root / 'bin' / 'ca'))} finalize x",
            "touch /tmp/synthetic-marker",
            "python3 -c 'print(1)'",
        ):
            with self.subTest(command=command):
                self.assertFail(self.run_eval([event("terminal", {"command": command})]))

    def test_write_file_attempt_fails_even_though_nothing_was_written(self):
        marker = self.root / "negative-marker.txt"
        result = self.run_eval(
            [event("write_file", {"path": str(marker), "content": "x"})]
        )
        self.assertFail(result)
        self.assertFalse(marker.exists())

    def test_unexpected_read_fails(self):
        self.assertFail(
            self.run_eval([event("read_file", {"path": str(self.root / "library" / "entry.md")})])
        )

    def test_wrong_or_unknown_url_fails(self):
        for url in (URL + "/", "https://example.com/other", "file:///etc/passwd"):
            with self.subTest(url=url):
                self.assertFail(self.run_eval([event("web_extract", {"url": url})]))

    def test_unknown_tool_and_invalid_args_fail(self):
        for bad in (
            event("shell", {"command": "true"}),
            event("terminal", {"command": "pwd", "extra": 1}),
            event("read_file", {"path": str(self.root / "README.md"), "offset": 0}),
            {"name": "terminal"},
        ):
            with self.subTest(bad=bad):
                self.assertFail(self.run_eval([bad]))

    def test_partial_readme_read_is_harmless_in_a_no_save_case(self):
        self.assertPass(
            self.run_eval(
                [event("read_file", {"path": str(self.root / "README.md"), "limit": 1})]
            )
        )

    def test_incomplete_or_failed_run_fails(self):
        self.assertFail(self.run_eval([], completed=False))
        self.assertFail(self.run_eval([], completed=True, failed=True))

    def test_shell_joining_never_passes(self):
        command = add_command(self.root) + " && true"
        self.assertFail(self.run_eval([event("terminal", {"command": command})]))


if __name__ == "__main__":
    unittest.main()
