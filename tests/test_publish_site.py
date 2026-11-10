"""Tests for the standalone encrypted-site publisher.

All filesystem work happens in a scratch directory.  No test performs a real
GitHub write, loads a real deployment key, or reads production credentials.
"""
from __future__ import annotations

import base64
import contextlib
import csv
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))

import publish_site as ps

# The publication target is whatever the publisher module allows; the test never
# bakes in a personal repository or site. A generic default keeps the suite
# usable when the allowlist moves into configuration.
REPO = getattr(ps, "ALLOWED_REPOSITORY", "archive-owner/content-archive-site")
SITE_URL = getattr(ps, "ALLOWED_SITE_URL", "https://archive-owner.github.io/content-archive-site/")
_REPO_OWNER, _REPO_NAME = REPO.split("/", 1)
_SITE_HOST = SITE_URL.split("//", 1)[1].split("/", 1)[0]
TEST_GIT_NAME = "Archive Publisher"
TEST_GIT_EMAIL = "publisher@example.com"
ARCHIVE_ID = "0123456789abcdef0123456789abcdef"
ARTICLE_TITLE = "这是一个不应进入密文发布仓库判定之外的标题MARKER"

# Minimal but genuine encrypted entry shell: the marker plus the two empty JSON
# scripts that reader.js expects in the published page.
ENCRYPTED_INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN" data-ca-encrypted="1">
<head>
  <meta charset="utf-8">
  <title>内容收藏夹</title>
</head>
<body>
  <script id="archive-data" type="application/json">
[]
</script>
  <script id="labels-data" type="application/json">
{}
</script>
  <script src="reader.js" defer></script>
</body>
</html>
"""


def index_without_favicon() -> str:
    """Legacy published page: same shell, no <link rel="icon"> at all."""
    return ENCRYPTED_INDEX_HTML



def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FakeEncryptedSite:
    """Stands in for the frozen encrypted_site API (implemented separately)."""

    def __init__(self, key_file: Path, payload: str = "v1", *, extra_files=None,
                 site_json_extra=None, site_doc_mutator=None, omit=(),
                 symlink_name=None, fail=None):
        self.key_file = key_file
        self.payload = payload
        self.extra_files = dict(extra_files or {})
        self.site_json_extra = dict(site_json_extra or {})
        self.site_doc_mutator = site_doc_mutator
        self.omit = set(omit)
        self.symlink_name = symlink_name
        self.fail = fail
        self.build_calls = 0
        self.init_calls = 0
        self.passphrase_seen = None
        self.last_root = None
        self.last_output = None

    # -- initialize_key ---------------------------------------------------
    def initialize_key(self, key_path, passphrase):
        self.init_calls += 1
        self.passphrase_seen = passphrase
        key_path = Path(key_path)
        key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, b"FAKE-PRIVATE-KEY-MATERIAL-DO-NOT-PUBLISH")
        finally:
            os.close(fd)
        os.chmod(key_path, 0o600)
        return {
            "algorithm": "PBKDF2-HMAC-SHA256",
            "iterations": 600000,
            "salt": base64.b64encode(b"0" * 16).decode("ascii"),
            "wrapped_dek": base64.b64encode(b"1" * 48).decode("ascii"),
            "archive_id": ARCHIVE_ID,
        }

    def site_doc(self, version: str, object_file: str) -> dict:
        """Mirror the frozen production schema exactly (7 top-level fields)."""
        doc = {
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
            "index": {"file": object_file, "bytes": len(self.payload)},
            "version": version,
        }
        doc.update(self.site_json_extra)
        if self.site_doc_mutator is not None:
            self.site_doc_mutator(doc)
        return doc

    # -- build_site -------------------------------------------------------
    def build_site(self, root, output_dir, key_path):
        self.build_calls += 1
        self.last_root = Path(root)
        root = Path(root)
        output_dir = Path(output_dir)
        self.last_output = output_dir
        if self.fail:
            raise self.fail
        assert Path(key_path) == self.key_file, (key_path, self.key_file)
        version = sha(self.payload)[:32]
        oid = sha("oid:" + self.payload)
        rev = sha("rev:" + self.payload)[:32]
        object_file = f"objects/{oid}.{rev}.bin"
        files = {
            "index.html": ENCRYPTED_INDEX_HTML,
            ".nojekyll": "",
            "reader.js": "/* reader */",
            object_file: base64.b64encode(self.payload.encode()).decode(),
            "site.json": json.dumps(self.site_doc(version, object_file),
                                    ensure_ascii=False, sort_keys=True),
            **self.extra_files,
        }
        for name in list(files):
            if name in self.omit:
                files.pop(name)
        names = set(files)
        previous = None
        old_site = output_dir / "site.json"
        if old_site.is_file():
            try:
                previous = json.loads(old_site.read_text("utf-8")).get("version")
            except (OSError, ValueError):
                previous = None
        output_dir.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            if name.startswith("/") or ".." in Path(name).parts:
                continue  # never let the fixture write outside the export dir
            target = output_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or target.exists():
                if target.is_symlink():
                    target.unlink()
            target.write_text(text, encoding="utf-8")
        if self.symlink_name:
            link = output_dir / self.symlink_name
            link.parent.mkdir(parents=True, exist_ok=True)
            if not link.is_symlink():
                os.symlink("/etc/hostname", link)
        return {
            "status": "ok",
            "changed": previous != version,
            "version": version,
            "files": list(files),
            "entries": 1,
            "objects_written": 1 if previous != version else 0,
            "objects_reused": 0 if previous != version else 1,
        }


class PublisherTestCase(unittest.TestCase):
    """Common scratch layout: project/, secrets/, workspace/, remote.git/."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(_support.SCRATCH),
                                                prefix="ca-publish-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        (self.root / "library").mkdir(parents=True)
        (self.root / "scripts").mkdir()
        self.secrets = self.base / "secrets"
        self.secrets.mkdir(mode=0o700)
        self.key_file = self.secrets / "site.key"
        self.ssh_key = self.secrets / "id_ed25519"
        self.ssh_key.write_text("FAKE-SSH-PRIVATE-KEY-MATERIAL", encoding="utf-8")
        os.chmod(self.ssh_key, 0o600)
        self.known_hosts = self.secrets / "github-known-hosts"
        self.known_hosts.write_text(
            "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTKEYONLY\n",
            encoding="utf-8",
        )
        os.chmod(self.known_hosts, 0o644)
        self.workspace = self.base / "workspace"
        self.config_path = self.root / "publish.yaml"
        self.write_entry("2026-11-01-demo")
        self.write_index()
        self.write_config()

    # -- fixtures ---------------------------------------------------------
    def write_entry(self, entry_id: str = "2026-11-01-demo", *, marker: str = "正文") -> None:
        entry = self.root / "library" / "tech" / entry_id / "entry.md"
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(
            "---\n"
            f"id: {entry_id}\n"
            f"title: {ARTICLE_TITLE}\n"
            "category: tech\n"
            "status: archived\n"
            "---\n\n"
            f"# {ARTICLE_TITLE}\n\n## 摘要\n\n摘要内容。\n\n## 全文\n\n{marker}\n",
            encoding="utf-8",
        )

    def write_index(self, rows=None) -> None:
        rows = rows if rows is not None else [{
            "id": "2026-11-01-demo",
            "published": "2026-11-01",
            "title": ARTICLE_TITLE,
            "status": "archived",
            "summary": "摘要内容。",
            "summary_chars": 5,
            "path": "library/tech/2026-11-01-demo/entry.md",
        }]
        path = self.root / "index.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            fields = ["id", "published", "title", "status", "summary", "summary_chars", "path"]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in fields})

    def write_config(self, **overrides) -> Path:
        data = {
            "enabled": False,
            "repository": REPO,
            "branch": "main",
            "site_url": SITE_URL,
            "key_file": str(self.key_file),
            "ssh_key_file": str(self.ssh_key),
            "known_hosts_file": str(self.known_hosts),
            "workspace": str(self.workspace),
            "git_name": TEST_GIT_NAME,
            "git_email": TEST_GIT_EMAIL,
        }
        data.update(overrides)
        for key in [k for k, v in data.items() if v is None]:
            data.pop(key)
        self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
        return self.config_path

    def config(self):
        return ps.load_config(self.root, self.config_path)

    def use_site(self, fake: FakeEncryptedSite) -> FakeEncryptedSite:
        patcher = mock.patch.object(ps, "_load_encrypted_site", return_value=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def snapshot_archive(self):
        digest = hashlib.sha256()
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and ".state" not in path.parts:
                digest.update(str(path.relative_to(self.root)).encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = ps.main(argv)
            except SystemExit as exc:  # argparse
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()


class ConfigTests(PublisherTestCase):
    def test_missing_config_raises(self):
        self.config_path.unlink()
        with self.assertRaises(ps.ConfigError):
            ps.load_config(self.root, self.config_path)

    def test_defaults_when_enabled_omitted(self):
        cfg = self.config()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.repository, REPO)
        self.assertEqual(cfg.branch, "main")
        self.assertEqual(cfg.site_url, SITE_URL)
        self.assertEqual(cfg.key_file, self.key_file)
        self.assertEqual(cfg.workspace, self.workspace)

    def test_rejects_foreign_repository_forms(self):
        bad = [
            f"attacker/{_REPO_NAME}",
            f"{_REPO_OWNER}/Other",
            f"https://github.com/{REPO}",
            f"git@github.com:{REPO}.git",
            f"{REPO}.git",
            f"{REPO}/extra",
            f"{_REPO_OWNER}/../{_REPO_NAME}",
        ]
        for value in bad:
            with self.subTest(repository=value):
                self.write_config(repository=value)
                with self.assertRaises(ps.ConfigError):
                    ps.load_config(self.root, self.config_path)

    def test_rejects_noncanonical_site_url(self):
        bad = [
            f"http://{_SITE_HOST}{SITE_URL.split(_SITE_HOST, 1)[1]}",
            f"https://example.com{SITE_URL.split(_SITE_HOST, 1)[1]}",
            f"https://{_SITE_HOST}/Other/",
            f"{SITE_URL}?x=1",
            f"https://user:pass@{_SITE_HOST}{SITE_URL.split(_SITE_HOST, 1)[1]}",
        ]
        for value in bad:
            with self.subTest(site_url=value):
                self.write_config(site_url=value)
                with self.assertRaises(ps.ConfigError):
                    ps.load_config(self.root, self.config_path)

    def test_site_url_without_trailing_slash_is_normalised(self):
        self.write_config(site_url=SITE_URL.rstrip("/"))
        self.assertEqual(self.config().site_url, SITE_URL)

    def test_rejects_relative_and_in_project_paths(self):
        cases = [
            {"key_file": "relative/site.key"},
            {"workspace": "workspace"},
            {"key_file": str(self.root / "site.key")},
            {"ssh_key_file": str(self.root / "library" / "id_ed25519")},
            {"workspace": str(self.root / "site-work")},
            {"key_file": str(self.workspace / "site.key")},
        ]
        for overrides in cases:
            with self.subTest(**overrides):
                self.write_config(**overrides)
                with self.assertRaises(ps.ConfigError):
                    ps.load_config(self.root, self.config_path)

    def test_rejects_unknown_and_mistyped_keys(self):
        self.write_config(remote_url="git@evil.example:x/y.git")
        with self.assertRaises(ps.ConfigError):
            ps.load_config(self.root, self.config_path)
        self.write_config(enabled="false")
        with self.assertRaises(ps.ConfigError):
            ps.load_config(self.root, self.config_path)

    def test_rejects_symlinked_config(self):
        real = self.base / "outside.yaml"
        shutil.copy(self.config_path, real)
        self.config_path.unlink()
        os.symlink(real, self.config_path)
        with self.assertRaises(ps.ConfigError):
            ps.load_config(self.root, self.config_path)

    def test_branch_rejects_refspec_injection(self):
        for value in ["--force", "main..evil", "refs/heads/main", "main\x00x", ""]:
            with self.subTest(branch=value):
                self.write_config(branch=value)
                with self.assertRaises(ps.ConfigError):
                    ps.load_config(self.root, self.config_path)

    def test_rejects_symlinked_ancestor_without_side_effects(self):
        real = self.base / "real-secrets"
        real.mkdir(mode=0o700)
        os.chmod(real, 0o700)
        alias = self.base / "alias"
        os.symlink(real, alias)
        before = stat.S_IMODE(real.stat().st_mode)
        for overrides in (
            {"key_file": str(alias / "site.key")},
            {"ssh_key_file": str(alias / "id_ed25519")},
            {"known_hosts_file": str(alias / "known-hosts")},
            {"workspace": str(alias / "workspace")},
        ):
            with self.subTest(**overrides):
                self.write_config(**overrides)
                with self.assertRaises(ps.ConfigError):
                    ps.load_config(self.root, self.config_path)
                self.assertFalse((real / "site.key").exists())
                self.assertFalse((real / "workspace").exists())
                self.assertEqual(stat.S_IMODE(real.stat().st_mode), before)

    def test_rejects_workspace_that_contains_the_project(self):
        self.write_config(workspace=str(self.base))
        with self.assertRaises(ps.ConfigError):
            ps.load_config(self.root, self.config_path)

    def test_rejects_workspace_symlink_leaf(self):
        target = self.base / "elsewhere"
        target.mkdir(mode=0o700)
        link = self.base / "workspace-link"
        os.symlink(target, link)
        self.write_config(workspace=str(link))
        with self.assertRaises(ps.ConfigError):
            ps.load_config(self.root, self.config_path)


class MaybePublishTests(PublisherTestCase):
    def test_disabled_skips_without_building_or_reading_key(self):
        fake = self.use_site(FakeEncryptedSite(self.key_file))
        result = ps.maybe_publish(self.root)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(fake.build_calls, 0)
        self.assertFalse(self.workspace.exists())

    def test_env_skip_wins_even_when_enabled(self):
        self.write_config(enabled=True)
        fake = self.use_site(FakeEncryptedSite(self.key_file))
        with mock.patch.dict(os.environ, {"CA_SKIP_AUTO_PUBLISH": "1"}):
            result = ps.maybe_publish(self.root)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(fake.build_calls, 0)

    def test_missing_config_is_skipped(self):
        self.config_path.unlink()
        result = ps.maybe_publish(self.root)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("config", result["reason"].lower())

    def test_invalid_config_returns_failed_instead_of_raising(self):
        self.write_config(repository="attacker/x")
        result = ps.maybe_publish(self.root)
        self.assertEqual(result["status"], "failed")
        self.assertIn("repository", json.dumps(result, ensure_ascii=False).lower())

    def test_enabled_maybe_publish_reports_failure_without_touching_archive(self):
        self.write_config(enabled=True)
        self.key_file.write_text("fake key", encoding="utf-8")
        os.chmod(self.key_file, 0o600)
        fake = self.use_site(FakeEncryptedSite(self.key_file, fail=RuntimeError("boom")))
        before = self.snapshot_archive()
        result = ps.maybe_publish(self.root)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(before, self.snapshot_archive())
        self.assertEqual(fake.build_calls, 1)


class InitTests(PublisherTestCase):
    def test_requires_a_tty(self):
        fake = self.use_site(FakeEncryptedSite(self.key_file))
        with mock.patch.object(ps, "_stdin_is_tty", return_value=False):
            code, out, err = self.run_cli(["init", "--root", str(self.root),
                                           "--config", str(self.config_path)])
        self.assertNotEqual(code, 0)
        self.assertEqual(fake.init_calls, 0)
        self.assertIn("终端", out + err)
        self.assertFalse(self.key_file.exists())

    def test_mismatched_passphrases_create_nothing(self):
        fake = self.use_site(FakeEncryptedSite(self.key_file))
        with mock.patch.object(ps, "_stdin_is_tty", return_value=True), \
                mock.patch.object(ps.getpass, "getpass", side_effect=["a" * 20, "b" * 20]):
            code, out, err = self.run_cli(["init", "--root", str(self.root),
                                           "--config", str(self.config_path)])
        self.assertNotEqual(code, 0)
        self.assertEqual(fake.init_calls, 0)
        self.assertFalse(self.key_file.exists())

    def test_short_passphrase_rejected(self):
        fake = self.use_site(FakeEncryptedSite(self.key_file))
        with mock.patch.object(ps, "_stdin_is_tty", return_value=True), \
                mock.patch.object(ps.getpass, "getpass", side_effect=["short", "short"]):
            code, _, _ = self.run_cli(["init", "--root", str(self.root),
                                       "--config", str(self.config_path)])
        self.assertNotEqual(code, 0)
        self.assertEqual(fake.init_calls, 0)

    def test_init_uses_getpass_and_never_prints_secret(self):
        fake = self.use_site(FakeEncryptedSite(self.key_file))
        secret = "correct-horse-battery-staple-42"
        with mock.patch.object(ps, "_stdin_is_tty", return_value=True), \
                mock.patch.object(ps.getpass, "getpass", side_effect=[secret, secret]):
            code, out, err = self.run_cli(["init", "--root", str(self.root),
                                           "--config", str(self.config_path)])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(fake.init_calls, 1)
        self.assertEqual(fake.passphrase_seen, secret)
        self.assertNotIn(secret, out + err)
        self.assertEqual(stat.S_IMODE(self.key_file.stat().st_mode), 0o600)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ok")
        self.assertNotIn("FAKE-PRIVATE-KEY", out + err)

    def test_cli_passphrase_argument_is_refused_without_echo(self):
        fake = self.use_site(FakeEncryptedSite(self.key_file))
        secret = "leaked-on-command-line-123"
        code, out, err = self.run_cli(["init", "--passphrase", secret,
                                       "--root", str(self.root),
                                       "--config", str(self.config_path)])
        self.assertNotEqual(code, 0)
        self.assertNotIn(secret, out + err)
        self.assertEqual(fake.init_calls, 0)

    def test_environment_passphrase_is_ignored(self):
        self.use_site(FakeEncryptedSite(self.key_file))
        typed = "typed-interactively-strong-1"
        with mock.patch.object(ps, "_stdin_is_tty", return_value=True), \
                mock.patch.object(ps.getpass, "getpass", side_effect=[typed, typed]), \
                mock.patch.dict(os.environ, {"CA_PUBLISH_PASSPHRASE": "env-secret-value"}):
            code, out, err = self.run_cli(["init", "--root", str(self.root),
                                           "--config", str(self.config_path)])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(json.loads(out)["status"], "ok")


class BuildTests(PublisherTestCase):
    def test_build_is_local_only(self):
        fake = self.use_site(FakeEncryptedSite(self.key_file))
        Path(self.key_file).write_text("key", encoding="utf-8")
        os.chmod(self.key_file, 0o600)
        with mock.patch.object(ps, "_run_git", side_effect=AssertionError("git must not run")):
            code, out, err = self.run_cli(["build", "--root", str(self.root),
                                           "--config", str(self.config_path)])
        self.assertEqual(code, 0, out + err)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["entries"], 1)
        self.assertEqual(fake.build_calls, 1)
        self.assertTrue((self.workspace / "build" / "site.json").is_file())
        self.assertFalse((self.root / ".git").exists())

    def test_build_reports_missing_key_without_calling_builder(self):
        fake = self.use_site(FakeEncryptedSite(self.key_file))
        code, out, err = self.run_cli(["build", "--root", str(self.root),
                                       "--config", str(self.config_path)])
        self.assertNotEqual(code, 0)
        self.assertEqual(fake.build_calls, 0)
        self.assertIn("密钥", out + err)

    def test_build_rejects_traversal_and_symlink_entries(self):
        Path(self.key_file).write_text("key", encoding="utf-8")
        os.chmod(self.key_file, 0o600)
        for bad in ("../evil.txt", "/etc/passwd", "objects/evil.md", ".git/config",
                    "notes.txt"):
            with self.subTest(name=bad):
                fake = self.use_site(FakeEncryptedSite(
                    self.key_file, extra_files={bad: "x"}))
                with self.assertRaises(ps.SecurityError):
                    ps.build_only(self.root, self.config(), fake)

    def test_build_rejects_symlink_in_output(self):
        Path(self.key_file).write_text("key", encoding="utf-8")
        os.chmod(self.key_file, 0o600)
        link_name = f"objects/{'a' * 64}.{'b' * 32}.bin"
        fake = FakeEncryptedSite(self.key_file, symlink_name=link_name)
        fake.build_site = self._wrap_with_link(fake)
        with self.assertRaises(ps.SecurityError):
            ps.build_only(self.root, self.config(), fake)

    def _wrap_with_link(self, fake):
        original = fake.build_site

        def build(root, output_dir, key_path):
            result = original(root, output_dir, key_path)
            result["files"] = result["files"] + [fake.symlink_name]
            return result

        return build

    def test_build_rejects_plaintext_site_json_fields(self):
        Path(self.key_file).write_text("key", encoding="utf-8")
        os.chmod(self.key_file, 0o600)
        for extra in ({"title": ARTICLE_TITLE}, {"summary": "明文摘要"},
                      {"entries": [{"canonical_url": "https://example.com/x"}]},
                      {"debug": "/srv/archive"}):
            with self.subTest(extra=extra):
                fake = self.use_site(FakeEncryptedSite(self.key_file, site_json_extra=extra))
                with self.assertRaises(ps.SecurityError):
                    ps.build_only(self.root, self.config(), fake)

    def test_build_requires_core_public_files(self):
        Path(self.key_file).write_text("key", encoding="utf-8")
        os.chmod(self.key_file, 0o600)
        for missing in (".nojekyll", "site.json", "index.html", "reader.js"):
            with self.subTest(missing=missing):
                fake = self.use_site(FakeEncryptedSite(self.key_file, omit=(missing,)))
                with self.assertRaises(ps.SecurityError):
                    ps.build_only(self.root, self.config(), fake)

    def test_build_rejects_json_image_and_unknown_script_even_if_listed(self):
        Path(self.key_file).write_text("key", encoding="utf-8")
        os.chmod(self.key_file, 0o600)
        for name in ("notes.json", "cover.jpg", "extra.html", "assets/app.js"):
            with self.subTest(name=name):
                fake = self.use_site(FakeEncryptedSite(
                    self.key_file, extra_files={name: "x"}))
                with self.assertRaises(ps.SecurityError):
                    ps.build_only(self.root, self.config(), fake)

    def test_build_rejects_site_json_off_protocol(self):
        Path(self.key_file).write_text("key", encoding="utf-8")
        os.chmod(self.key_file, 0o600)

        def set_pair(outer, key, value):
            def mutate(doc):
                doc[outer][key] = value
            return mutate

        cases = {
            "raw_dek": lambda d: d.__setitem__("dek", "SYNTHETIC-DEK"),
            "private_key": lambda d: d.__setitem__("private_key", "SYNTHETIC"),
            "entries": lambda d: d.__setitem__("entries", []),
            "unknown_top": lambda d: d.__setitem__("link", "https://example.com/x"),
            "missing_format": lambda d: d.pop("format"),
            "wrong_format": lambda d: d.__setitem__("format", "other-site"),
            "wrong_protocol": lambda d: d.__setitem__("protocol", "CA-ENC-2"),
            "kdf_extra": set_pair("kdf", "extra", 1),
            "kdf_iterations": set_pair("kdf", "iterations", 1),
            "kdf_salt_short": set_pair("kdf", "salt", base64.b64encode(b"0" * 8).decode()),
            "kdf_salt_plain": set_pair("kdf", "salt", "not-base64!!"),
            "kdf_key_length": set_pair("kdf", "key_length", 16),
            "wrap_extra": set_pair("wrap", "extra", 1),
            "wrap_nonce_short": set_pair("wrap", "nonce", base64.b64encode(b"2" * 8).decode()),
            "wrap_cipher_short": set_pair("wrap", "ciphertext", base64.b64encode(b"3" * 16).decode()),
            "wrap_tag_length": set_pair("wrap", "tag_length", 96),
            "index_extra": set_pair("index", "extra", 1),
            "index_absolute": set_pair("index", "file", "/srv/leak.bin"),
            "index_bad_bytes": set_pair("index", "bytes", True),
            "index_missing": lambda d: d["index"].pop("file"),
            "archive_id_nonhex": lambda d: d.__setitem__("archive_id", "z" * 32),
            "version_short": lambda d: d.__setitem__("version", "v1"),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                fake = self.use_site(FakeEncryptedSite(
                    self.key_file, site_doc_mutator=mutate))
                with self.assertRaises(ps.SecurityError):
                    ps.build_only(self.root, self.config(), fake)

    def test_site_json_index_object_must_be_in_whitelist(self):
        Path(self.key_file).write_text("key", encoding="utf-8")
        os.chmod(self.key_file, 0o600)
        other = f"objects/{'1' * 64}.{'2' * 32}.bin"
        fake = self.use_site(FakeEncryptedSite(
            self.key_file,
            site_doc_mutator=lambda d: d["index"].__setitem__("file", other)))
        with self.assertRaises(ps.SecurityError):
            ps.build_only(self.root, self.config(), fake)


class StatusTests(PublisherTestCase):
    def test_status_reports_known_hosts_readiness(self):
        code, out, err = self.run_cli(["status", "--root", str(self.root),
                                       "--config", str(self.config_path)])
        self.assertEqual(code, 0, out + err)
        payload = json.loads(out)
        self.assertTrue(payload["known_hosts"]["ready"])

    def test_status_reports_readiness_without_key_contents(self):
        secret = "SUPER-SECRET-KEY-BYTES"
        Path(self.key_file).write_text(secret, encoding="utf-8")
        os.chmod(self.key_file, 0o600)
        code, out, err = self.run_cli(["status", "--root", str(self.root),
                                       "--config", str(self.config_path)])
        self.assertEqual(code, 0, out + err)
        payload = json.loads(out)
        self.assertFalse(payload["enabled"])
        self.assertTrue(payload["key"]["ready"])
        self.assertTrue(payload["ssh_key"]["ready"])
        self.assertEqual(payload["repository"], REPO)
        self.assertNotIn(secret, out + err)
        self.assertNotIn(secret, json.dumps(payload, ensure_ascii=False))

    def test_status_flags_loose_key_permissions(self):
        Path(self.key_file).write_text("k", encoding="utf-8")
        os.chmod(self.key_file, 0o644)
        code, out, _ = self.run_cli(["status", "--root", str(self.root),
                                     "--config", str(self.config_path)])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertFalse(payload["key"]["ready"])
        self.assertIn("权限", payload["key"]["note"])


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=check)


class GitTestCase(PublisherTestCase):
    """Real temporary bare repositories; nothing ever reaches github.com."""

    def setUp(self):
        super().setUp()
        self.bare = self.base / "remote.git"
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(self.bare)],
                       check=True, capture_output=True, text=True)
        self.key_file.write_text("fake site key material", encoding="utf-8")
        os.chmod(self.key_file, 0o600)
        self.pages_calls = []
        self.pages_error = None
        self.pages_version = None

    def target(self):
        return ps.GitTarget(url=str(self.bare), env={})

    def fetch_pages(self, url, timeout):
        self.pages_calls.append((url, timeout))
        if self.pages_error is not None:
            raise self.pages_error
        if self.pages_version is not None:
            return {"version": self.pages_version, "archive_id": ARCHIVE_ID}
        data = json.loads((self.workspace / "build" / "site.json").read_text("utf-8"))
        return {"version": data["version"], "archive_id": data["archive_id"]}

    def publish(self, **kwargs):
        kwargs.setdefault("target", self.target())
        kwargs.setdefault("pages_fetch", self.fetch_pages)
        return ps.publish_site(self.root, self.config(), **kwargs)

    def remote_files(self, ref="main"):
        out = git(self.bare, "ls-tree", "-r", "--name-only", "-z", ref).stdout
        return sorted(p for p in out.split("\0") if p)

    def remote_commit(self, ref="main"):
        return git(self.bare, "rev-parse", ref).stdout.strip()

    def remote_commit_count(self):
        out = git(self.bare, "rev-list", "--count", "main").stdout.strip()
        return int(out)

    def state(self):
        return ps.read_state(self.config())

    def seed_foreign_repo(self):
        work = self.base / "foreign"
        work.mkdir()
        git(work, "init", "--initial-branch=main")
        (work / "README.md").write_text("someone else's repo\n", encoding="utf-8")
        git(work, "-c", "user.name=x", "-c", "user.email=x@example.com", "add", "README.md")
        git(work, "-c", "user.name=x", "-c", "user.email=x@example.com",
            "commit", "-m", "initial")
        git(work, "push", str(self.bare), "main")
        return work

    def add_rejecting_hook(self):
        hook = self.bare / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho rejected >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)


class RunTests(GitTestCase):
    def test_publish_commits_only_whitelist_and_verifies_pages(self):
        self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        archive_before = self.snapshot_archive()
        result = self.publish()

        self.assertEqual(result["status"], "success", result)
        self.assertTrue(result["pushed"])
        self.assertTrue(result["verified"])
        self.assertTrue(result["changed"])
        commit = result["commit"]
        self.assertRegex(commit, r"^[0-9a-f]{40,64}$")
        self.assertEqual(self.remote_commit(), commit)
        self.assertEqual(self.remote_files(), sorted(result["files"]))
        self.assertIn("site.json", self.remote_files())
        self.assertIn(".nojekyll", self.remote_files())
        self.assertNotIn("library", self.remote_files())
        self.assertFalse((self.root / ".git").exists())
        self.assertEqual(archive_before, self.snapshot_archive())
        # commit message / author carry no article titles and use configured identity
        message = git(self.bare, "log", "-1", "--format=%B", "main").stdout
        author = git(self.bare, "log", "-1", "--format=%an <%ae>", "main").stdout.strip()
        self.assertNotIn(ARTICLE_TITLE, message)
        self.assertEqual(author, f"{TEST_GIT_NAME} <{TEST_GIT_EMAIL}>")
        self.assertTrue(self.pages_calls)
        url, timeout = self.pages_calls[0]
        self.assertTrue(url.startswith(SITE_URL + "site.json"), url)
        self.assertNotIn("token", url.lower())
        self.assertEqual(timeout, 20.0)
        state = self.state()
        self.assertEqual(state["status"], "success")
        self.assertEqual(state["verified_commit"], commit)
        self.assertEqual(state["verified_version"], result["version"])
        self.assertEqual(state["verified_archive_id"], ARCHIVE_ID)
        self.assertIn("verified_at", state)
        self.assertNotIn("FAKE-PRIVATE-KEY", json.dumps(state))
        self.assertNotIn("fake site key material", json.dumps(state))

    def test_second_identical_build_is_a_noop_without_network(self):
        self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        first = self.publish()
        self.assertEqual(first["status"], "success")
        self.pages_calls.clear()
        second = self.publish()
        self.assertEqual(second["status"], "unchanged", second)
        self.assertFalse(second["pushed"])
        self.assertTrue(second["verified"])
        self.assertEqual(second["commit"], first["commit"])
        self.assertEqual(self.remote_commit_count(), 1)
        self.assertEqual(self.pages_calls, [])

    def test_pages_timeout_records_pushed_pending_and_recovers_later(self):
        self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        self.pages_error = TimeoutError("timed out")
        first = self.publish()
        self.assertEqual(first["status"], "pushed_pending", first)
        self.assertTrue(first["pushed"])
        self.assertFalse(first["verified"])
        self.assertEqual(first["commit"], self.remote_commit())
        self.assertEqual(self.state()["status"], "pushed_pending")

        self.pages_error = None
        second = self.publish()
        self.assertEqual(second["status"], "success", second)
        self.assertFalse(second["pushed"])
        self.assertTrue(second["verified"])
        self.assertEqual(second["commit"], first["commit"])
        self.assertEqual(self.remote_commit_count(), 1)

    def test_version_mismatch_never_reports_online_success(self):
        self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        self.pages_version = "stale-version"
        result = self.publish()
        self.assertEqual(result["status"], "pushed_pending")
        self.assertFalse(result["verified"])
        self.assertIn("version", json.dumps(result, ensure_ascii=False).lower())

    def test_push_failure_keeps_local_commit_and_retry_succeeds(self):
        self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        self.add_rejecting_hook()
        before = self.snapshot_archive()
        with self.assertRaises(ps.PublishError):
            self.publish()
        self.assertEqual(before, self.snapshot_archive())
        repo = self.workspace / "repo"
        local = git(repo, "rev-parse", "HEAD").stdout.strip()
        self.assertRegex(local, r"^[0-9a-f]{40,64}$")
        self.assertNotEqual(git(self.bare, "rev-parse", "--verify", "main", check=False).returncode, 0)
        self.assertEqual(self.state()["status"], "failed")

        (self.bare / "hooks" / "pre-receive").unlink()
        retry = self.publish()
        self.assertEqual(retry["status"], "success", retry)
        self.assertTrue(retry["pushed"])
        self.assertEqual(self.remote_commit(), retry["commit"])

    def test_foreign_nonempty_repository_is_never_overwritten(self):
        self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        self.seed_foreign_repo()
        before = self.remote_commit()
        with self.assertRaises(ps.SecurityError):
            self.publish()
        self.assertEqual(self.remote_commit(), before)
        self.assertEqual(self.remote_files(), ["README.md"])
        self.assertEqual(self.state()["status"], "failed")

    def test_remote_updated_during_publish_aborts_without_force(self):
        self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        original = ps._remote_tip
        calls = {"n": 0, "other": "0" * 40}

        def racing(repo, target, branch):
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            if calls["n"] == 2:
                return calls["other"]
            return original(repo, target, branch)

        with mock.patch.object(ps, "_remote_tip", side_effect=racing):
            with self.assertRaises(ps.PublishError):
                self.publish()
        self.assertNotEqual(git(self.bare, "rev-parse", "--verify", "main", check=False).returncode, 0)
        self.assertEqual(self.state()["status"], "failed")

    def test_symlink_in_export_is_rejected_before_any_git_write(self):
        fake = FakeEncryptedSite(self.key_file, payload="v1")
        original = fake.build_site
        link_name = f"objects/{'c' * 64}.{'d' * 32}.bin"

        def build(root, output_dir, key_path):
            result = original(root, output_dir, key_path)
            link = Path(output_dir) / link_name
            if not link.is_symlink():
                os.symlink("/etc/hostname", link)
            result["files"] = result["files"] + [link_name]
            return result

        fake.build_site = build
        self.use_site(fake)
        with self.assertRaises(ps.SecurityError):
            self.publish()
        self.assertFalse((self.workspace / "repo").exists())

    def test_publish_refuses_unfinished_entries_before_building(self):
        self.write_index(rows=[{
            "id": "2026-11-01-demo", "title": "标题", "status": "needs_review",
            "summary": "", "path": "library/tech/2026-11-01-demo/entry.md",
        }])
        fake = self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        with self.assertRaises(ps.PublishError) as ctx:
            self.publish()
        self.assertIn("2026-11-01-demo", str(ctx.exception))
        self.assertEqual(fake.build_calls, 0)

    def test_publish_lock_is_exclusive(self):
        self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        import fcntl
        self.workspace.mkdir(parents=True, mode=0o700)
        lock = self.workspace / "publish.lock"
        with open(lock, "a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ps.PublishError) as ctx:
                self.publish()
        self.assertIn("另一个发布进程", str(ctx.exception))

    def test_stale_remote_objects_are_removed_by_whitelist_mirror(self):
        fake = self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        first = self.publish()
        stale = next(name for name in first["files"] if name.startswith("objects/"))
        self.assertIn(stale, self.remote_files())
        fake.payload = "v2"
        second = self.publish()
        self.assertEqual(second["status"], "success", second)
        files = self.remote_files()
        self.assertNotIn(stale, files)
        self.assertTrue(any(name.startswith("objects/") for name in files))

    def test_cli_run_reports_machine_readable_success(self):
        self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        with mock.patch.object(ps, "production_target", return_value=self.target()), \
                mock.patch.object(ps, "_fetch_pages_site_json", side_effect=self.fetch_pages):
            code, out, err = self.run_cli(["run", "--root", str(self.root),
                                           "--config", str(self.config_path)])
        self.assertEqual(code, 0, out + err)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["repository"], REPO)
        self.assertEqual(self.remote_commit(), payload["commit"])

    def test_cli_run_failure_is_machine_readable_and_nonzero(self):
        self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        self.seed_foreign_repo()
        with mock.patch.object(ps, "production_target", return_value=self.target()):
            code, out, err = self.run_cli(["run", "--root", str(self.root),
                                           "--config", str(self.config_path)])
        self.assertEqual(code, 1, out + err)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "failed")
        self.assertNotIn(ARTICLE_TITLE, out)


    def test_cli_run_pending_verification_uses_distinct_exit_code(self):
        self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        self.pages_error = TimeoutError("timed out")
        with mock.patch.object(ps, "production_target", return_value=self.target()):
            code, out, err = self.run_cli(["run", "--root", str(self.root),
                                           "--config", str(self.config_path)])
        self.assertEqual(code, 3, out + err)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "pushed_pending")
        self.assertFalse(payload["verified"])
        self.assertTrue(payload["pushed"])

class TransportTests(GitTestCase):
    def test_production_target_pins_dedicated_ssh_and_known_hosts(self):
        target = ps.production_target(self.config())
        self.assertEqual(target.url, f"git@github.com:{REPO}.git")
        command = target.env["GIT_SSH_COMMAND"]
        self.assertIn("-F /dev/null", command)
        self.assertIn("UserKnownHostsFile=" + str(self.known_hosts), command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("IdentitiesOnly=yes", command)
        self.assertIn("BatchMode=yes", command)
        self.assertIn(str(self.ssh_key), command)
        self.assertNotIn("accept-new", command)
        self.assertNotIn("StrictHostKeyChecking=no", command)
        self.assertNotIn("password", command.lower())
        self.assertNotIn("token", command.lower())
        env = ps._git_env(target)
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_AUTHOR_EMAIL"], TEST_GIT_EMAIL)

    def test_run_requires_dedicated_known_hosts(self):
        self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        self.known_hosts.unlink()
        with self.assertRaises(ps.SecurityError) as ctx:
            self.publish()
        self.assertIn("known_hosts", str(ctx.exception))

    def test_known_hosts_requires_exact_github_host_field(self):
        path = self.base / "kh-probe"

        def inspect(text):
            path.write_text(text, encoding="utf-8")
            return ps._inspect_known_hosts(path)[0]

        accepted = [
            "github.com ssh-ed25519 AAAATESTONLY\n",
            "github.com SYNTHETIC-NOT-A-HOST-KEY\n",
            "github.com,140.82.112.3 ssh-rsa AAAATESTONLY\n",
            "# github.com comment only\n\ngithub.com ssh-ed25519 AAAATESTONLY\n",
            "@cert-authority github.com ssh-ed25519 AAAATESTONLY\n",
            "[github.com]:22 ssh-ed25519 AAAATESTONLY\n",
        ]
        for text in accepted:
            with self.subTest(accept=text.splitlines()[0]):
                self.assertTrue(inspect(text))
        rejected = [
            "github.com.fake.example ssh-ed25519 AAAATESTONLY\n",
            "evilgithub.com ssh-ed25519 AAAATESTONLY\n",
            "github.com.evil ssh-ed25519 AAAATESTONLY\n",
            "|1|AAAA|BBBB ssh-ed25519 AAAATESTONLY\n",
            "# github.com\n",
            "@revoked github.com ssh-ed25519 AAAATESTONLY\n",
            "example.com ssh-ed25519 AAAATESTONLY\n",
        ]
        for text in rejected:
            with self.subTest(reject=text.splitlines()[0]):
                self.assertFalse(inspect(text))

    def test_known_hosts_note_never_echoes_key_material(self):
        path = self.base / "kh-secret"
        path.write_text("github.com ssh-ed25519 SUPERSECRETKEYMATERIAL\n",
                        encoding="utf-8")
        ready, note = ps._inspect_known_hosts(path)
        self.assertTrue(ready)
        self.assertNotIn("SUPERSECRETKEYMATERIAL", note)

    def test_git_env_drops_inherited_redirection_and_config(self):
        target = self.target()
        injected = {
            "GIT_DIR": "/tmp/evil",
            "GIT_WORK_TREE": "/tmp/evil",
            "GIT_INDEX_FILE": "/tmp/evil/index",
            "GIT_OBJECT_DIRECTORY": "/tmp/evil/objects",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/tmp/evil/alt",
            "GIT_CONFIG": "/tmp/evil/config",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/tmp/evil/hooks",
            "GIT_NAMESPACE": "evil",
        }
        with mock.patch.dict(os.environ, injected):
            env = ps._git_env(target)
        for key in injected:
            self.assertNotIn(key, env)
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertIn("PATH", env)

    def test_git_env_refuses_target_redirection_keys(self):
        bad = ps.GitTarget(url=str(self.bare), env={"GIT_DIR": "/tmp/evil"})
        with self.assertRaises(ps.SecurityError):
            ps._git_env(bad)

    def test_push_command_never_contains_force_or_credentials(self):
        target = self.target()
        with mock.patch.object(ps, "_run_git") as run:
            ps._push(self.workspace, target, "main", "a" * 40)
        args = run.call_args[0][1]
        self.assertEqual(args[:2], ["push", "--porcelain"])
        self.assertNotIn("--force", args)
        self.assertNotIn("-f", args)
        self.assertNotIn("token", " ".join(args).lower())

    def test_maybe_publish_success_uses_injected_transport_and_pages_stub(self):
        self.write_config(enabled=True)
        self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        with mock.patch.object(ps, "production_target", return_value=self.target()), \
                mock.patch.object(ps, "_fetch_pages_site_json", side_effect=self.fetch_pages):
            result = ps.maybe_publish(self.root)
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(self.remote_commit(), result["commit"])



class RemoteGuardTests(GitTestCase):
    def seed_managed_with_symlink(self):
        work = self.base / "managed"
        work.mkdir()
        git(work, "init", "--initial-branch=main")
        (work / "index.html").write_text("<html></html>", encoding="utf-8")
        (work / ".nojekyll").write_text("", encoding="utf-8")
        (work / "site.json").write_text(json.dumps({
            "version": "v0", "archive_id": ARCHIVE_ID,
            "wrapped_key": {"nonce": "AA", "ciphertext": "BB"},
        }), encoding="utf-8")
        git(work, "-c", "user.name=x", "-c", "user.email=x@example.com",
            "add", "index.html", ".nojekyll", "site.json")
        blob = subprocess.run(["git", "-C", str(work), "hash-object", "-w", "--stdin"],
                              input="../../etc/passwd", capture_output=True, text=True,
                              check=True).stdout.strip()
        git(work, "update-index", "--add", "--cacheinfo", f"120000,{blob},objects/link")
        git(work, "-c", "user.name=x", "-c", "user.email=x@example.com",
            "commit", "-m", "managed")
        git(work, "push", str(self.bare), "main")

    def test_remote_symlink_blob_is_rejected(self):
        self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        self.seed_managed_with_symlink()
        before = self.remote_commit()
        with self.assertRaises(ps.SecurityError) as ctx:
            self.publish()
        self.assertIn("非普通文件", str(ctx.exception))
        self.assertEqual(self.remote_commit(), before)

    def test_credential_bearing_origin_is_rejected(self):
        self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        repo = self.workspace / "repo"
        repo.mkdir(parents=True, mode=0o700)
        git(repo, "init", "--initial-branch=main")
        git(repo, "remote", "add", "origin",
            "https://x-access-token:secret-token@github.com/example/site.git")
        with self.assertRaises(ps.SecurityError) as ctx:
            self.publish()
        self.assertIn("origin", str(ctx.exception))
        self.assertNotIn("secret", str(ctx.exception))

    def test_hidden_directory_in_export_is_rejected(self):
        self.use_site(FakeEncryptedSite(
            self.key_file, payload="v1",
            extra_files={".hidden/data.bin": "x"}))
        with self.assertRaises(ps.SecurityError):
            self.publish()

    def test_site_json_url_value_is_rejected(self):
        self.use_site(FakeEncryptedSite(
            self.key_file, payload="v1",
            site_json_extra={"link": "https://mp.weixin.qq.com/s/leak"}))
        with self.assertRaises(ps.SecurityError):
            self.publish()


class UnconfiguredStatusTests(PublisherTestCase):
    def test_status_works_without_publish_yaml(self):
        self.config_path.unlink()
        code, out, err = self.run_cli(["status", "--root", str(self.root)])
        self.assertEqual(code, 0, out + err)
        payload = json.loads(out)
        self.assertFalse(payload["configured"])
        self.assertFalse(payload["enabled"])


class IndexHtmlGuardTests(GitTestCase):
    """The published index.html must be an empty encrypted shell, not plaintext."""

    def test_guard_accepts_encrypted_shell_without_favicon(self):
        ps._validate_encrypted_index_html(
            index_without_favicon().encode("utf-8"), "index.html"
        )

    def test_guard_rejects_plaintext_offline_page(self):
        offline = (
            '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
            f"<title>{ARTICLE_TITLE}</title></head><body>"
            '<script id="archive-data" type="application/json">'
            + json.dumps([{"title": ARTICLE_TITLE}])
            + "</script>"
            '<script id="labels-data" type="application/json">{}</script>'
            "</body></html>"
        )
        with self.assertRaises(ps.SecurityError):
            ps._validate_encrypted_index_html(offline.encode("utf-8"), "index.html")

    def test_guard_rejects_bad_data_scripts(self):
        cases = {
            "archive-data missing": ENCRYPTED_INDEX_HTML.replace(
                'id="archive-data"', 'id="archive-data-renamed"'),
            "labels-data missing": ENCRYPTED_INDEX_HTML.replace(
                'id="labels-data"', 'id="labels-data-renamed"'),
            "duplicate archive-data": ENCRYPTED_INDEX_HTML.replace(
                '<script id="labels-data"',
                '<script id="archive-data" type="application/json">[]</script>'
                '<script id="labels-data"'),
            "non-empty archive-data": ENCRYPTED_INDEX_HTML.replace(
                "[]\n</script>", '[{"title": "' + ARTICLE_TITLE + '"}]\n</script>'),
            "non-empty labels-data": ENCRYPTED_INDEX_HTML.replace(
                "{}\n</script>", '{"coding": "编程与开发"}\n</script>'),
            "broken json": ENCRYPTED_INDEX_HTML.replace("[]\n</script>", "[,]\n</script>"),
            "wrong type": ENCRYPTED_INDEX_HTML.replace(
                '<script id="archive-data" type="application/json">',
                '<script id="archive-data" type="text/plain">'),
            "marker missing": ENCRYPTED_INDEX_HTML.replace(
                ' data-ca-encrypted="1"', ""),
        }
        for label, html in cases.items():
            with self.subTest(label):
                with self.assertRaises(ps.SecurityError):
                    ps._validate_encrypted_index_html(html.encode("utf-8"), "index.html")

    def test_guard_rejects_duplicate_attributes(self):
        # Browsers keep the first attribute; dict(attrs) keeps the last. Reject
        # ambiguity rather than letting a later id conceal plaintext data.
        cases = {
            "marker": ENCRYPTED_INDEX_HTML.replace(
                'data-ca-encrypted="1"',
                'data-ca-encrypted="0" data-ca-encrypted="1"'),
            "id": ENCRYPTED_INDEX_HTML.replace(
                '<script id="archive-data"',
                '<script id="archive-data" id="ignored" type="application/json">'
                '[{"title":"SYNTHETIC"}]</script><script id="archive-data"'),
            "type": ENCRYPTED_INDEX_HTML.replace(
                'id="archive-data" type="application/json"',
                'id="archive-data" type="text/plain" type="application/json"'),
        }
        for label, html in cases.items():
            with self.subTest(label):
                with self.assertRaises(ps.SecurityError):
                    ps._validate_encrypted_index_html(html)

    def test_plaintext_index_in_export_is_rejected_before_git(self):
        offline = ENCRYPTED_INDEX_HTML.replace(
            "[]\n</script>", '[{"title": "' + ARTICLE_TITLE + '"}]\n</script>')
        self.use_site(FakeEncryptedSite(
            self.key_file, extra_files={"index.html": offline}))
        with self.assertRaises(ps.SecurityError):
            self.publish()
        probe = git(self.bare, "rev-parse", "--verify", "main", check=False)
        self.assertNotEqual(probe.returncode, 0)

    def test_legacy_index_without_favicon_still_publishes(self):
        self.use_site(FakeEncryptedSite(
            self.key_file, extra_files={"index.html": index_without_favicon()}))
        result = self.publish()
        self.assertEqual(result["status"], "success", result)


class HistoryAuditTests(GitTestCase):
    """Unmanaged local history must never become public via a push."""

    def seed_repo(self):
        repo = self.workspace / "repo"
        repo.mkdir(parents=True, mode=0o700)
        git(repo, "init", "--initial-branch=main")
        git(repo, "config", "user.name", "seed")
        git(repo, "config", "user.email", "seed@example.invalid")
        return repo

    def commit_all(self, repo, message="seed"):
        git(repo, "add", "-A")
        git(repo, "commit", "-m", message)

    def assert_remote_unpublished(self):
        probe = git(self.bare, "rev-parse", "--verify", "main", check=False)
        self.assertNotEqual(probe.returncode, 0)

    def test_preseeded_plaintext_parent_commit_is_rejected(self):
        repo = self.seed_repo()
        (repo / "private-notes.md").write_text(
            "SYNTHETIC-PLAINTEXT-HISTORY-MARKER", encoding="utf-8")
        self.commit_all(repo, "private notes")
        self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        with self.assertRaises(ps.SecurityError):
            self.publish()
        self.assert_remote_unpublished()
        self.assertEqual(self.state()["status"], "failed")

    def test_clean_head_with_dirty_ancestor_is_rejected(self):
        repo = self.seed_repo()
        (repo / "private-notes.md").write_text(
            "SYNTHETIC-PLAINTEXT-HISTORY-MARKER", encoding="utf-8")
        self.commit_all(repo, "private notes")
        git(repo, "rm", "private-notes.md")
        self.commit_all(repo, "remove notes")
        self.assertEqual(git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout, "")
        self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        with self.assertRaises(ps.SecurityError):
            self.publish()
        self.assert_remote_unpublished()

    def test_preseeded_foreign_archive_history_is_rejected(self):
        repo = self.seed_repo()
        fake = self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        built = ps.build_only(self.root, self.config(), fake)
        build_dir = self.workspace / "build"
        for name in built["files"]:
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(build_dir / name, target)
        site_path = repo / "site.json"
        site = json.loads(site_path.read_text("utf-8"))
        site["archive_id"] = "f" * 32
        site_path.write_text(json.dumps(site), encoding="utf-8")
        self.commit_all(repo, "foreign site history")
        with self.assertRaises(ps.SecurityError):
            self.publish()
        self.assert_remote_unpublished()

    def test_head_tree_must_equal_build_files(self):
        repo = self.seed_repo()
        fake = self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        built = ps.build_only(self.root, self.config(), fake)
        build_dir = self.workspace / "build"
        for name in built["files"]:
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(build_dir / name, target)
        extra = f"objects/{'e' * 64}.{'f' * 32}.bin"
        (repo / extra).write_text("stale-object", encoding="utf-8")
        self.commit_all(repo, "head with extra file")
        with mock.patch.object(ps, "_sync_worktree"), \
                mock.patch.object(ps, "_staged_changes", return_value=False):
            with self.assertRaises(ps.SecurityError):
                self.publish()
        self.assert_remote_unpublished()

    def test_dirty_index_in_closed_set_ancestor_blocks_push(self):
        repo = self.seed_repo()
        fake = self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        built = ps.build_only(self.root, self.config(), fake)
        build_dir = self.workspace / "build"
        for name in built["files"]:
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(build_dir / name, target)
        # The ancestor has the closed file set and a valid site.json, but its
        # index.html carries plaintext archive data instead of an empty shell.
        dirty = ENCRYPTED_INDEX_HTML.replace(
            "[]\n</script>",
            json.dumps([{"title": ARTICLE_TITLE}]) + "\n</script>",
        )
        (repo / "index.html").write_text(dirty, encoding="utf-8")
        self.commit_all(repo, "dirty index history")
        # Restore a clean HEAD; the dirty ancestor must still block the push.
        (repo / "index.html").write_text(ENCRYPTED_INDEX_HTML, encoding="utf-8")
        self.commit_all(repo, "clean head")
        with self.assertRaises(ps.SecurityError):
            self.publish()
        self.assert_remote_unpublished()

    def test_failed_push_retry_with_changed_content_succeeds(self):
        fake = self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        self.add_rejecting_hook()
        with self.assertRaises(ps.PublishError):
            self.publish()
        self.assert_remote_unpublished()
        (self.bare / "hooks" / "pre-receive").unlink()
        fake.payload = "v2"
        retry = self.publish()
        self.assertEqual(retry["status"], "success", retry)
        self.assertTrue(retry["pushed"])
        self.assertEqual(self.remote_commit(), retry["commit"])
        self.assertEqual(self.remote_commit_count(), 2)
        self.assertEqual(self.remote_files(), sorted(retry["files"]))


class RecoveryTests(GitTestCase):
    """A transient network failure must not bury a verified publish."""

    def test_pending_then_transport_failure_retry_reverifies(self):
        self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        self.pages_error = TimeoutError("timed out")
        first = self.publish()
        self.assertEqual(first["status"], "pushed_pending", first)

        with mock.patch.object(ps, "_remote_tip",
                               side_effect=ps.PublishError("simulated transport failure")):
            with self.assertRaises(ps.PublishError):
                self.publish()
        self.assertEqual(self.state()["status"], "failed")

        self.pages_error = None
        self.pages_calls.clear()
        third = self.publish()
        self.assertEqual(third["status"], "success", third)
        self.assertTrue(third["verified"])
        self.assertFalse(third["pushed"])
        self.assertEqual(third["commit"], first["commit"])
        self.assertEqual(self.remote_commit_count(), 1)
        self.assertTrue(self.pages_calls)

    def test_missing_state_is_reverified_instead_of_unchanged(self):
        self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        first = self.publish()
        self.assertEqual(first["status"], "success")
        ps._state_path(self.config()).unlink()
        self.pages_calls.clear()
        second = self.publish()
        self.assertEqual(second["status"], "success", second)
        self.assertTrue(second["verified"])
        self.assertEqual(second["commit"], first["commit"])
        self.assertEqual(self.remote_commit_count(), 1)
        self.assertTrue(self.pages_calls)

    def test_mismatched_verified_credential_is_reverified(self):
        self.use_site(FakeEncryptedSite(self.key_file, payload="v1"))
        first = self.publish()
        self.assertEqual(first["status"], "success")
        config = self.config()
        state = ps.read_state(config)
        state["version"] = "0" * 32
        Path(ps._state_path(config)).write_text(
            json.dumps(state), encoding="utf-8")
        self.pages_calls.clear()
        second = self.publish()
        self.assertEqual(second["status"], "success", second)
        self.assertTrue(second["verified"])
        self.assertEqual(second["commit"], first["commit"])
        self.assertTrue(self.pages_calls)


class RealEncryptedIntegrationTests(unittest.TestCase):
    """Build and publish the real encrypted exporter into a local bare repo."""

    def setUp(self):
        if str(ROOT / "tests") not in sys.path:
            sys.path.insert(0, str(ROOT / "tests"))
        from test_encrypted_site import EncryptedSiteBase

        self.fixture = EncryptedSiteBase()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.base = self.fixture.base
        self.root = self.fixture.root
        self.key = self.fixture.init_key()
        secrets = self.base / "publish-secrets"
        secrets.mkdir(mode=0o700)
        self.ssh_key = secrets / "id_ed25519"
        self.ssh_key.write_text("SYNTHETIC-NOT-A-CREDENTIAL", encoding="utf-8")
        os.chmod(self.ssh_key, 0o600)
        self.known_hosts = secrets / "known-hosts"
        self.known_hosts.write_text(
            "github.com ssh-ed25519 AAAATESTONLY\n", encoding="utf-8")
        self.workspace = self.base / "publish-workspace"
        self.config = ps.PublishConfig(
            enabled=False,
            repository=REPO,
            branch="main",
            site_url=SITE_URL,
            key_file=self.key,
            ssh_key_file=self.ssh_key,
            known_hosts_file=self.known_hosts,
            workspace=self.workspace,
            git_name="test",
            git_email="test@example.invalid",
            pages_timeout_seconds=2.0,
            config_path=self.root / "publish.yaml",
        )

    def test_real_exporter_output_passes_strict_publisher_validation(self):
        result = ps.build_only(self.root, self.config)
        self.assertEqual(result["status"], "ok")
        files = result["files"]
        self.assertIn("reader.js", files)
        self.assertIn("site.json", files)
        objects = [name for name in files if name.startswith("objects/")]
        self.assertTrue(objects)
        self.assertRegex(objects[0],
                         r"^objects/[0-9a-f]{64}\.[0-9a-f]{32}\.bin$")
        build_dir = self.workspace / "build"
        needles = (b"ALPHANEEDLE", b"SECRETURL", "加密标题甲".encode("utf-8"))
        for name in files:
            data = (build_dir / name).read_bytes()
            for needle in needles:
                self.assertNotIn(needle, data, name)

    def test_real_offline_html_is_rejected_as_published_index(self):
        from encrypted_site import build_html

        built = ps.build_only(self.root, self.config)
        build_dir = self.workspace / "build"
        offline = build_html(self.root, self.fixture.rows, {})
        (build_dir / "index.html").write_text(offline, encoding="utf-8")
        with self.assertRaises(ps.SecurityError):
            ps.validate_whitelist(build_dir, built["files"])

    def test_real_build_pushes_only_whitelist_to_bare_repo(self):
        bare = self.base / "remote.git"
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(bare)],
                       check=True, capture_output=True, text=True)

        def pages_fetch(url, timeout):
            return json.loads((self.workspace / "build" / "site.json").read_text("utf-8"))

        result = ps.publish_site(
            self.root, self.config,
            target=ps.GitTarget(url=str(bare), env={}),
            pages_fetch=pages_fetch,
        )
        self.assertEqual(result["status"], "success", result)
        self.assertTrue(result["verified"])
        remote = git(bare, "ls-tree", "-r", "--name-only", "-z", "main").stdout
        self.assertEqual(sorted(p for p in remote.split("\0") if p),
                         sorted(result["files"]))
        self.assertEqual(git(bare, "rev-list", "--count", "main").stdout.strip(), "1")


if __name__ == "__main__":
    unittest.main()
