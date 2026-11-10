#!/usr/bin/env python3
"""Comprehensive unit and integration tests for Phase 3B.

Covers:
- SSRF prevention (assert_public_url, safe_fetch, hop-by-hop redirects, 127.0.0.1 fixture)
- Path verification (--content-file, --raw-file, allowed directories, 5MB limits, UTF-8)
- Finalize atomicity (tmp+os.replace, os.rename, destination conflict handling)
- Seen self-healing & backups (.bak on save, recovery on load, no empty dict on failure)
- Reconcile seen & snapshots (scripts/reconcile_seen.py, bin/ca reconcile-seen)
- Audit seen ↔ library reconciliation (missing keys, mismatched paths, orphans)
- Storage & image quotas (single image 2MB, total images 20MB, image_note)
"""
from __future__ import annotations

import gzip
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))

import common
from common import (
    assert_public_url,
    count_summary_chars,
    dedupe_key,
    is_allowed_file_path,
    load_seen,
    read_entry,
    safe_fetch,
    save_seen,
    seen_bak_path,
    seen_path,
    write_entry,
    write_json_atomic,
)
from reconcile_seen import reconcile_seen, snapshot_seen
from media_policy import MediaBudget

# Legacy storage-limit probes keep their original 2 MiB / 20 MiB boundaries by
# passing an explicit budget; production defaults are now 10 / 50 MiB.
LEGACY_IMAGE_BUDGET = MediaBudget(
    max_images=30,
    max_single_image_bytes=2 * 1024 * 1024,
    max_total_image_bytes=20 * 1024 * 1024,
    max_download_seconds=180.0,
)


class LocalTestServer:
    """Threaded local HTTP server for testing SSRF and redirect defenses."""

    def __init__(self, routes: dict[str, tuple[int, dict[str, str], bytes]] | None = None):
        self.routes = routes or {}
        self.requests_log: list[str] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests_log.append(self.path)
                if self.path in outer.routes:
                    code, headers, body = outer.routes[self.path]
                    self.send_response(code)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        # Clients are allowed to stop reading early (size caps).
                        pass
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    try:
                        self.wfile.write(b"OK default")
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            def log_message(self, format, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()


class TestSSRFProtection(unittest.TestCase):
    def test_assert_public_url_blocks_private_and_special_ips(self):
        blocked_urls = [
            "http://127.0.0.1",
            "http://127.0.0.1:8080/path",
            "http://localhost",
            "http://10.0.0.1/private",
            "http://172.16.0.1/private",
            "http://192.168.1.1/admin",
            "http://169.254.169.254/latest/meta-data/",
            "http://100.64.0.1/cgnat",
            "http://0.0.0.0",
            "http://224.0.0.1/multicast",
            "http://240.0.0.1/reserved",
            "http://[::1]/ipv6_loopback",
            "http://[fd00::1]/ipv6_ula",
            "http://[fe80::1]/ipv6_link_local",
            "http://[::ffff:127.0.0.1]/mapped",
        ]
        for u in blocked_urls:
            with self.subTest(url=u):
                with self.assertRaises(ValueError, msg=f"Should block {u}"):
                    assert_public_url(u)

    def test_assert_public_url_blocks_non_http_schemes(self):
        non_http = [
            "file:///etc/passwd",
            "ftp://example.com/file",
            "gopher://example.com",
            "data:text/plain;base64,SGVsbG8=",
            "",
            "   ",
        ]
        for u in non_http:
            with self.subTest(url=u):
                with self.assertRaises(ValueError):
                    assert_public_url(u)

    def test_assert_public_url_allows_public_domain(self):
        assert_public_url("https://example.com")
        assert_public_url("https://openai.com/index")

    def test_archive_url_cli_blocks_127_0_0_1_fixture(self):
        srv = LocalTestServer()
        try:
            target_url = srv.url("/sensitive")
            py = sys.executable
            cmd = [py, str(ROOT / "scripts" / "archive_url.py"), target_url]
            res = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(res.returncode, 1)
            self.assertEqual(len(srv.requests_log), 0, "Server must receive 0 requests")
            self.assertIn("SSRF blocked", res.stdout)
        finally:
            srv.shutdown()

    def test_archive_url_cli_blocks_metadata_ip(self):
        py = sys.executable
        cmd = [py, str(ROOT / "scripts" / "archive_url.py"), "http://169.254.169.254/latest/meta-data/"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("SSRF blocked", res.stdout)

    def test_safe_fetch_hop_by_hop_redirect_to_private_blocked(self):
        srv = LocalTestServer({
            "/redirect_to_loopback": (302, {"Location": "http://127.0.0.1:9999/evil"}, b""),
        })
        try:
            with self.assertRaises(ValueError):
                safe_fetch(srv.url("/redirect_to_loopback"))
        finally:
            srv.shutdown()


class TestPathVerification(unittest.TestCase):
    def setUp(self):
        self.scratch = _support.scratch_dir("p3b_test_path_")

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_is_allowed_file_path(self):
        self.assertTrue(is_allowed_file_path(Path("/tmp/foo.txt")))
        self.assertTrue(is_allowed_file_path(self.scratch / "file.txt"))
        self.assertTrue(is_allowed_file_path(ROOT / ".state" / "pending.json"))
        self.assertFalse(is_allowed_file_path(Path("/etc/hostname")))
        self.assertFalse(is_allowed_file_path(Path("/etc/passwd")))
        self.assertFalse(is_allowed_file_path(ROOT / "sources.yaml"))

    def test_content_file_outside_allowed_roots_rejected(self):
        # The forbidden file is synthetic and exists: a project file outside
        # .state/ in an isolated copied project. Relying on a host system file
        # (e.g. /etc/hostname) makes the refusal unreachable on hosts where that
        # file is absent, because the CLI reports "not found" first.
        sandbox = _support.archive_project("p3b_test_cf_forbidden_", state=True)
        self.addCleanup(shutil.rmtree, sandbox, ignore_errors=True)
        forbidden = sandbox / "forbidden-content.txt"
        forbidden_text = "Project file outside .state must never be accepted."
        forbidden.write_text(forbidden_text, encoding="utf-8")
        self.assertTrue(forbidden.is_file())
        py = sys.executable
        cmd = [
            py,
            str(sandbox / "scripts" / "archive_url.py"),
            "https://example.com/post",
            "--content-file",
            str(forbidden),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("content-file path not allowed", res.stdout)
        self.assertEqual(forbidden.read_text(encoding="utf-8"), forbidden_text)

    def test_content_file_not_found(self):
        py = sys.executable
        cmd = [
            py,
            str(ROOT / "scripts" / "archive_url.py"),
            "https://example.com/post",
            "--content-file",
            "/tmp/definitely_not_exist_p3b.txt",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("content-file not found", res.stdout)

    def test_content_file_exceeds_5mb(self):
        big_file = self.scratch / "too_big.txt"
        big_file.write_bytes(b"a" * (5 * 1024 * 1024 + 10))
        py = sys.executable
        cmd = [
            py,
            str(ROOT / "scripts" / "archive_url.py"),
            "https://example.com/post",
            "--content-file",
            str(big_file),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("exceeds 5MB limit", res.stdout)

    def test_content_file_non_utf8_rejected(self):
        bad_utf8 = self.scratch / "bad_utf8.txt"
        bad_utf8.write_bytes(b"\xff\xfe\x00\x00\x80\x81\x82")
        py = sys.executable
        cmd = [
            py,
            str(ROOT / "scripts" / "archive_url.py"),
            "https://example.com/post",
            "--content-file",
            str(bad_utf8),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("UTF-8 decode failed", res.stdout)

    def test_raw_file_outside_allowed_roots_rejected(self):
        # Same rationale as the content-file case: the forbidden raw snapshot is
        # a synthetic project file outside .state/, so the refusal is exercised
        # on every host instead of depending on a system file being present.
        sandbox = _support.archive_project("p3b_test_raw_forbidden_", state=True)
        self.addCleanup(shutil.rmtree, sandbox, ignore_errors=True)
        forbidden = sandbox / "forbidden-raw.html"
        forbidden_text = "<html><body>forbidden raw snapshot</body></html>"
        forbidden.write_text(forbidden_text, encoding="utf-8")
        self.assertTrue(forbidden.is_file())
        valid_cf = self.scratch / "content.txt"
        valid_cf.write_text("Valid markdown content for test article.", encoding="utf-8")
        py = sys.executable
        cmd = [
            py,
            str(sandbox / "scripts" / "archive_url.py"),
            "https://example.com/post",
            "--content-file",
            str(valid_cf),
            "--raw-file",
            str(forbidden),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("raw-file path not allowed", res.stdout)
        self.assertEqual(forbidden.read_text(encoding="utf-8"), forbidden_text)

    def test_raw_file_exceeds_5mb_rejected(self):
        valid_cf = self.scratch / "content.txt"
        valid_cf.write_text("Valid markdown content.", encoding="utf-8")
        big_raw = self.scratch / "big_raw.html.gz"
        big_raw.write_bytes(b"x" * (5 * 1024 * 1024 + 20))
        py = sys.executable
        cmd = [
            py,
            str(ROOT / "scripts" / "archive_url.py"),
            "https://example.com/post",
            "--content-file",
            str(valid_cf),
            "--raw-file",
            str(big_raw),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("raw-file exceeds 5MB limit", res.stdout)


class TestFinalizeAtomicity(unittest.TestCase):
    def setUp(self):
        self.scratch = _support.archive_project("p3b_test_fin_", state=True)
        self.lib = self.scratch / "library"
        self.state = self.scratch / ".state"
        self.state.mkdir(parents=True, exist_ok=True)
        self.lib.mkdir(parents=True, exist_ok=True)

        self.tech_dir = self.lib / "tech" / "2026-10-27-atomicity-test"
        self.tech_dir.mkdir(parents=True, exist_ok=True)
        self.entry_path = self.tech_dir / "entry.md"

        self.summary_text = "这是一段两百字左右的测试中文摘要。用于验证终审脚本的原子性目录迁移和元数据写入逻辑。代码应首先在原目录下以临时文件方式写入条目文件，再通过操作系统重命名原子性迁移到新分类目录，最后更新去重索引。"
        self.fm = {
            "id": "2026-10-27-atomicity-test",
            "title": "测试原始标题",
            "category": "tech",
            "status": "archived",
            "context": "work",
            "summary_chars": count_summary_chars(self.summary_text),
            "url": "https://example.com/atomicity-test",
            "canonical_url": "https://example.com/atomicity-test",
        }
        self.body = f"# 测试原始标题\n\n> 来源：...\n\n## 摘要\n\n{self.summary_text}\n\n## 全文\n\n正文详细内容。" + "更多内容" * 30
        write_entry(self.entry_path, self.fm, self.body)

        self.seen_file = self.state / "seen.json"
        save_seen({
            "entries": {
                "https://example.com/atomicity-test": {
                    "id": "2026-10-27-atomicity-test",
                    "path": f"library/tech/2026-10-27-atomicity-test/entry.md",
                    "first_seen": "2026-10-27T10:00:00+08:00",
                }
            }
        }, self.seen_file)

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_finalize_moves_category_atomically(self):
        py = sys.executable
        cmd = [
            py,
            str(self.scratch / "scripts" / "finalize_entry.py"),
            "--path",
            str(self.entry_path),
            "--category",
            "coding",
            "--title-zh",
            "测试原子重命名标题",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

        # Old directory must no longer exist
        self.assertFalse(self.tech_dir.exists())

        # New directory must exist in coding/
        expected_new_dir = self.lib / "coding" / "2026-10-27-atomicity-test"
        self.assertTrue(expected_new_dir.exists())
        new_entry = expected_new_dir / "entry.md"
        self.assertTrue(new_entry.exists())

        # Check content and frontmatter
        saved_fm, saved_body = read_entry(new_entry)
        self.assertEqual(saved_fm.get("category"), "coding")
        self.assertEqual(saved_fm.get("title"), "测试原子重命名标题")

        # Check seen was updated
        seen = load_seen(self.seen_file)
        seen_entry = seen["entries"]["https://example.com/atomicity-test"]
        self.assertEqual(seen_entry["path"], "library/coding/2026-10-27-atomicity-test/entry.md")

    def test_finalize_destination_exists_fails_without_overwriting(self):
        conflict_dir = self.lib / "coding" / "2026-10-27-atomicity-test"
        conflict_dir.mkdir(parents=True, exist_ok=True)
        existing_marker = conflict_dir / "existing_file.txt"
        existing_marker.write_text("existing canary", encoding="utf-8")

        py = sys.executable
        cmd = [
            py,
            str(self.scratch / "scripts" / "finalize_entry.py"),
            "--path",
            str(self.entry_path),
            "--category",
            "coding",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("target exists", res.stdout)

        # Existing canary must be untouched
        self.assertTrue(existing_marker.exists())
        self.assertEqual(existing_marker.read_text(encoding="utf-8"), "existing canary")

        # Original source directory must remain intact
        self.assertTrue(self.tech_dir.exists())


class TestSeenSelfHealingAndBackup(unittest.TestCase):
    def setUp(self):
        self.scratch = _support.scratch_dir("p3b_test_seen_")
        self.seen_file = self.scratch / "seen.json"
        self.bak_file = self.scratch / "seen.json.bak"

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_save_seen_creates_bak_of_previous_state(self):
        data1 = {"entries": {"k1": {"id": "1"}}}
        save_seen(data1, self.seen_file)
        self.assertTrue(self.seen_file.exists())
        self.assertFalse(self.bak_file.exists())

        data2 = {"entries": {"k1": {"id": "1"}, "k2": {"id": "2"}}}
        save_seen(data2, self.seen_file)
        self.assertTrue(self.bak_file.exists())
        bak_data = json.loads(self.bak_file.read_text(encoding="utf-8"))
        self.assertEqual(bak_data, data1)

    def test_load_seen_recovers_from_bak_on_corruption(self):
        valid_bak = {"entries": {"https://example.com/saved": {"id": "saved-1"}}}
        self.bak_file.write_text(json.dumps(valid_bak), encoding="utf-8")
        self.seen_file.write_text("{corrupt json !!!", encoding="utf-8")

        loaded = load_seen(self.seen_file)
        self.assertEqual(loaded, valid_bak)

    def test_load_seen_raises_when_no_valid_backup(self):
        # 1. Corrupt seen with corrupt backup
        self.seen_file.write_text("{corrupt", encoding="utf-8")
        self.bak_file.write_text("{also corrupt", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_seen(self.seen_file)

        # 2. Corrupt seen with missing backup
        self.bak_file.unlink()
        with self.assertRaises(ValueError):
            load_seen(self.seen_file)


class TestReconcileSeen(unittest.TestCase):
    def setUp(self):
        self.scratch = _support.scratch_dir("p3b_test_rec_")
        self.lib = self.scratch / "library"
        self.state = self.scratch / ".state"
        self.state.mkdir(parents=True, exist_ok=True)
        self.lib.mkdir(parents=True, exist_ok=True)
        self.seen_file = self.state / "seen.json"

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_reconcile_fixes_missing_and_mismatched_paths(self):
        e1_dir = self.lib / "tech" / "entry-one"
        e1_dir.mkdir(parents=True, exist_ok=True)
        write_entry(e1_dir / "entry.md", {
            "id": "entry-one",
            "url": "https://example.com/one",
            "title": "条目一",
        }, "# 条目一\n\n正文一")

        e2_dir = self.lib / "coding" / "entry-two"
        e2_dir.mkdir(parents=True, exist_ok=True)
        write_entry(e2_dir / "entry.md", {
            "id": "entry-two",
            "url": "https://example.com/two",
            "title": "条目二",
        }, "# 条目二\n\n正文二")

        save_seen({
            "entries": {
                "https://example.com/one": {
                    "id": "entry-one",
                    "path": "library/other/entry-one/entry.md",  # wrong path
                    "first_seen": "2026-10-27T00:00:00+08:00",
                }
            }
        }, self.seen_file)

        res = reconcile_seen(self.scratch, dry_run=False)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(res["added"]), 1)
        self.assertEqual(len(res["updated"]), 1)

        seen = load_seen(self.seen_file)
        self.assertEqual(seen["entries"]["https://example.com/one"]["path"], "library/tech/entry-one/entry.md")
        self.assertEqual(seen["entries"]["https://example.com/two"]["path"], "library/coding/entry-two/entry.md")

    def test_reconcile_prunes_orphans(self):
        save_seen({
            "entries": {
                "https://example.com/ghost": {
                    "id": "ghost-entry",
                    "path": "library/tech/ghost/entry.md",
                }
            }
        }, self.seen_file)

        res = reconcile_seen(self.scratch, dry_run=False, prune_orphans=True)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["pruned"], 1)

        seen = load_seen(self.seen_file)
        self.assertNotIn("https://example.com/ghost", seen["entries"])

    def test_snapshot_seen_and_pruning(self):
        save_seen({"entries": {"k": {"id": "1"}}}, self.seen_file)
        res = snapshot_seen(self.scratch, retain_days=14)
        self.assertEqual(res["status"], "ok")

        backups = list((self.scratch / ".state" / "backups").glob("seen-*.json.gz"))
        self.assertEqual(len(backups), 1)
        with gzip.open(backups[0], "rb") as f:
            data = json.loads(f.read().decode("utf-8"))
            self.assertIn("entries", data)


class TestAuditSeenLibraryCheck(unittest.TestCase):
    def setUp(self):
        self.scratch = _support.scratch_dir("p3b_test_audit_")
        (self.scratch / "scripts").mkdir(parents=True, exist_ok=True)
        (self.scratch / ".state").mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "scripts" / "common.py", self.scratch / "scripts" / "common.py")
        shutil.copy(ROOT / "scripts" / "audit.py", self.scratch / "scripts" / "audit.py")
        shutil.copy(_support.template("taxonomy.yaml"), self.scratch / "taxonomy.yaml")
        shutil.copy(_support.template("sources.yaml"), self.scratch / "sources.yaml")

        self.entry_dir = self.scratch / "library" / "tech" / "audit-test-entry"
        self.entry_dir.mkdir(parents=True, exist_ok=True)
        summary = "这是一段用于测试审计对账的中文摘要，确保字符数量在两百到两百五十字符之间，满足系统严格的字符数校验规范。" * 4 + "补充测试文字满足字数限制条件。"
        chars = count_summary_chars(summary)
        fm = {
            "id": "audit-test-entry",
            "title": "测试审计条目",
            "category": "tech",
            "context": "work",
            "summary_chars": chars,
            "status": "archived",
            "source_type": "vendor_blog",
            "url": "https://example.com/audit-test",
        }
        body = (
            f"# 测试审计条目\n\n> 来源：...\n\n## 摘要\n\n{summary}\n\n## 全文\n\n正文详细内容。" * 20
        )
        write_entry(self.entry_dir / "entry.md", fm, body)

        idx = self.scratch / "index.csv"
        idx.write_text("id,path\naudit-test-entry,library/tech/audit-test-entry/entry.md\n", encoding="utf-8-sig")

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def _run_audit(self) -> tuple[int, dict]:
        py = sys.executable
        p = subprocess.run([py, "scripts/audit.py"], capture_output=True, text=True, cwd=str(self.scratch))
        try:
            data = json.loads(p.stdout)
        except Exception:
            data = {"raw": p.stdout, "err": p.stderr}
        return p.returncode, data

    def test_audit_passes_when_seen_aligned(self):
        save_seen({
            "entries": {
                "https://example.com/audit-test": {
                    "id": "audit-test-entry",
                    "path": "library/tech/audit-test-entry/entry.md",
                }
            }
        }, self.scratch / ".state" / "seen.json")

        code, data = self._run_audit()
        self.assertEqual(code, 0, data)
        self.assertTrue(data.get("ok"))

    def test_audit_detects_seen_missing_key(self):
        save_seen({"entries": {}}, self.scratch / ".state" / "seen.json")
        code, data = self._run_audit()
        self.assertEqual(code, 1)
        issue_types = [i["type"] for i in data.get("issues", [])]
        self.assertIn("seen_missing_key", issue_types)

    def test_audit_detects_seen_path_mismatch(self):
        save_seen({
            "entries": {
                "https://example.com/audit-test": {
                    "id": "audit-test-entry",
                    "path": "library/other/wrong-path/entry.md",
                }
            }
        }, self.scratch / ".state" / "seen.json")
        code, data = self._run_audit()
        self.assertEqual(code, 1)
        issue_types = [i["type"] for i in data.get("issues", [])]
        self.assertIn("seen_path_mismatch", issue_types)


class TestStorageAndImageLimits(unittest.TestCase):
    def test_localize_images_blocks_ssrf_images(self):
        from archive_url import localize_images

        scratch = _support.scratch_dir("p3b_img_ssrf_")
        try:
            md_text = "![internal](http://127.0.0.1:9090/secret.png)\n![meta](http://169.254.169.254/img.jpg)"
            out_text, ok, failed, notes = localize_images(md_text, "https://example.com", scratch)
            self.assertEqual(ok, 0)
            self.assertEqual(failed, 2)
            self.assertTrue(any("SSRF" in n for n in notes))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_localize_images_caps_single_image_at_2mb(self):
        from archive_url import localize_images

        scratch = _support.scratch_dir("p3b_img_2mb_")
        try:
            mock_resp = MagicMock()
            mock_resp.content = b"\x89PNG\r\n\x1a\n" + b"0" * (2 * 1024 * 1024 + 1024)

            with patch("archive_url.assert_public_url"), patch("archive_url.safe_fetch", return_value=mock_resp):
                md_text = "![big image](https://example.com/big.png)"
                out_text, ok, failed, notes = localize_images(
                    md_text, "https://example.com", scratch, budget=LEGACY_IMAGE_BUDGET
                )
                self.assertEqual(ok, 0)
                self.assertEqual(failed, 1)
                self.assertTrue(any("单图大小超限" in n for n in notes))
                self.assertNotIn("images/", out_text)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_localize_images_caps_total_images_at_20mb(self):
        from archive_url import localize_images

        scratch = _support.scratch_dir("p3b_img_20mb_")
        try:
            # Each image is 1.8MB (under 2MB single cap). 12 images = 21.6MB > 20MB
            mock_resp = MagicMock()
            mock_resp.content = b"\x89PNG\r\n\x1a\n" + b"0" * int(1.8 * 1024 * 1024)

            lines = [f"![img{i}](https://example.com/img_{i}.png)" for i in range(15)]
            md_text = "\n\n".join(lines)

            with patch("archive_url.assert_public_url"), patch("archive_url.safe_fetch", return_value=mock_resp):
                out_text, ok, failed, notes = localize_images(
                    md_text, "https://example.com", scratch, budget=LEGACY_IMAGE_BUDGET
                )
                # At 1.8MB each: 11 images = 19.8MB <= 20MB. 12th image would exceed 20MB, so capped!
                self.assertEqual(ok, 11)
                self.assertEqual(failed, 4)
                self.assertTrue(any("20MB" in n for n in notes))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


class TestStreamingRealLoop(unittest.TestCase):
    """Real-loop tests: no safe_fetch mock, only assert_public_url is relaxed.

    Guards the Phase 4 fix where ``stream=True`` must never touch
    ``r.apparent_encoding`` (which would slurp the entire body into memory).
    """

    PAYLOAD = b"\x89PNG\r\n\x1a\n" + b"0" * (5 * 1024 * 1024)

    def setUp(self):
        self.scratch = _support.scratch_dir("p3b_stream_")
        self.srv = LocalTestServer({
            "/big.html": (
                200,
                {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(self.PAYLOAD))},
                self.PAYLOAD,
            ),
            "/big.png": (
                200,
                {"Content-Type": "image/png", "Content-Length": str(len(self.PAYLOAD))},
                self.PAYLOAD,
            ),
            # No Content-Length: the server closes the socket to end the body, so
            # only the streaming byte cap can stop the download.
            "/big_stream.png": (
                200,
                {"Content-Type": "image/png"},
                self.PAYLOAD,
            ),
            # Chinese HTML with no charset in the header (requests guesses ISO-8859-1).
            "/cn.html": (
                200,
                {"Content-Type": "text/html"},
                "<html><body><p>中文正文内容，需要按 UTF-8 解码。</p></body></html>".encode("utf-8"),
            ),
        })

    def tearDown(self):
        self.srv.shutdown()
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_safe_fetch_stream_leaves_body_unread(self):
        with patch("common.assert_public_url"):
            r = safe_fetch(self.srv.url("/big.html"), stream=True)
            try:
                self.assertFalse(
                    r._content_consumed,
                    "stream=True must not consume the body (apparent_encoding would read all 5MB)",
                )
                self.assertEqual(r._content, False)
            finally:
                r.close()

    def test_safe_fetch_buffered_path_decodes_utf8_body(self):
        with patch("common.assert_public_url"):
            r = safe_fetch(self.srv.url("/cn.html"), max_bytes=5 * 1024 * 1024)
            try:
                self.assertTrue(r._content_consumed)
                self.assertIsInstance(r._content, bytes)
                self.assertIn("中文正文内容", r.text)
                self.assertEqual(r.encoding.lower().replace("_", "-").replace(" ", ""), "utf-8")
            finally:
                r.close()

    def test_safe_fetch_buffered_rejects_oversized_body(self):
        with patch("common.assert_public_url"):
            with self.assertRaises(ValueError):
                safe_fetch(self.srv.url("/big.html"), max_bytes=1024)

    def _localize_counting(self, path: str):
        """Run localize_images with real streaming; return (result, bytes_read)."""
        from archive_url import localize_images

        read_total = {"bytes": 0}
        real_safe_fetch = common.safe_fetch

        def counting_fetch(url, **kwargs):
            resp = real_safe_fetch(url, **kwargs)
            original_iter = resp.iter_content

            def counting_iter(chunk_size=1, decode_unicode=False):
                for chunk in original_iter(chunk_size, decode_unicode):
                    read_total["bytes"] += len(chunk)
                    yield chunk

            resp.iter_content = counting_iter
            return resp

        with patch("archive_url.assert_public_url"), patch("common.assert_public_url"), \
                patch("archive_url.safe_fetch", side_effect=counting_fetch):
            result = localize_images(
                f"![big]({self.srv.url(path)})",
                "https://example.com/article",
                self.scratch / "images",
                budget=LEGACY_IMAGE_BUDGET,
            )
        return result, read_total["bytes"]

    def test_localize_images_rejects_by_content_length_without_reading(self):
        (text, ok, failed, notes), read_bytes = self._localize_counting("/big.png")
        self.assertEqual(ok, 0)
        self.assertEqual(failed, 1)
        self.assertTrue(any("单图大小超限" in n for n in notes), notes)
        self.assertEqual(read_bytes, 0)

    def test_localize_images_stream_cap_stops_before_full_payload(self):
        (text, ok, failed, notes), read_bytes = self._localize_counting("/big_stream.png")

        self.assertEqual(ok, 0)
        self.assertEqual(failed, 1)
        self.assertTrue(any("单图大小超限" in n for n in notes), notes)
        self.assertIn("![big", text)
        self.assertIn("未本地化", text)
        self.assertEqual(list((self.scratch / "images").glob("*")), [])
        # The cap must trip during streaming, well before the 5MB payload ends.
        self.assertGreater(read_bytes, 2 * 1024 * 1024)
        self.assertLessEqual(read_bytes, 2 * 1024 * 1024 + 64 * 1024)
        self.assertLess(read_bytes, len(self.PAYLOAD))


if __name__ == "__main__":
    unittest.main()
