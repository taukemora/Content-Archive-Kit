"""Publish integration must never roll back a successful local archive."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))
import rebuild_index
import daily_gate


class TestIndexPublishHook(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(_support.SCRATCH), prefix="ca-publish-hooks-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "library").mkdir()

    def test_publish_runs_after_all_local_outputs_exist(self):
        def publish(root):
            self.assertEqual(root, self.root)
            for name in ("index.csv", "index.md", "index.html"):
                self.assertTrue((root / name).is_file(), name)
            return {"status": "skipped", "reason": "disabled"}

        call = Mock(side_effect=publish)
        module = types.SimpleNamespace(maybe_publish=call)
        output = io.StringIO()
        with patch.object(rebuild_index, "ROOT", self.root), \
             patch.object(rebuild_index, "taxonomy", return_value={"categories": []}), \
             patch.object(rebuild_index, "build_html", return_value="<!doctype html><title>fixture</title>"), \
             patch.dict(sys.modules, {"publish_site": module}), \
             contextlib.redirect_stdout(output):
            self.assertEqual(rebuild_index.main(), 0)
        call.assert_called_once_with(self.root)
        self.assertEqual(json.loads(output.getvalue())["publication"]["status"], "skipped")

    def test_publisher_error_does_not_fail_local_index_or_echo_exception(self):
        module = types.SimpleNamespace(maybe_publish=Mock(side_effect=RuntimeError("sensitive-exception-marker")))
        output = io.StringIO()
        errors = io.StringIO()
        with patch.object(rebuild_index, "ROOT", self.root), \
             patch.object(rebuild_index, "taxonomy", return_value={"categories": []}), \
             patch.object(rebuild_index, "build_html", return_value="<html>local snapshot</html>"), \
             patch.dict(sys.modules, {"publish_site": module}), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                result = rebuild_index.main()
            except Exception as exc:
                self.fail(f"Publisher must not interrupt local indexing: {type(exc).__name__}")
        self.assertEqual(result, 0)
        self.assertEqual((self.root / "index.html").read_text(), "<html>local snapshot</html>")
        self.assertEqual(json.loads(output.getvalue())["publication"]["status"], "failed")
        self.assertNotIn("sensitive-exception-marker", output.getvalue() + errors.getvalue())


class TestDailyPublishHook(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(_support.SCRATCH), prefix="ca-daily-publish-hooks-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runs = self.root / ".state" / "runs"
        self.runs.mkdir(parents=True)
        stamp = daily_gate.iso()
        (self.runs / "daily-gate.json").write_text(json.dumps({
            "status": "pending", "started_at": stamp, "fetch_generated_at": stamp,
        }))
        (self.runs / "latest-fetch.json").write_text(json.dumps({
            "generated_at": stamp, "candidates": [],
        }))

    def test_daily_publishes_once_after_audit_and_digest(self):
        events = []

        def run(command, **kwargs):
            name = Path(command[1]).name
            events.append(name)
            if name == "rebuild_index.py":
                self.assertEqual(kwargs.get("env", {}).get("CA_SKIP_AUTO_PUBLISH"), "1")
            return subprocess.CompletedProcess(command, 0, "fixture digest", "")

        def publish(root):
            self.assertEqual(root, self.root)
            events.append("publish")
            return {"status": "pushed_pending", "version": "fixture-version"}

        call = Mock(side_effect=publish)
        with patch.object(daily_gate, "ROOT", self.root), \
             patch.object(daily_gate.subprocess, "run", side_effect=run), \
             patch("reconcile_seen.reconcile_seen", return_value={"fixed": 0}), \
             patch.object(daily_gate, "take_seen_snapshot", return_value={}), \
             patch.object(daily_gate, "cleanup_runs", return_value=0), \
             patch.dict(sys.modules, {"publish_site": types.SimpleNamespace(maybe_publish=call)}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(daily_gate.run_finish(), 0)
        self.assertEqual(events, ["rebuild_index.py", "audit.py", "build_digest.py", "publish"])
        call.assert_called_once_with(self.root)
        gate = json.loads((self.runs / "daily-gate.json").read_text())
        self.assertEqual(gate["archive_status"], "ok")
        self.assertEqual(gate["publication"]["status"], "pushed_pending")

    def test_daily_publish_exception_keeps_durable_archive_success(self):
        durable_statuses = []
        def publish(root):
            saved = json.loads((self.runs / "daily-gate.json").read_text())
            durable_statuses.append(saved.get("archive_status"))
            raise RuntimeError("sensitive-publisher-exception")

        output, errors = io.StringIO(), io.StringIO()
        with patch.object(daily_gate, "ROOT", self.root), \
             patch.object(daily_gate.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "digest", "")), \
             patch("reconcile_seen.reconcile_seen", return_value={"fixed": 0}), \
             patch.object(daily_gate, "take_seen_snapshot", return_value={}), \
             patch.object(daily_gate, "cleanup_runs", return_value=0), \
             patch.dict(sys.modules, {"publish_site": types.SimpleNamespace(maybe_publish=publish)}), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                result = daily_gate.run_finish()
            except Exception as exc:
                self.fail(f"Publication interrupted archived batch: {type(exc).__name__}")
        self.assertEqual(result, 0)
        self.assertEqual(durable_statuses, ["ok"])
        gate = json.loads((self.runs / "daily-gate.json").read_text())
        self.assertEqual(gate["archive_status"], "ok")
        self.assertEqual(gate["publication"]["status"], "failed")
        self.assertNotIn("sensitive-publisher-exception", output.getvalue() + errors.getvalue())


class TestPublishCommand(unittest.TestCase):
    def test_wrapper_routes_publish_without_using_the_real_archive(self):
        with tempfile.TemporaryDirectory(dir=str(_support.SCRATCH), prefix="ca-publish-cli-") as tmp:
            root = Path(tmp)
            for name in ("bin", "scripts", ".venv/bin"):
                (root / name).mkdir(parents=True)
            shutil.copy2(ROOT / "bin/ca", root / "bin/ca")
            (root / ".venv/bin/python").symlink_to(sys.executable)
            (root / "scripts/publish_site.py").write_text("import json,sys; print(json.dumps(sys.argv[1:]))\n")
            result = subprocess.run([str(root / "bin/ca"), "publish", "status"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), ["status"])


if __name__ == "__main__":
    unittest.main()