"""Tests for ``tools/check_repository.py`` and the shipped example configs.

Every repository tree is synthetic and lives under :data:`_support.SCRATCH`.
No private archive, article, credential, recipient, or machine-local path is
read; fixtures are invented names such as ``synthetic.txt``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
CHECKER = ROOT / "tools" / "check_repository.py"
PYTHON = _support.PYTHON
MANIFEST = "docs/public-tree-sha256.json"
PEM_MARKER = "-----BEGIN " + "PRIVATE KEY-----"


def file_digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


class RepositoryTreeCase(unittest.TestCase):
    """Build a throwaway project tree and run the checker through its CLI."""

    def setUp(self) -> None:
        self.root = _support.scratch_dir("repo_checks_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    # -- fixture construction -------------------------------------------------
    def write(self, rel: str, text: str = "") -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def files_of(self, rels=None) -> dict:
        rels = self.tree_files() if rels is None else rels
        return {
            rel: {"bytes": (self.root / rel).stat().st_size, "sha256": file_digest(self.root / rel)}
            for rel in sorted(rels)
        }

    def tree_files(self) -> list[str]:
        result = []
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root).as_posix()
            if rel == MANIFEST or ".git" in Path(rel).parts:
                continue
            result.append(rel)
        return sorted(result)

    def write_manifest(self, files=None, metadata=None) -> Path:
        if files is None:
            entries = self.files_of()
        elif isinstance(files, dict):
            entries = files
        else:
            entries = self.files_of(files)
        data = {
            "captured_at": "2026-01-01T00:00:00+00:00",
            "manifest_role": "synthetic_test_tree",
            "license": "MIT",
            "self_excluded": True,
            "files": entries,
        }
        if metadata:
            data.update(metadata)
        path = self.root / MANIFEST
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def copy_examples(self) -> None:
        shutil.copytree(ROOT / "examples", self.root / "examples")
        shutil.copytree(
            ROOT / "scripts",
            self.root / "scripts",
            ignore=shutil.ignore_patterns("__pycache__"),
        )

    def replace_in(self, rel: str, old: str, new: str) -> None:
        path = self.root / rel
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def symlink_loader_probe(self, rel: str) -> Path:
        """Replace ``rel`` with a symlink to code that drops a marker file."""
        probe = _support.scratch_dir("loader_probe_")
        self.addCleanup(shutil.rmtree, probe, ignore_errors=True)
        marker = probe / "executed.marker"
        target = probe / "probe.py"
        target.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
            encoding="utf-8",
        )
        link = self.root / rel
        link.unlink()
        os.symlink(target, link)
        return marker

    def git(self, *args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.update(
            GIT_AUTHOR_NAME="Synthetic Test",
            GIT_AUTHOR_EMAIL="synthetic@example.com",
            GIT_COMMITTER_NAME="Synthetic Test",
            GIT_COMMITTER_EMAIL="synthetic@example.com",
        )
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )

    def commit(self) -> None:
        self.git("init", "-q")
        # Some filesystems make git default to core.filemode=false, which records
        # 100644 even for an executable file. The fixture must record the mode it
        # actually intends to test.
        self.git("config", "core.filemode", "true")
        self.git("add", "-A")
        self.git("commit", "-qm", "synthetic fixture")

    # -- checker invocation ---------------------------------------------------
    def run_check(self, *args: str) -> subprocess.CompletedProcess:
        # Run the checker with the Python bytecode-cache switches absent so the
        # default CLI is tested for read-only behavior: no __pycache__ residue
        # from importing the example configuration loaders.
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        env.pop("PYTHONPYCACHEPREFIX", None)
        return subprocess.run(
            [PYTHON, str(CHECKER), "--root", str(self.root), *args],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
        )

    def output(self, proc: subprocess.CompletedProcess) -> str:
        return proc.stdout + proc.stderr

    def assert_finding(self, proc: subprocess.CompletedProcess, text: str) -> None:
        self.assertEqual(proc.returncode, 1, self.output(proc))
        self.assertIn(text, self.output(proc))


@unittest.skipUnless(shutil.which("git"), "git is required for tracked-tree checks")
class VerifyManifestTests(RepositoryTreeCase):
    def test_valid_git_tree_passes(self):
        self.write("README.md", "# synthetic\n")
        self.write("scripts/tool.py", "print('ok')\n")
        self.write_manifest()
        self.commit()

        proc = self.run_check()

        self.assertEqual(proc.returncode, 0, self.output(proc))
        self.assertIn("OK (2 files)", proc.stdout)

    def test_tracked_file_missing_from_manifest_is_rejected(self):
        self.write("README.md", "# synthetic\n")
        self.write("notes.txt", "unlisted\n")
        self.write_manifest(["README.md"])
        self.commit()

        self.assert_finding(self.run_check(), "not listed in manifest: notes.txt")

    def test_manifest_entry_missing_from_tree_is_rejected(self):
        self.write("README.md", "# synthetic\n")
        files = self.files_of()
        files["ghost.txt"] = {"bytes": 4, "sha256": "0" * 64}
        self.write_manifest(files)
        self.commit()

        self.assert_finding(self.run_check(), "missing from tree: ghost.txt")

    def test_byte_count_mismatch_is_rejected(self):
        self.write("README.md", "1234\n")
        files = self.files_of()
        files["README.md"]["bytes"] += 1
        self.write_manifest(files)
        self.commit()

        self.assert_finding(self.run_check(), "size mismatch: README.md")

    def test_sha256_mismatch_is_rejected(self):
        self.write("README.md", "# synthetic\n")
        files = self.files_of()
        files["README.md"]["sha256"] = "0" * 64
        self.write_manifest(files)
        self.commit()

        self.assert_finding(self.run_check(), "sha256 mismatch: README.md")

    def test_untracked_nonignored_file_is_rejected(self):
        self.write("README.md", "# synthetic\n")
        self.write_manifest()
        self.commit()
        self.write("unexpected.txt", "not staged\n")

        self.assert_finding(self.run_check(), "untracked file not allowed: unexpected.txt")

    def test_path_traversal_in_manifest_is_rejected(self):
        self.write("README.md", "# synthetic\n")
        files = self.files_of()
        files["../escape.txt"] = {"bytes": 1, "sha256": "0" * 64}
        self.write_manifest(files)
        self.commit()

        self.assert_finding(self.run_check(), "unsafe path: ../escape.txt")

    def test_noncanonical_path_in_manifest_is_rejected(self):
        self.write("README.md", "# synthetic\n")
        files = self.files_of()
        files["./README.md"] = files.pop("README.md")
        self.write_manifest(files)
        self.commit()

        self.assert_finding(self.run_check(), "unsafe path: ./README.md")

    def test_manifest_must_not_list_itself(self):
        self.write("README.md", "# synthetic\n")
        files = self.files_of()
        files[MANIFEST] = {"bytes": 1, "sha256": "0" * 64}
        self.write_manifest(files)
        self.commit()

        self.assert_finding(self.run_check(), f"{MANIFEST} must not list itself")

    def test_missing_manifest_is_an_operational_error(self):
        self.write("README.md", "# synthetic\n")
        self.commit()

        proc = self.run_check()

        self.assertEqual(proc.returncode, 2, self.output(proc))
        self.assertIn("manifest not found", self.output(proc))

    def test_symlinked_file_is_rejected(self):
        self.write("target.txt", "synthetic\n")
        os.symlink("target.txt", self.root / "link.txt")
        self.write_manifest(["target.txt", "link.txt"])
        self.commit()

        self.assert_finding(self.run_check(), "symbolic link not allowed: link.txt")

    def test_symlinked_ancestor_is_rejected(self):
        self.write("real/notes.md", "synthetic\n")
        self.write_manifest(["real/notes.md"])
        self.commit()
        shutil.rmtree(self.root / "real")
        (self.root / "linked").mkdir()
        (self.root / "linked" / "notes.md").write_text("synthetic\n", encoding="utf-8")
        os.symlink("linked", self.root / "real")

        self.assert_finding(self.run_check(), "path traverses a symbolic link: real/notes.md")

    def test_verify_does_not_execute_symlinked_example_loader(self):
        self.copy_examples()
        marker = self.symlink_loader_probe("scripts/common.py")
        self.write_manifest()

        proc = self.run_check()

        self.assert_finding(proc, "symbolic link not allowed: scripts/common.py")
        self.assertFalse(marker.exists(), "the checker executed a symlinked loader")

    def test_verify_does_not_execute_loader_through_symlinked_ancestor(self):
        self.copy_examples()
        (self.root / "scripts").rename(self.root / "real_scripts")
        marker = self.symlink_loader_probe("real_scripts/common.py")
        os.symlink("real_scripts", self.root / "scripts")
        self.write_manifest()

        proc = self.run_check()

        self.assert_finding(proc, "symbolic link not allowed: scripts")
        self.assertFalse(marker.exists(), "the checker executed a loader through a symlinked directory")

    def test_runtime_state_path_is_rejected(self):
        self.write("README.md", "# synthetic\n")
        self.write(".state/seen.json", "{}\n")
        self.write_manifest()
        self.commit()

        self.assert_finding(self.run_check(), "private path not allowed: .state/seen.json")

    def test_live_root_configuration_is_rejected(self):
        self.write("README.md", "# synthetic\n")
        self.write("publish.yaml", "enabled: false\n")
        self.write_manifest()
        self.commit()

        self.assert_finding(self.run_check(), "private path not allowed: publish.yaml")

    def test_example_configuration_is_the_deliberate_allowance(self):
        self.write("README.md", "# synthetic\n")
        self.write("examples/publish.yaml", "enabled: false\n")
        self.write_manifest()
        self.commit()

        proc = self.run_check()

        self.assertEqual(proc.returncode, 0, self.output(proc))

    def test_checker_source_is_not_private_material(self):
        self.write("tools/check_repository.py", CHECKER.read_text(encoding="utf-8"))
        self.write_manifest()
        self.commit()

        proc = self.run_check()

        self.assertEqual(proc.returncode, 0, self.output(proc))

    def test_pgp_private_key_marker_is_still_rejected(self):
        marker = "-----BEGIN PGP " + "PRIVATE KEY BLOCK-----"
        self.write("notes.txt", marker + "\nsynthetic-not-a-key\n")
        self.write_manifest()
        self.commit()

        proc = self.run_check()

        self.assert_finding(proc, "private key material found: notes.txt")
        self.assertNotIn(marker, self.output(proc))

    def test_credential_material_is_reported_without_content(self):
        self.write("README.md", "# synthetic\n")
        self.write("notes.txt", f"{PEM_MARKER}\nnot-a-real-key\n")
        self.write_manifest()
        self.commit()

        proc = self.run_check()

        self.assert_finding(proc, "private key material found: notes.txt")
        self.assertNotIn(PEM_MARKER, self.output(proc))

    def test_launcher_must_be_tracked_executable(self):
        self.write("bin/ca", "#!/usr/bin/env bash\nexit 0\n")
        os.chmod(self.root / "bin/ca", 0o644)
        self.write_manifest()
        self.commit()

        self.assert_finding(
            self.run_check(),
            "bin/ca must be tracked as executable mode 100755 (found 100644)",
        )

    def test_executable_launcher_in_tracked_tree_passes(self):
        self.write("bin/ca", "#!/usr/bin/env bash\nexit 0\n")
        os.chmod(self.root / "bin/ca", 0o755)
        self.write_manifest()
        self.commit()

        proc = self.run_check()

        self.assertEqual(proc.returncode, 0, self.output(proc))


class VerifyArtifactTests(RepositoryTreeCase):
    """A read-only export has no ``.git``; only local venv/cache dirs are ignored."""

    def test_exported_tree_passes_and_ignores_local_caches(self):
        self.write("README.md", "# synthetic\n")
        self.write("docs/guide.md", "guide\n")
        self.write(".venv/lib/site.py", "print('local venv')\n")
        self.write("__pycache__/module.cpython-313.pyc", "cache\n")
        self.write_manifest(["README.md", "docs/guide.md"])

        proc = self.run_check()

        self.assertEqual(proc.returncode, 0, self.output(proc))
        self.assertIn("OK (2 files)", proc.stdout)

    def test_extra_exported_file_is_rejected(self):
        self.write("README.md", "# synthetic\n")
        self.write("docs/extra.md", "extra\n")
        self.write_manifest(["README.md"])

        self.assert_finding(self.run_check(), "not listed in manifest: docs/extra.md")

    def test_nonregular_file_is_rejected(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo is not available")
        self.write("README.md", "# synthetic\n")
        os.mkfifo(self.root / "pipe")
        files = self.files_of(["README.md"])
        files["pipe"] = {"bytes": 0, "sha256": "0" * 64}
        self.write_manifest(files)

        self.assert_finding(self.run_check(), "not a regular file: pipe")

    def test_exported_launcher_must_be_executable(self):
        self.write("bin/ca", "#!/usr/bin/env bash\nexit 0\n")
        os.chmod(self.root / "bin/ca", 0o644)
        self.write_manifest()

        self.assert_finding(
            self.run_check(),
            "bin/ca must be executable in an extracted artifact",
        )

    def test_exported_executable_launcher_passes(self):
        self.write("bin/ca", "#!/usr/bin/env bash\nexit 0\n")
        os.chmod(self.root / "bin/ca", 0o755)
        self.write_manifest()

        proc = self.run_check()

        self.assertEqual(proc.returncode, 0, self.output(proc))


class UpdateManifestTests(RepositoryTreeCase):
    def test_update_writes_a_complete_manifest_and_then_verifies(self):
        self.write("README.md", "# synthetic\n")
        self.write("docs/guide.md", "guide\n")

        proc = self.run_check("--update-manifest")

        self.assertEqual(proc.returncode, 0, self.output(proc))
        data = json.loads((self.root / MANIFEST).read_text(encoding="utf-8"))
        self.assertIs(data["self_excluded"], True)
        self.assertEqual(sorted(data["files"]), ["README.md", "docs/guide.md"])
        self.assertNotIn(MANIFEST, data["files"])
        self.assertEqual(self.run_check().returncode, 0, self.output(self.run_check()))

    def test_update_preserves_metadata_and_file_mode(self):
        self.write("README.md", "# synthetic\n")
        self.write_manifest(
            metadata={
                "manifest_role": "accepted_public_tree",
                "usage": "verify the public source",
                "license": "MIT",
            }
        )
        os.chmod(self.root / MANIFEST, 0o640)
        before = json.loads((self.root / MANIFEST).read_text(encoding="utf-8"))

        proc = self.run_check("--update-manifest")

        self.assertEqual(proc.returncode, 0, self.output(proc))
        after = json.loads((self.root / MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual(after["manifest_role"], "accepted_public_tree")
        self.assertEqual(after["usage"], "verify the public source")
        self.assertEqual(after["license"], "MIT")
        self.assertNotEqual(after["captured_at"], before["captured_at"])
        self.assertEqual(os.stat(self.root / MANIFEST).st_mode & 0o777, 0o640)
        self.assertTrue((self.root / MANIFEST).read_text(encoding="utf-8").endswith("\n"))

    def test_update_refuses_private_paths_without_touching_the_manifest(self):
        self.write("README.md", "# synthetic\n")
        self.write_manifest(["README.md"])
        before = (self.root / MANIFEST).read_bytes()
        self.write(".state/seen.json", "{}\n")

        proc = self.run_check("--update-manifest")

        self.assertEqual(proc.returncode, 1, self.output(proc))
        self.assertIn("private path not allowed: .state/seen.json", self.output(proc))
        self.assertEqual((self.root / MANIFEST).read_bytes(), before)

    def test_update_refuses_symlinked_paths_without_touching_the_manifest(self):
        self.write("README.md", "# synthetic\n")
        self.write_manifest(["README.md"])
        before = (self.root / MANIFEST).read_bytes()
        os.symlink("README.md", self.root / "link.md")

        proc = self.run_check("--update-manifest")

        self.assertEqual(proc.returncode, 1, self.output(proc))
        self.assertIn("symbolic link not allowed: link.md", self.output(proc))
        self.assertEqual((self.root / MANIFEST).read_bytes(), before)

    def test_update_refuses_untracked_files_in_a_checkout(self):
        self.write("README.md", "# synthetic\n")
        self.write_manifest(["README.md"])
        self.commit()
        before = (self.root / MANIFEST).read_bytes()
        self.write("unexpected.txt", "not staged\n")

        proc = self.run_check("--update-manifest")

        self.assertEqual(proc.returncode, 1, self.output(proc))
        self.assertIn("untracked file not allowed: unexpected.txt", self.output(proc))
        self.assertEqual((self.root / MANIFEST).read_bytes(), before)

    def test_update_accepts_a_staged_new_file_in_a_checkout(self):
        self.write("README.md", "# synthetic\n")
        self.write_manifest(["README.md"])
        self.commit()
        self.write("docs/guide.md", "guide\n")
        self.git("add", "docs/guide.md")

        proc = self.run_check("--update-manifest")

        self.assertEqual(proc.returncode, 0, self.output(proc))
        data = json.loads((self.root / MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual(sorted(data["files"]), ["README.md", "docs/guide.md"])
        self.assertEqual(self.run_check().returncode, 0, self.output(self.run_check()))

    def test_update_refuses_a_tracked_non_executable_launcher(self):
        self.write("bin/ca", "#!/usr/bin/env bash\nexit 0\n")
        os.chmod(self.root / "bin/ca", 0o644)
        self.write_manifest(["bin/ca"])
        self.commit()
        before = (self.root / MANIFEST).read_bytes()

        proc = self.run_check("--update-manifest")

        self.assertEqual(proc.returncode, 1, self.output(proc))
        self.assertIn("must be tracked as executable mode 100755", self.output(proc))
        self.assertEqual((self.root / MANIFEST).read_bytes(), before)

    def test_update_refuses_enabled_watched_source_without_touching_manifest(self):
        self.copy_examples()
        self.write_manifest()
        before = (self.root / MANIFEST).read_bytes()
        self.replace_in("examples/sources.yaml", "enabled: false", "enabled: true")

        proc = self.run_check("--update-manifest")

        self.assertEqual(proc.returncode, 1, self.output(proc))
        self.assertIn("example sources.yaml enables source:", self.output(proc))
        self.assertEqual((self.root / MANIFEST).read_bytes(), before)

    def test_update_refuses_enabled_publication_without_touching_manifest(self):
        self.copy_examples()
        self.write_manifest()
        before = (self.root / MANIFEST).read_bytes()
        self.replace_in("examples/publish.yaml", "enabled: false", "enabled: true")

        proc = self.run_check("--update-manifest")

        self.assertEqual(proc.returncode, 1, self.output(proc))
        self.assertIn("example publish.yaml must remain disabled", self.output(proc))
        self.assertEqual((self.root / MANIFEST).read_bytes(), before)

    def test_update_refuses_invalid_syntax_without_creating_manifest(self):
        self.write("README.md", "# synthetic\n")
        self.write("scripts/broken.py", "def broken(:\n")

        proc = self.run_check("--update-manifest")

        self.assertEqual(proc.returncode, 1, self.output(proc))
        self.assertIn("python syntax error: scripts/broken.py", self.output(proc))
        self.assertFalse((self.root / MANIFEST).exists())

    def test_update_does_not_execute_symlinked_example_loader(self):
        self.copy_examples()
        self.write_manifest()
        before = (self.root / MANIFEST).read_bytes()
        marker = self.symlink_loader_probe("scripts/common.py")

        proc = self.run_check("--update-manifest")

        self.assertEqual(proc.returncode, 1, self.output(proc))
        self.assertIn("symbolic link not allowed: scripts/common.py", self.output(proc))
        self.assertEqual((self.root / MANIFEST).read_bytes(), before)
        self.assertFalse(marker.exists(), "the checker executed a symlinked loader")


class SyntaxCheckTests(RepositoryTreeCase):
    def test_invalid_python_is_rejected(self):
        self.write("scripts/broken.py", "def broken(:\n")
        self.write_manifest()

        self.assert_finding(self.run_check(), "python syntax error: scripts/broken.py")

    @unittest.skipUnless(shutil.which("bash"), "bash is required for the launcher syntax check")
    def test_invalid_bash_launcher_is_rejected(self):
        self.write("bin/ca", "if then\n")
        os.chmod(self.root / "bin/ca", 0o755)
        self.write_manifest()

        self.assert_finding(self.run_check(), "bash syntax error: bin/ca")

    @unittest.skipUnless(shutil.which("node"), "node is required for the JavaScript syntax check")
    def test_invalid_javascript_is_rejected(self):
        self.write("scripts/reader.js", "function {\n")
        self.write_manifest()

        self.assert_finding(self.run_check(), "javascript syntax error: scripts/reader.js")

    @unittest.skipUnless(
        shutil.which("bash") and shutil.which("node"),
        "bash and node are required for the full syntax check",
    )
    def test_valid_syntax_tree_passes(self):
        self.write("scripts/tool.py", "def main():\n    return 0\n")
        self.write("scripts/reader.js", "function read() { return 1; }\n")
        self.write("bin/ca", "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n")
        os.chmod(self.root / "bin/ca", 0o755)
        self.write_manifest()

        proc = self.run_check()

        self.assertEqual(proc.returncode, 0, self.output(proc))


class WorkflowEnvironmentTests(RepositoryTreeCase):
    def test_runner_scratch_is_allowlisted_without_allowing_project_config(self):
        import yaml

        workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))
        job = workflow["jobs"]["test"]
        step = next(item for item in job["steps"] if item.get("working-directory") == "tests")
        runner_temp = self.root / "runner-temp"
        platform_temp = self.root / "platform-temp"
        runner_temp.mkdir()
        platform_temp.mkdir()
        env = os.environ.copy()
        for key in ("PYTHONPATH", "CA_SCRATCH_ROOT", "CA_TEST_SCRATCH"):
            env.pop(key, None)
        env.update(TMPDIR=str(platform_temp), PYTHONDONTWRITEBYTECODE="1")
        for config in (workflow.get("env", {}), job.get("env", {}), step.get("env", {})):
            for key, value in config.items():
                env[key] = str(value).replace("${{ runner.temp }}", str(runner_temp))
        code = '''import json, pathlib, sys
sys.path[:0] = [str(pathlib.Path(sys.argv[1]) / name) for name in ("tests", "scripts")]
import _support, common, extract_remote
project = _support.scratch_dir("workflow_archive_")
common.ROOT = project
extract_remote.ROOT = project
output = _support.SCRATCH / "output"
try:
    output_allowed = extract_remote.validate_output_dir(output) == output.resolve()
except ValueError:
    output_allowed = False
print(json.dumps({
    "input_allowed": common.is_allowed_file_path(_support.SCRATCH / "content.md", project),
    "output_allowed": output_allowed,
    "project_config_allowed": common.is_allowed_file_path(project / "sources.yaml", project),
    "outside_allowed": common.is_allowed_file_path(pathlib.Path("/etc/cak-synthetic-outside.txt"), project),
}))
'''
        proc = subprocess.run(
            [PYTHON, "-c", code, str(ROOT)], env=env,
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, self.output(proc))
        result = json.loads(proc.stdout)
        self.assertTrue(result["input_allowed"], result)
        self.assertTrue(result["output_allowed"], result)
        self.assertFalse(result["project_config_allowed"], result)
        self.assertFalse(result["outside_allowed"], result)


class ExampleConfigTests(RepositoryTreeCase):
    def test_shipped_examples_are_safe_and_disabled(self):
        self.copy_examples()
        self.write_manifest()

        proc = self.run_check()

        self.assertEqual(proc.returncode, 0, self.output(proc))

    def test_default_check_leaves_no_bytecode_residue(self):
        self.copy_examples()
        self.write_manifest()
        before = (self.root / MANIFEST).read_bytes()

        proc = self.run_check()

        self.assertEqual(proc.returncode, 0, self.output(proc))
        self.assertEqual((self.root / MANIFEST).read_bytes(), before)
        residue = sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"}
        )
        self.assertEqual(residue, [], "the default checker wrote bytecode into the checked tree")

    def test_enabled_watched_source_is_rejected(self):
        self.copy_examples()
        self.replace_in("examples/sources.yaml", "enabled: false", "enabled: true")
        self.write_manifest()

        self.assert_finding(
            self.run_check(), "example sources.yaml enables source: example_rss"
        )

    def test_enabled_publication_is_rejected(self):
        self.copy_examples()
        self.replace_in("examples/publish.yaml", "enabled: false", "enabled: true")
        self.write_manifest()

        self.assert_finding(self.run_check(), "example publish.yaml must remain disabled")

    def test_invalid_media_example_is_rejected(self):
        self.copy_examples()
        self.replace_in("examples/media.yaml", "max_images: 100", "max_images: 0")
        self.write_manifest()

        self.assert_finding(self.run_check(), "example media.yaml invalid")


if __name__ == "__main__":
    unittest.main()
