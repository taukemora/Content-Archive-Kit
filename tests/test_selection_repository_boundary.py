"""Public repository boundary for the optional private selection policy."""
from __future__ import annotations

import subprocess
import unittest

from test_repository_checks import MANIFEST, ROOT, RepositoryTreeCase


class SelectionRepositoryBoundaryTests(RepositoryTreeCase):
    def test_live_root_policy_is_rejected_even_if_in_manifest(self):
        self.write("selection.yaml", "schema: 1\nenabled: true\n# synthetic private policy\n")
        self.write_manifest()
        result = self.run_check()
        self.assertEqual(result.returncode, 1, self.output(result))
        self.assertIn("private", self.output(result).lower())
        self.assertIn("selection.yaml", self.output(result))

    def test_manifest_update_cannot_bless_live_root_policy(self):
        self.write("README.md", "# Synthetic tree\n")
        self.write_manifest()
        before = (self.root / MANIFEST).read_bytes()
        self.write("selection.yaml", "schema: 1\nenabled: false\n")
        result = self.run_check("--update-manifest")
        self.assertEqual(result.returncode, 1, self.output(result))
        self.assertEqual((self.root / MANIFEST).read_bytes(), before)

    def test_example_policy_is_not_treated_as_private_root_config(self):
        self.write("examples/selection.yaml", "schema: 1\nenabled: false\n")
        self.write_manifest()
        result = self.run_check()
        self.assertEqual(result.returncode, 0, self.output(result))

    def test_gitignore_covers_live_policy_but_not_shipped_example(self):
        # Works in exported artifacts too; all Git state lives in the fixture.
        self.write(".gitignore", (ROOT / ".gitignore").read_text(encoding="utf-8"))
        self.git("init", "-q")
        for path, expected in (("selection.yaml", 0), ("examples/selection.yaml", 1)):
            result = subprocess.run(
                ["git", "-C", str(self.root), "check-ignore", "--no-index", "-q", path],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, expected, path + result.stderr)


class SelectionExampleTests(RepositoryTreeCase):
    def test_enabled_selection_example_is_rejected(self):
        self.copy_examples()
        self.replace_in("examples/selection.yaml", "enabled: false", "enabled: true")
        self.write_manifest()
        self.assert_finding(self.run_check(), "example selection.yaml must remain disabled")

    def test_disabled_but_invalid_selection_example_is_rejected(self):
        self.copy_examples()
        self.replace_in("examples/selection.yaml", "judgments: 20", "judgments: true")
        self.write_manifest()
        self.assert_finding(self.run_check(), "example selection.yaml invalid:")

    def test_selection_loader_symlink_is_never_executed(self):
        self.copy_examples()
        self.write_manifest()
        marker = self.symlink_loader_probe("scripts/subscription_selection.py")
        self.assert_finding(self.run_check(), "symbolic link not allowed:")
        self.assertFalse(marker.exists())

    def test_manifest_update_refuses_enabled_selection_example(self):
        self.copy_examples()
        self.write_manifest()
        before = (self.root / MANIFEST).read_bytes()
        self.replace_in("examples/selection.yaml", "enabled: false", "enabled: true")
        self.assert_finding(self.run_check("--update-manifest"),
                            "example selection.yaml must remain disabled")
        self.assertEqual((self.root / MANIFEST).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
