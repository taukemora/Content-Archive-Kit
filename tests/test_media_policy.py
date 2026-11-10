#!/usr/bin/env python3
"""图片预算配置：默认值、media.yaml、CLI 覆盖优先级与严格校验。"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))

from media_policy import (  # noqa: E402
    DEFAULT_MAX_DOWNLOAD_SECONDS,
    DEFAULT_MAX_IMAGES,
    DEFAULT_MAX_SINGLE_IMAGE_MIB,
    DEFAULT_MAX_TOTAL_IMAGE_MIB,
    MediaBudget,
    MediaConfigError,
    load_media_config,
    resolve_budget,
)

MIB = 1024 * 1024


class MediaPolicyTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(dir=str(_support.SCRATCH), prefix="media_policy_"))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.base, ignore_errors=True)

    def write_yaml(self, text: str) -> Path:
        (self.base / "media.yaml").write_text(text, encoding="utf-8")
        return self.base

    def test_defaults_without_media_yaml(self):
        budget = load_media_config(self.base)
        self.assertEqual(budget.max_images, DEFAULT_MAX_IMAGES)
        self.assertEqual(budget.max_single_image_bytes, DEFAULT_MAX_SINGLE_IMAGE_MIB * MIB)
        self.assertEqual(budget.max_total_image_bytes, DEFAULT_MAX_TOTAL_IMAGE_MIB * MIB)
        self.assertEqual(budget.max_download_seconds, DEFAULT_MAX_DOWNLOAD_SECONDS)
        self.assertEqual(budget.max_images, 100)
        self.assertEqual(budget.max_single_image_mib, 10)
        self.assertEqual(budget.max_total_image_mib, 50)
        self.assertEqual(budget.max_download_seconds, 180)

    def test_project_media_yaml_is_the_approved_budget(self):
        budget = load_media_config(ROOT)
        self.assertEqual(budget.max_images, 100)
        self.assertEqual(budget.max_single_image_bytes, 10 * MIB)
        self.assertEqual(budget.max_total_image_bytes, 50 * MIB)
        self.assertEqual(budget.max_download_seconds, 180)

    def test_yaml_values_override_defaults(self):
        self.write_yaml(
            "max_images: 42\n"
            "max_single_image_mib: 4.5\n"
            "max_total_image_mib: 33\n"
            "max_download_seconds: 12.5\n"
        )
        budget = load_media_config(self.base)
        self.assertEqual(budget.max_images, 42)
        self.assertEqual(budget.max_single_image_bytes, int(4.5 * MIB))
        self.assertEqual(budget.max_total_image_bytes, 33 * MIB)
        self.assertEqual(budget.max_download_seconds, 12.5)

    def test_cli_overrides_yaml_and_defaults(self):
        self.write_yaml(
            "max_images: 42\n"
            "max_single_image_mib: 4\n"
            "max_total_image_mib: 30\n"
            "max_download_seconds: 12\n"
        )
        budget = resolve_budget(
            {
                "max_images": 7,
                "max_single_image_mib": 2.5,
                "max_total_image_mib": None,
                "max_download_seconds": 3.5,
            },
            root=self.base,
        )
        self.assertEqual(budget.max_images, 7)
        self.assertEqual(budget.max_single_image_bytes, int(2.5 * MIB))
        self.assertEqual(budget.max_total_image_bytes, 30 * MIB)
        self.assertEqual(budget.max_download_seconds, 3.5)

    def test_cli_only_uses_defaults_for_unset_fields(self):
        budget = resolve_budget({"max_images": 3}, root=self.base)
        self.assertEqual(budget.max_images, 3)
        self.assertEqual(budget.max_single_image_mib, 10)
        self.assertEqual(budget.max_total_image_mib, 50)
        self.assertEqual(budget.max_download_seconds, 180)

    def test_sixteen_mib_single_image_is_allowed(self):
        budget = resolve_budget({"max_single_image_mib": 16}, root=self.base)
        self.assertEqual(budget.max_single_image_bytes, 16 * MIB)

    def test_rejects_out_of_range_or_wrong_type_values(self):
        bad = [
            {"max_images": 0},
            {"max_images": -1},
            {"max_images": 1.5},
            {"max_images": True},
            {"max_images": "10"},
            {"max_images": 1001},
            {"max_single_image_mib": 0},
            {"max_single_image_mib": 16.01},
            {"max_single_image_mib": float("nan")},
            {"max_single_image_mib": float("inf")},
            {"max_total_image_mib": -3},
            {"max_total_image_mib": 501},
            {"max_download_seconds": 0},
            {"max_download_seconds": 3601},
            {"max_download_seconds": "abc"},
        ]
        for override in bad:
            with self.subTest(override=override):
                with self.assertRaises(MediaConfigError):
                    resolve_budget(override, root=self.base)

    def test_rejects_invalid_yaml_types_and_unknown_keys(self):
        cases = [
            "max_images: nope\n",
            "max_images: 0\n",
            "max_single_image_mib: 17\n",
            "max_total_image_mib: -1\n",
            "max_download_seconds: 0\n",
            "max_imagess: 100\n",
            "max_images: 100\nmax_images: 200\n",
            "- just\n- a\n- list\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.write_yaml(text)
                with self.assertRaises(MediaConfigError):
                    load_media_config(self.base)

    def test_budget_serializes_for_output(self):
        budget = MediaBudget(
            max_images=100,
            max_single_image_bytes=10 * MIB,
            max_total_image_bytes=50 * MIB,
            max_download_seconds=180,
        )
        data = budget.as_dict()
        self.assertEqual(data["max_images"], 100)
        self.assertEqual(data["max_single_image_mib"], 10)
        self.assertEqual(data["max_total_image_mib"], 50)
        self.assertEqual(data["max_download_seconds"], 180)

    def test_cli_rejects_invalid_budget_before_network(self):
        # 预算校验先于抓取：非法 CLI 值必须直接失败，不能发起请求。
        proc = subprocess.run(
            [_support.PYTHON, str(ROOT / "scripts" / "archive_url.py"),
             "https://example.com/a", "--max-images", "0"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("max_images", proc.stdout)
        self.assertIn("error", proc.stdout)


if __name__ == "__main__":
    unittest.main()
