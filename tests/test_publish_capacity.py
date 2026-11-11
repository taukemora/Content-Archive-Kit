"""Capacity reporting for the encrypted-site publisher (synthetic trees only).

The report may only quantify artifacts the publisher's own whitelist accepted.
These tests build tiny exports, never decrypt an object, never contact a
network and never touch a real archive.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))

import publish_site as ps  # noqa: E402
import test_publish_site as fixtures  # noqa: E402

ARCHIVE_ID = fixtures.ARCHIVE_ID
ENCRYPTED_INDEX_HTML = fixtures.ENCRYPTED_INDEX_HTML


def site_doc(version: str, object_file: str, object_size: int) -> dict:
    """The frozen public manifest shape, minimal but genuine."""
    return {
        "format": "contentarchive-encrypted-site",
        "protocol": "CA-ENC-1",
        "archive_id": ARCHIVE_ID,
        "kdf": {
            "name": "PBKDF2-HMAC-SHA256",
            "hash": "sha256",
            "iterations": 600000,
            "salt": base64.b64encode(b"0" * 16).decode("ascii"),
            "key_length": 32,
        },
        "wrap": {
            "cipher": "AES-256-GCM",
            "nonce": base64.b64encode(b"2" * 12).decode("ascii"),
            "ciphertext": base64.b64encode(b"3" * 48).decode("ascii"),
            "tag_length": 128,
        },
        "index": {"file": object_file, "bytes": object_size},
        "version": version,
    }


def tree_files(object_sizes=(1000,)) -> dict[str, bytes]:
    """Return the four static files plus opaque objects, keyed by path."""
    objects = {}
    for index, size in enumerate(object_sizes):
        digest = hashlib.sha256(f"object-{index}".encode("ascii")).hexdigest()
        objects[f"objects/{digest}.{'b' * 32}.bin"] = bytes([index % 256]) * size
    version = hashlib.sha256(b"capacity-version").hexdigest()[:32]
    doc = json.dumps(
        site_doc(version, next(iter(objects)), object_sizes[0]),
        ensure_ascii=False,
        sort_keys=True,
    )
    files = {
        "index.html": ENCRYPTED_INDEX_HTML.encode("utf-8"),
        "site.json": doc.encode("utf-8"),
        "reader.js": b"/* reader */\n",
        ".nojekyll": b"",
        **objects,
    }
    return files


class CapacityTreeCase(unittest.TestCase):
    """Scratch export trees that satisfy the real whitelist validator."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            dir=str(_support.SCRATCH), prefix="ca-capacity-"
        )
        self.addCleanup(self.temp.cleanup)
        self.build = Path(self.temp.name) / "build"

    def write_tree(self, object_sizes=(1000,), *, total_bytes=None,
                   files=None) -> list[str]:
        files = dict(tree_files(object_sizes) if files is None else files)
        self.build.mkdir(parents=True, exist_ok=True)
        for relative, data in files.items():
            target = self.build / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if total_bytes is not None:
            current = sum((self.build / name).stat().st_size for name in files)
            padding = total_bytes - current
            self.assertGreaterEqual(padding, 0, "tree already exceeds the target size")
            (self.build / ".nojekyll").write_bytes(b"p" * padding)
        return sorted(files)

    def report(self, names: list[str]) -> dict:
        validated = ps.validate_whitelist(self.build, names)
        return ps._capacity_report(self.build, validated)


class CapacityMathTests(CapacityTreeCase):
    def test_groups_shell_and_ciphertext_bytes_and_largest_file(self):
        names = self.write_tree(object_sizes=(1000, 250))
        report = self.report(names)

        expected_shell = sum(
            (self.build / name).stat().st_size
            for name in names
            if name in ps.PUBLIC_STATIC_FILES
        )
        expected_object = 1250
        self.assertEqual(report["status"], "ok", report)
        self.assertEqual(report["file_count"], len(names))
        self.assertEqual(report["shell_bytes"], expected_shell)
        self.assertEqual(report["object_bytes"], expected_object)
        self.assertEqual(report["total_bytes"], expected_shell + expected_object)
        self.assertEqual(report["largest_file_bytes"], 1000)
        self.assertEqual(report["local_limit_bytes"], ps.MAX_TOTAL_BYTES)

    def test_report_uses_only_allowlisted_fields(self):
        names = self.write_tree()
        report = self.report(names)
        self.assertLessEqual(set(report), set(ps.CAPACITY_KEYS))
        for key in ("file_count", "total_bytes", "shell_bytes", "object_bytes",
                    "largest_file_bytes", "local_limit_bytes", "ratio", "percent"):
            self.assertIn(key, report)
        payload = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("objects/", payload)
        self.assertNotIn(str(self.build), payload)
        self.assertIn("<0.1%", report["message"])

    def test_boundary_ratios_use_integer_comparisons(self):
        # Exactly 75%, exactly 90%, and one byte below each threshold.
        names = self.write_tree(total_bytes=3000)
        cases = {
            4000: ("warning", 75.0),
            4001: ("ok", None),
            3333: ("critical", None),
        }
        for limit, (level, percent) in cases.items():
            with self.subTest(limit=limit):
                with mock.patch.object(ps, "MAX_TOTAL_BYTES", limit):
                    report = self.report(names)
                self.assertEqual(report["status"], level, report)
                self.assertEqual(report["local_limit_bytes"], limit)
                self.assertEqual(report["total_bytes"], 3000)
                if percent is not None:
                    self.assertEqual(report["percent"], percent)

        names = self.write_tree(total_bytes=3600)
        with mock.patch.object(ps, "MAX_TOTAL_BYTES", 4000):
            exact = self.report(names)
        self.assertEqual(exact["status"], "critical")
        self.assertEqual(exact["percent"], 90.0)

        names = self.write_tree(total_bytes=3599)
        with mock.patch.object(ps, "MAX_TOTAL_BYTES", 4000):
            below = self.report(names)
        self.assertEqual(below["status"], "warning")
        self.assertLess(below["percent"], 90.0)

    def test_report_never_reads_object_contents(self):
        names = self.write_tree(object_sizes=(500,))
        validated = ps.validate_whitelist(self.build, names)

        def guarded(*args, **kwargs):
            raise AssertionError("capacity must not read published files")

        with mock.patch.object(ps, "_read_nofollow", side_effect=guarded), \
                mock.patch("builtins.open", side_effect=guarded):
            report = ps._capacity_report(self.build, validated)
        self.assertEqual(report["object_bytes"], 500)

    def test_symlink_and_unknown_paths_are_still_rejected(self):
        names = self.write_tree(object_sizes=(10,))
        outside = Path(self.temp.name) / "outside.bin"
        outside.write_bytes(b"secret")
        link = next(name for name in names if name.startswith("objects/"))
        (self.build / link).unlink()
        os.symlink(outside, self.build / link)
        with self.assertRaises(ps.SecurityError):
            self.report(names)

        forged = self.write_tree()
        with self.assertRaises(ps.SecurityError):
            ps._capacity_report(self.build, forged + ["notes.txt"])

    def test_missing_file_is_an_error_not_a_zero(self):
        names = self.write_tree()
        (self.build / names[0]).unlink()
        with self.assertRaises((ps.SecurityError, OSError)):
            self.report(names)


class StatusCapacityTests(fixtures.PublisherTestCase):
    def build_once(self, payload="capacity-v1"):
        Path(self.key_file).write_text("key material", encoding="utf-8")
        os.chmod(self.key_file, 0o600)
        fake = self.use_site(fixtures.FakeEncryptedSite(self.key_file, payload=payload))
        built = ps.build_only(self.root, self.config(), fake)
        return built

    def status_payload(self):
        code, out, err = self.run_cli(["status", "--root", str(self.root),
                                       "--config", str(self.config_path)])
        self.assertEqual(code, 0, out + err)
        return json.loads(out)

    def test_status_reports_capacity_of_validated_build(self):
        built = self.build_once()
        payload = self.status_payload()
        capacity = payload["capacity"]
        self.assertEqual(capacity["status"], "ok", capacity)
        self.assertEqual(capacity["file_count"], len(built["files"]))
        self.assertEqual(capacity["total_bytes"], built["capacity"]["total_bytes"])
        build_dir = self.workspace / "build"
        self.assertEqual(
            capacity["total_bytes"],
            sum((build_dir / name).stat().st_size for name in built["files"]),
        )
        self.assertEqual(capacity, built["capacity"])

    def test_status_measures_without_writing_git_or_network(self):
        self.build_once()
        (self.workspace / "state.json").write_text(
            json.dumps({"status": "sentinel", "at": "2026-01-01T00:00:00+00:00"}),
            encoding="utf-8",
        )

        def snapshot():
            entries = {}
            for path in sorted(self.workspace.rglob("*")):
                stat_result = path.lstat()
                entries[str(path.relative_to(self.workspace))] = (
                    stat_result.st_mode,
                    stat_result.st_size,
                    stat_result.st_mtime_ns,
                    path.read_bytes() if path.is_file() and not path.is_symlink() else None,
                )
            return entries

        before = snapshot()
        with mock.patch.object(ps, "_run_git",
                               side_effect=AssertionError("status must not run git")), \
                mock.patch.object(ps.urllib.request, "build_opener",
                                  side_effect=AssertionError("status must not use network")), \
                mock.patch.object(ps, "_ensure_private_dir",
                                  side_effect=AssertionError("status must not write")):
            payload = self.status_payload()
        self.assertEqual(snapshot(), before)
        self.assertEqual(payload["capacity"]["status"], "ok")
        self.assertEqual(payload["last_publish"]["status"], "sentinel")

    def test_status_without_build_reports_unavailable_not_zero(self):
        payload = self.status_payload()
        capacity = payload["capacity"]
        self.assertEqual(capacity["status"], "unavailable")
        for key in ("total_bytes", "shell_bytes", "object_bytes", "file_count"):
            self.assertNotIn(key, capacity)
        self.assertIn("reason", capacity)
        self.assertFalse(payload["site_build"]["ready"])
        self.assertFalse((self.workspace / "state.json").exists())

    def test_status_with_corrupt_export_reports_unavailable(self):
        self.build_once()
        site_json = self.workspace / "build" / "site.json"
        site_json.write_text("{not json", encoding="utf-8")
        capacity = self.status_payload()["capacity"]
        self.assertEqual(capacity["status"], "unavailable")
        self.assertNotIn("total_bytes", capacity)

    def test_status_with_symlinked_object_reports_unavailable(self):
        built = self.build_once()
        build_dir = self.workspace / "build"
        object_name = next(name for name in built["files"] if name.startswith("objects/"))
        outside = self.base / "outside.bin"
        outside.write_bytes(b"private bytes")
        (build_dir / object_name).unlink()
        os.symlink(outside, build_dir / object_name)
        capacity = self.status_payload()["capacity"]
        self.assertEqual(capacity["status"], "unavailable")
        self.assertIn("符号链接", capacity["reason"])

    def test_status_with_partial_export_reports_unavailable(self):
        self.build_once()
        (self.workspace / "build" / "reader.js").unlink()
        capacity = self.status_payload()["capacity"]
        self.assertEqual(capacity["status"], "unavailable")
        self.assertIn("reason", capacity)
        self.assertNotIn("total_bytes", capacity)

    def test_status_build_path_that_is_not_a_directory_is_unavailable(self):
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "build").write_text("not a directory", encoding="utf-8")
        capacity = self.status_payload()["capacity"]
        self.assertEqual(capacity["status"], "unavailable")
        self.assertNotIn("total_bytes", capacity)

    def test_status_without_config_reports_capacity_unavailable(self):
        self.config_path.unlink()
        code, out, err = self.run_cli(["status", "--root", str(self.root)])
        self.assertEqual(code, 0, out + err)
        payload = json.loads(out)
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["capacity"]["status"], "unavailable")
        self.assertNotIn("total_bytes", payload["capacity"])

    def test_status_ignores_files_outside_the_allowlist(self):
        built = self.build_once()
        build_dir = self.workspace / "build"
        (build_dir / "notes.txt").write_text("not published", encoding="utf-8")
        (build_dir / "raw").mkdir()
        (build_dir / "raw" / "source.bin").write_bytes(b"x" * 4096)
        capacity = self.status_payload()["capacity"]
        self.assertEqual(capacity["status"], "ok")
        self.assertEqual(capacity["file_count"], len(built["files"]))
        self.assertEqual(
            capacity["total_bytes"],
            sum((build_dir / name).stat().st_size for name in built["files"]),
        )


class BuildCapacityTests(fixtures.PublisherTestCase):
    def prepare(self, fake=None):
        Path(self.key_file).write_text("key material", encoding="utf-8")
        os.chmod(self.key_file, 0o600)
        return self.use_site(fake or fixtures.FakeEncryptedSite(self.key_file,
                                                               payload="capacity-v1"))

    def test_build_capacity_matches_validated_files(self):
        fake = self.prepare()
        built = ps.build_only(self.root, self.config(), fake)
        build_dir = self.workspace / "build"
        sizes = {name: (build_dir / name).stat().st_size for name in built["files"]}
        capacity = built["capacity"]
        self.assertEqual(capacity["file_count"], len(sizes))
        self.assertEqual(capacity["total_bytes"], sum(sizes.values()))
        self.assertEqual(capacity["largest_file_bytes"], max(sizes.values()))
        self.assertEqual(
            capacity["shell_bytes"],
            sum(size for name, size in sizes.items() if name in ps.PUBLIC_STATIC_FILES),
        )
        self.assertEqual(
            capacity["object_bytes"],
            sum(size for name, size in sizes.items() if name.startswith("objects/")),
        )
        self.assertEqual(capacity["total_bytes"],
                         capacity["shell_bytes"] + capacity["object_bytes"])

    def test_build_ignores_forged_capacity_metadata(self):
        fake = self.prepare()
        real_build = fake.build_site

        def build(root, output_dir, key_path):
            result = real_build(root, output_dir, key_path)
            result["capacity"] = {"status": "ok", "total_bytes": 1, "file_count": 9999}
            result["total_bytes"] = 1
            result["shell_bytes"] = 0
            return result

        fake.build_site = build
        built = ps.build_only(self.root, self.config(), fake)
        capacity = built["capacity"]
        self.assertNotEqual(capacity["total_bytes"], 1)
        self.assertNotEqual(capacity["file_count"], 9999)
        self.assertGreater(capacity["total_bytes"], 1)
        self.assertEqual(
            capacity["total_bytes"],
            sum((self.workspace / "build" / name).stat().st_size
                for name in built["files"]),
        )

    def test_cli_build_prints_capacity(self):
        self.prepare()
        code, out, err = self.run_cli(["build", "--root", str(self.root),
                                       "--config", str(self.config_path)])
        self.assertEqual(code, 0, out + err)
        payload = json.loads(out)
        self.assertEqual(payload["capacity"]["status"], "ok")
        self.assertGreater(payload["capacity"]["total_bytes"], 0)
        self.assertNotIn("output_dir", payload)

    def test_build_rejects_symlink_before_reporting_capacity(self):
        fake = self.prepare()
        link_name = f"objects/{'a' * 64}.{'b' * 32}.bin"
        outside = self.base / "synthetic-outside-object.bin"
        outside.write_bytes(b"synthetic forbidden object")
        real_build = fake.build_site

        def build(root, output_dir, key_path):
            result = real_build(root, output_dir, key_path)
            link = Path(output_dir) / link_name
            link.parent.mkdir(parents=True, exist_ok=True)
            if not link.is_symlink():
                os.symlink(outside, link)
            result["files"] = result["files"] + [link_name]
            return result

        fake.build_site = build
        with self.assertRaises(ps.SecurityError):
            ps.build_only(self.root, self.config(), fake)
        self.assertEqual(outside.read_bytes(), b"synthetic forbidden object")


class RunCapacityTests(fixtures.GitTestCase):
    def test_success_and_noop_run_report_capacity(self):
        self.use_site(fixtures.FakeEncryptedSite(self.key_file, payload="capacity-v1"))
        first = self.publish()
        self.assertEqual(first["status"], "success", first)
        capacity = first["capacity"]
        self.assertEqual(capacity["status"], "ok")
        self.assertEqual(
            capacity["total_bytes"],
            sum((self.workspace / "build" / name).stat().st_size
                for name in first["files"]),
        )
        self.assertEqual(self.state()["capacity"], capacity)

        second = self.publish()
        self.assertEqual(second["status"], "unchanged", second)
        self.assertEqual(second["capacity"], capacity)

    def test_failed_run_still_reports_built_capacity(self):
        self.use_site(fixtures.FakeEncryptedSite(self.key_file, payload="capacity-v1"))
        self.seed_foreign_repo()
        with self.assertRaises(ps.PublishError):
            self.publish()
        state = self.state()
        self.assertEqual(state["status"], "failed")
        self.assertGreater(state["capacity"]["total_bytes"], 0)
        self.assertEqual(state["capacity"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
