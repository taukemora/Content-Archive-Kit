"""Exercise the public CLI's read-only backfill route in synthetic trees."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

import _support


class FormatBackfillCLITest(unittest.TestCase):
    def setUp(self):
        self.work = _support.scratch_dir("backfill-cli-")
        self.toolkit = self.work / "toolkit with spaces"
        shutil.copytree(
            _support.ROOT / "scripts", self.toolkit / "scripts",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        (self.toolkit / "bin").mkdir()
        shutil.copy2(_support.ROOT / "bin" / "ca", self.toolkit / "bin" / "ca")
        self.source = self.work / "source with spaces"
        self.relative = Path("library/tech/synthetic/entry.md")
        entry = self.source / self.relative
        entry.parent.mkdir(parents=True)
        entry.write_text(
            "---\nid: synthetic\ntitle: 合成标题\nsource_type: wechat\n"
            "url: https://mp.weixin.qq.com/s/synthetic-cli\nstatus: archived\n"
            "raw_file: raw.html.gz\ncontent_hash: sha256:synthetic-old\n---\n"
            "# 合成标题\n\n## 摘要\n\n摘要保留。\n\n## 全文\n\n重点内容。\n",
            encoding="utf-8",
        )
        (entry.parent / "raw.html.gz").write_bytes(gzip.compress(
            '<div id="js_content"><p><span style="font-weight:700">重点</span>内容。</p></div>'.encode()
        ))
        (entry.parent / "images").mkdir()
        (entry.parent / "images" / "synthetic.bin").write_bytes(b"synthetic asset")

    def tearDown(self):
        shutil.rmtree(self.work)

    def run_cli(self, *args):
        env = dict(os.environ, CA_PYTHON=_support.PYTHON, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONPATH", None)
        return subprocess.run(
            [str(self.toolkit / "bin" / "ca"), "format-backfill", *map(str, args)],
            cwd=self.work, env=env, text=True, capture_output=True, timeout=30,
        )

    def source_hashes(self):
        return {
            p.relative_to(self.source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.source.rglob("*") if p.is_file()
        }

    def test_real_plan_prepare_routes_arguments_and_never_writes_source(self):
        before = self.source_hashes()
        plan_path = self.work / "review plan.json"
        planned = self.run_cli("plan", "--source", self.source, "--out", plan_path)
        self.assertEqual(planned.returncode, 0, planned.stdout + planned.stderr)
        self.assertEqual(json.loads(planned.stdout)["counts"]["changed"], 1)
        destination = self.work / "candidate with spaces"
        prepared = self.run_cli(
            "prepare", "--source", self.source, "--plan", plan_path, "--dest", destination,
        )
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        self.assertEqual(json.loads(prepared.stdout)["written"], 1)
        self.assertIn("**重点**内容。", (destination / self.relative).read_text(encoding="utf-8"))
        self.assertEqual(self.source_hashes(), before)
        for name in ("raw.html.gz", "images/synthetic.bin"):
            self.assertEqual(
                (destination / self.relative.parent / name).read_bytes(),
                (self.source / self.relative.parent / name).read_bytes(),
            )
        self.assertFalse((destination / ".state").exists())
        self.assertFalse((destination / "index.html").exists())


if __name__ == "__main__":
    unittest.main()
