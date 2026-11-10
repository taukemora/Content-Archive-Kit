#!/usr/bin/env python3
"""Batch 3A tests: layered archive/delivery status and honest digest coverage.

Every test runs in a throwaway project tree. Email goes to a loopback SMTP
sink, Weixin goes to a fake ``hermes`` executable; production state/data is
never read for assertions and never written.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from email import message_from_bytes, policy
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))

import deliver_daily as dd  # noqa: E402
import daily_gate as dg  # noqa: E402

PY = _support.PYTHON
SCRATCH = _support.scratch_dir("maintenance-delivery-")
COPY = ("common.py", "build_digest.py", "daily_gate.py", "deliver_daily.py", "reconcile_seen.py")
STUB = "#!/usr/bin/env python3\nprint('stub: ok')\nraise SystemExit(0)\n"
INDEX_FIELDS = [
    "id", "published", "title", "original_title", "source", "source_type",
    "category", "subcategory", "context", "tags", "url", "summary_chars",
    "images", "status", "content_note", "relevance", "path", "archived",
    "language", "summary",
]

FAKE_HERMES = '''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

log = Path(os.environ["FAKE_HERMES_LOG"])
counter = log.with_suffix(".count")
n = int(counter.read_text()) if counter.exists() else 0
n += 1
counter.write_text(str(n))

argv = sys.argv[1:]
target = argv[argv.index("-t") + 1] if "-t" in argv else ""
text = argv[-1] if argv else ""
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"n": n, "target": target, "text": text}, ensure_ascii=False) + "\\n")

mode = os.environ.get("FAKE_HERMES_MODE", "ok")
if mode == "fail_prepare":
    print(json.dumps({"success": False, "error": "weixin session not ready (prepare failed)"}))
    raise SystemExit(1)
if mode == "fail_always":
    print(json.dumps({"success": False, "error": "weixin bridge rejected the message"}))
    raise SystemExit(1)
print(json.dumps({"success": True, "message_id": "m-%d" % n}))
'''


class _SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server = self.server
        self.wfile.write(b"220 test.local ESMTP ready\r\n")
        in_data = False
        buffer = b""
        while True:
            line = self.rfile.readline()
            if not line:
                break
            if in_data:
                if line in (b".\r\n", b".\n"):
                    in_data = False
                    server.messages.append(buffer)
                    buffer = b""
                    self.wfile.write(b"250 2.0.0 Ok: queued as TEST\r\n")
                else:
                    buffer += line[1:] if line.startswith(b"..") else line
                continue
            command = line.decode("utf-8", "replace").strip()
            upper = command.upper()
            if upper.startswith(("EHLO", "HELO")):
                self.wfile.write(b"250-test.local\r\n250-AUTH PLAIN LOGIN\r\n250 8BITMIME\r\n")
            elif upper.startswith("AUTH"):
                self.wfile.write(b"235 2.7.0 Authentication successful\r\n")
            elif upper.startswith("RCPT TO"):
                server.rcpts.append(command.split(":", 1)[-1].strip())
                self.wfile.write(b"250 2.1.5 Ok\r\n")
            elif upper.startswith("MAIL FROM"):
                self.wfile.write(b"250 2.1.0 Ok\r\n")
            elif upper.startswith("DATA"):
                self.wfile.write(b"354 End data\r\n")
                in_data = True
            elif upper.startswith("QUIT"):
                self.wfile.write(b"221 2.0.0 Bye\r\n")
                break
            else:
                self.wfile.write(b"250 Ok\r\n")


class SmtpSink(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _SMTPHandler)
        self.messages: list[bytes] = []
        self.rcpts: list[str] = []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def stop(self) -> None:
        try:
            self.shutdown()
        finally:
            self.server_close()

    def parsed(self) -> list:
        return [message_from_bytes(raw, policy=policy.default) for raw in self.messages]


def _free_port() -> int:
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


class Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=str(SCRATCH), prefix="proj_"))
        (self.root / "scripts").mkdir()
        for name in COPY:
            shutil.copy(ROOT / "scripts" / name, self.root / "scripts" / name)
        for name in ("audit.py", "rebuild_index.py", "fetch_sources.py"):
            (self.root / "scripts" / name).write_text(STUB, encoding="utf-8")
        shutil.copy(_support.template("taxonomy.yaml"), self.root / "taxonomy.yaml")
        (self.root / "sources.yaml").write_text(
            yaml.safe_dump({"version": 1, "sources": []}, allow_unicode=True), encoding="utf-8"
        )
        (self.root / ".state" / "runs").mkdir(parents=True)
        (self.root / ".state" / "seen.json").write_text(json.dumps({"entries": {}}), encoding="utf-8")
        (self.root / "library").mkdir()
        (self.root / "fake_hermes").write_text(FAKE_HERMES, encoding="utf-8")
        (self.root / "fake_hermes").chmod(0o755)
        (self.root / "env.test").write_text(
            "\n".join(
                [
                    "EMAIL_ADDRESS=sender@test.local",
                    "EMAIL_PASSWORD=test-only-password",
                    "EMAIL_SMTP_HOST=127.0.0.1",
                    "EMAIL_SMTP_PORT=PORT_PLACEHOLDER",
                    "EMAIL_SMTP_SECURITY=none",
                    "WEIXIN_HOME_CHANNEL=home-test@im.wechat",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.sink = SmtpSink()
        path = self.root / "env.test"
        path.write_text(
            path.read_text(encoding="utf-8").replace("PORT_PLACEHOLDER", str(self.sink.port)),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        try:
            self.sink.stop()
        finally:
            shutil.rmtree(self.root, ignore_errors=True)

    # --- fixtures -----------------------------------------------------
    def write_index(self, rows: list[dict]) -> None:
        import csv

        with open(self.root / "index.csv", "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=INDEX_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in INDEX_FIELDS})

    def gate_path(self) -> Path:
        return self.root / ".state" / "runs" / "daily-gate.json"

    def write_fetch(self, urls: list[str], **over) -> dict:
        data = {
            "generated_at": "2026-10-27T10:00:00+08:00",
            "candidates": [{"url": url} for url in urls],
            "sources": [
                {"id": "openai", "label": "OpenAI", "status": "ok", "selected": len(urls), "skipped": 4}
            ],
            "skipped_total": 4,
            "global_dropped": 0,
            "pending_total": 0,
        }
        data.update(over)
        (self.root / ".state" / "runs" / "latest-fetch.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return data

    def write_gate(self, **over) -> dict:
        data = {
            "started_at": dd.now().isoformat(timespec="seconds"),
            "fetch_generated_at": "2026-10-27T10:00:00+08:00",
            "sources": [
                {"id": "openai", "label": "OpenAI", "status": "ok", "selected": 1, "skipped": 4}
            ],
            "not_ok": [],
            "needs_attention": [],
            "failed_candidates": [],
            "global_dropped": 0,
            "pending_total": 0,
            "status": "started",
        }
        data.update(over)
        self.gate_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data

    def index_rows(self, urls: list[str], *, archived: str = "2026-10-27T10:30:00+08:00") -> list[dict]:
        return [
            {
                "id": f"2026-10-27-item-{i}",
                "published": "2026-10-27",
                "title": f"第 {i} 篇：测试文章标题 {i}",
                "source": "OpenAI",
                "status": "archived",
                "url": url,
                "archived": archived,
                "path": f"library/tech/2026-10-27-item-{i}/entry.md",
                "summary": "测试用摘要。",
            }
            for i, url in enumerate(urls, 1)
        ]

    def seen_entries(self, urls: list[str]) -> None:
        from common import dedupe_key

        data = {"entries": {}}
        for url in urls:
            key = dedupe_key(url)
            data["entries"][key] = {"id": key, "path": "library/tech/x/entry.md"}
        (self.root / ".state" / "seen.json").write_text(json.dumps(data), encoding="utf-8")

    def prime_started_run(self, urls: list[str]) -> None:
        gen = "2026-10-27T10:00:00+08:00"
        self.write_fetch(urls)
        self.write_gate(status="started", fetch_generated_at=gen)
        self.write_index(self.index_rows(urls))
        self.seen_entries(urls)

    def gate(self) -> dict:
        return json.loads(self.gate_path().read_text(encoding="utf-8"))

    def hermes_calls(self) -> list[dict]:
        path = self.root / "hermes_calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def env(self, **extra) -> dict:
        env = os.environ.copy()
        for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            env.pop(key, None)
        env.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "CA_HERMES_ENV_FILE": str(self.root / "env.test"),
                "CA_EMAIL_TO": "archive@example.com",
                "CA_WEIXIN_TARGET": "weixin:test-target",
                "CA_SMTP_PROXY": "",
                "CA_DELIVERY_LOG": str(self.root / "delivery.log"),
                "CA_WEIXIN_RETRY_DELAY": "0",
                "HERMES_BIN": str(self.root / "fake_hermes"),
                "FAKE_HERMES_LOG": str(self.root / "hermes_calls.jsonl"),
            }
        )
        env.update(extra)
        return env

    def run_script(self, name: str, *args: str, **env_extra) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PY, "-B", str(self.root / "scripts" / name), *args],
            capture_output=True, text=True, cwd=str(self.root), env=self.env(**env_extra), timeout=180,
        )


# --------------------------------------------------- 1. layered status truth


class TestStatusLayering(Sandbox):
    def test_start_success_leaves_archive_and_overall_pending(self):
        (self.root / "scripts" / "fetch_sources.py").write_text(
            "import json, sys\n"
            "args = sys.argv[1:]\n"
            "out = args[args.index('--json-out') + 1]\n"
            "json.dump({'generated_at': '2026-10-27T10:00:00+08:00',\n"
            "           'sources': [{'id': 'openai', 'label': 'OpenAI', 'status': 'ok', 'selected': 0}],\n"
            "           'candidates': []}, open(out, 'w'))\n",
            encoding="utf-8",
        )
        res = self.run_script("daily_gate.py", "start")
        self.assertEqual(res.returncode, 0, res.stderr)
        gate = self.gate()
        self.assertEqual(gate["status"], "started")
        self.assertEqual(gate["archive_status"], "pending")
        self.assertEqual(gate["overall_status"], "pending")

    def test_finish_without_delivery_is_pending_not_delivered(self):
        urls = [f"https://example.com/post-{i}" for i in range(1, 3)]
        self.prime_started_run(urls)
        res = self.run_script("daily_gate.py", "finish")
        self.assertEqual(res.returncode, 0, res.stderr)
        gate = self.gate()
        self.assertEqual(gate["status"], "ok")
        self.assertEqual(gate["archive_status"], "ok")
        self.assertEqual(gate["overall_status"], "pending")
        self.assertNotIn("delivery", gate)

    def test_archive_failure_fails_every_layer(self):
        urls = ["https://example.com/post-1", "https://example.com/post-2"]
        self.prime_started_run(urls)
        self.seen_entries(urls[:1])
        res = self.run_script("daily_gate.py", "finish")
        self.assertEqual(res.returncode, 1)
        gate = self.gate()
        self.assertEqual(gate["status"], "failed")
        self.assertEqual(gate["archive_status"], "failed")
        self.assertEqual(gate["overall_status"], "failed")

    def test_dual_delivery_success_is_overall_ok(self):
        urls = [f"https://example.com/post-{i}" for i in range(1, 3)]
        self.prime_started_run(urls)
        res = self.run_script("daily_gate.py", "finish", "--deliver")
        self.assertEqual(res.returncode, 0, res.stderr)
        gate = self.gate()
        self.assertEqual(gate["status"], "ok")
        self.assertEqual(gate["archive_status"], "ok")
        self.assertEqual(gate["overall_status"], "ok")
        self.assertEqual(gate["delivery"]["email"]["status"], "ok")
        self.assertEqual(gate["delivery"]["weixin"]["status"], "ok")

    def test_partial_delivery_fails_overall_but_keeps_archive_ok(self):
        urls = [f"https://example.com/post-{i}" for i in range(1, 3)]
        self.prime_started_run(urls)
        first = self.run_script("daily_gate.py", "finish", "--deliver", FAKE_HERMES_MODE="fail_prepare")
        self.assertEqual(first.returncode, 1)
        self.assertIn("[DELIVERY_PARTIAL_FAILURE]", first.stderr)
        gate = self.gate()
        self.assertEqual(gate["status"], "ok")
        self.assertEqual(gate["archive_status"], "ok")
        self.assertEqual(gate["overall_status"], "failed")
        self.assertEqual(gate["delivery"]["email"]["status"], "ok")
        self.assertEqual(gate["delivery"]["weixin"]["status"], "failed")
        self.assertEqual(len(self.sink.messages), 1)

        # Retry the failed channel only; overall recovers once it succeeds.
        second = self.run_script("daily_gate.py", "deliver")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(self.sink.messages), 1, "email must not be re-sent")
        self.assertEqual(len(self.hermes_calls()), 2)
        gate = self.gate()
        self.assertEqual(gate["archive_status"], "ok")
        self.assertEqual(gate["overall_status"], "ok")
        self.assertEqual(gate["delivery"]["email"]["status"], "ok")
        self.assertEqual(gate["delivery"]["weixin"]["status"], "ok")

    def test_both_channels_failed_overall_failed(self):
        urls = ["https://example.com/post-1"]
        self.prime_started_run(urls)
        dead = _free_port()
        env_file = self.root / "env.test"
        env_file.write_text(
            env_file.read_text(encoding="utf-8").replace(
                f"EMAIL_SMTP_PORT={self.sink.port}", f"EMAIL_SMTP_PORT={dead}"
            ),
            encoding="utf-8",
        )
        res = self.run_script("daily_gate.py", "finish", "--deliver", FAKE_HERMES_MODE="fail_always")
        self.assertEqual(res.returncode, 1)
        self.assertNotIn("[DELIVERY_PARTIAL_FAILURE]", res.stderr)
        gate = self.gate()
        self.assertEqual(gate["archive_status"], "ok")
        self.assertEqual(gate["overall_status"], "failed")

    def test_failure_notification_success_still_overall_failed(self):
        res = self.run_script("daily_gate.py", "start", "--deliver-on-fail")
        self.assertEqual(res.returncode, 1)
        gate = self.gate()
        self.assertEqual(gate["status"], "failed")
        self.assertEqual(gate["archive_status"], "failed")
        self.assertEqual(gate["overall_status"], "failed")
        self.assertEqual(gate["delivery"]["email"]["status"], "ok")
        self.assertEqual(gate["delivery"]["weixin"]["status"], "ok")
        self.assertEqual(len(self.sink.messages), 1)

    def test_unavailable_delivery_module_is_visible_failure(self):
        urls = ["https://example.com/post-1"]
        self.prime_started_run(urls)
        (self.root / "scripts" / "deliver_daily.py").write_text(
            "raise RuntimeError('delivery module exploded')\n", encoding="utf-8"
        )
        res = self.run_script("daily_gate.py", "finish", "--deliver")
        self.assertEqual(res.returncode, 1)
        gate = self.gate()
        self.assertEqual(gate["status"], "ok")
        self.assertEqual(gate["archive_status"], "ok")
        self.assertEqual(gate["overall_status"], "failed")
        for channel in ("email", "weixin"):
            self.assertEqual(gate["delivery"][channel]["status"], "failed")
            self.assertIn("delivery", gate["delivery"][channel]["detail"])

    def test_legacy_gate_derives_archive_status(self):
        urls = ["https://example.com/post-1"]
        self.write_fetch(urls)
        self.write_gate(status="ok", delivery=None)
        self.write_index(self.index_rows(urls))
        gate = self.gate()
        gate.pop("archive_status", None)
        gate.pop("overall_status", None)
        self.gate_path().write_text(json.dumps(gate), encoding="utf-8")

        res = self.run_script("deliver_daily.py")
        self.assertEqual(res.returncode, 0, res.stderr)
        gate = self.gate()
        self.assertEqual(gate["status"], "ok")
        self.assertEqual(gate["archive_status"], "ok")
        self.assertEqual(gate["overall_status"], "ok")

    def test_dry_run_writes_nothing(self):
        urls = ["https://example.com/post-1"]
        self.write_fetch(urls)
        self.write_gate(status="ok")
        self.write_index(self.index_rows(urls))
        before = self.gate_path().read_bytes()
        res = self.run_script("deliver_daily.py", "--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["archive_status"], "ok")
        self.assertEqual(payload["overall_status"], "pending", "preview must not claim delivery")
        self.assertEqual(self.gate_path().read_bytes(), before)
        self.assertEqual(self.sink.messages, [])
        self.assertEqual(self.hermes_calls(), [])


# ------------------------------------------------ 2. digest totals/coverage


class TestDigestHonesty(Sandbox):
    def write_digest_run(self, rows: list[dict], **run_over) -> None:
        urls = [row["url"] for row in rows]
        self.write_index(rows)
        data = {
            "generated_at": "2026-10-26T00:00:00+08:00",
            "candidates": [{"url": u} for u in urls],
            "sources": [
                {"id": "openai", "label": "OpenAI", "status": "ok", "selected": len(urls), "skipped": 0}
            ],
            "skipped_total": 0,
            "global_dropped": 0,
            "pending_total": 0,
            "coverage_skipped": 0,
            "pending_expired": 0,
            "pending_exits": [],
        }
        data.update(run_over)
        (self.root / ".state" / "runs" / "latest-fetch.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def digest(self, *args: str) -> subprocess.CompletedProcess:
        return self.run_script(
            "build_digest.py", "--run", str(self.root / ".state" / "runs" / "latest-fetch.json"), *args
        )

    def rows(self, count: int) -> list[dict]:
        rows = self.index_rows([f"https://example.com/post-{i}" for i in range(1, count + 1)])
        for i, row in enumerate(rows, 1):
            row["published"] = f"2026-10-{28 - i:02d}"
        return rows

    def test_total_new_is_counted_before_display_truncation(self):
        self.write_digest_run(self.rows(5))
        res = self.digest("--limit", "2")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("新增 5 篇", res.stdout)
        self.assertIn("展示 2 篇，省略 3 篇", res.stdout)
        self.assertEqual(len(re.findall(r"^\d+\. ", res.stdout, re.M)), 2)

    def test_limit_zero_reports_total_without_claiming_no_news(self):
        self.write_digest_run(self.rows(5))
        res = self.digest("--limit", "0")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("新增 5 篇", res.stdout)
        self.assertIn("展示 0 篇，省略 5 篇", res.stdout)
        self.assertNotIn("今日无新增", res.stdout)

    def test_negative_limit_is_rejected(self):
        self.write_digest_run(self.rows(2))
        res = self.digest("--limit", "-1")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("limit", res.stderr.lower())

    def test_partial_coverage_includes_rows_omitted_from_display(self):
        rows = self.rows(3)
        rows[-1]["status"] = "partial"
        rows[-1]["content_note"] = "正文在末尾截断"
        self.write_digest_run(rows)
        res = self.digest("--limit", "1")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("展示 1 篇，省略 2 篇", res.stdout)
        self.assertIn("（本 run 新增）", res.stdout)
        self.assertIn(rows[-1]["title"], res.stdout.split("## 覆盖说明", 1)[-1])
        self.assertNotIn("（今日已存）", res.stdout)

    def test_coverage_window_skips_and_pending_exits_are_surfaced(self):
        exits = [{
            "url": "https://example.com/expired-old",
            "source_id": "openai",
            "reason": "expired",
            "queued_at": "2026-09-01T08:00:00+08:00",
        }]
        self.write_digest_run(
            self.rows(1),
            coverage_skipped=7,
            pending_expired=1,
            pending_exits=exits,
            sources=[{
                "id": "openai", "label": "OpenAI", "status": "ok", "selected": 1,
                "skipped": 0, "coverage_window": "watermark:30d",
                "pending_expired": 1, "pending_exits": exits,
            }],
        )
        res = self.digest()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("覆盖窗外跳过 7 篇", res.stdout)
        self.assertIn("watermark:30d", res.stdout)
        self.assertIn("https://example.com/expired-old", res.stdout)
        self.assertIn("已过期", res.stdout)
        self.assertIn("不等于净减少", res.stdout)

    def test_section_rejections_show_source_title_reason_and_count(self):
        rejections = [
            {
                "title": "GLM 示例版本发布",
                "reason": "missing_section_id",
                "message": "section 缺少真实 id 属性，不编造锚点",
                "date": "2026-09-02",
            },
            {
                "title": "未命名章节",
                "reason": "missing_section_id",
                "message": "section 缺少真实 id 属性，不编造锚点",
                "date": "",
            },
        ]
        self.write_digest_run(
            self.rows(1),
            rejections_total=2,
            sources=[{
                "id": "glm", "label": "GLM 文档更新", "status": "ok", "selected": 1,
                "skipped": 0, "rejected": 2, "rejections": rejections,
            }],
        )
        res = self.digest()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("章节拒绝 2 条", res.stdout)
        body = res.stdout.split("## 章节拒绝", 1)[-1]
        for needle in ("GLM 文档更新", "GLM 示例版本发布", "missing_section_id", "不编造锚点", "2026-09-02"):
            self.assertIn(needle, body)

    def test_section_rejections_not_silent_when_source_goes_empty(self):
        rejections = [{
            "title": "GLM 示例更新条",
            "reason": "missing_section_id",
            "message": "section 缺少真实 id 属性，不编造锚点",
            "date": "2026-09-10",
        }]
        self.write_digest_run(
            [],
            rejections_total=1,
            sources=[{
                "id": "glm", "label": "GLM 文档更新", "status": "empty",
                "empty_reason": "文档更新页未解析到有效章节（1 个章节因无 id 锚点被拒绝）",
                "rejected": 1, "rejections": rejections,
            }],
        )
        res = self.digest()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("章节拒绝 1 条", res.stdout)
        self.assertIn("GLM 示例更新条", res.stdout)
        self.assertIn("missing_section_id", res.stdout)

    def test_old_run_without_rejection_fields_is_unchanged(self):
        self.write_digest_run(self.rows(1))
        res = self.digest()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("新增 1 篇", res.stdout)
        self.assertNotIn("章节拒绝", res.stdout)

    def test_pending_exit_lifecycle_respects_seen_only_gate(self):
        exits = [
            {
                "url": "https://example.com/expired-old",
                "source_id": "openai",
                "reason": "expired",
                "queued_at": "2026-09-01T08:00:00+08:00",
            },
            {"url": "https://example.com/seen-old", "source_id": "openai", "reason": "seen"},
        ]
        self.write_digest_run(self.rows(1), pending_expired=1, pending_exits=exits)
        res = self.digest()
        self.assertEqual(res.returncode, 0, res.stderr)
        # 旧文案把“滚出回看窗口/已归档”也承诺为可重入，违反 seen 唯一归档门。
        self.assertNotIn("仍可能作为新候选重新进入", res.stdout)
        self.assertNotIn("滚出回看窗口", res.stdout)
        note = res.stdout.split("## 待补队列退出", 1)[-1]
        self.assertIn("仍在覆盖窗口内", note)
        self.assertIn("未被 seen 归档", note)
        self.assertIn("不会再被新选", note)

    def test_no_rows_with_failed_source_does_not_claim_no_news(self):
        self.write_digest_run(
            [],
            sources=[{"id": "openai", "label": "OpenAI", "status": "error", "error": "502 Bad Gateway"}],
        )
        res = self.digest()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("不能据此断定今日无新增", res.stdout)
        self.assertIn("但不是全源正常", res.stdout)

    def test_no_rows_with_all_sources_ok_says_no_new(self):
        self.write_digest_run([])
        res = self.digest()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("今日无新增", res.stdout)


# --------------------------------------- 3. direct helper for crashed writes


class TestDeliveryFailureHelper(Sandbox):
    def test_record_delivery_failure_marks_unrecorded_channels(self):
        gate = self.write_gate(status="ok")
        self.gate_path().write_text(json.dumps(gate), encoding="utf-8")
        # The helper locks its module's project root; point it at the sandbox so
        # the test never creates state inside the checkout.
        with mock.patch.object(dg, "ROOT", self.root):
            dg.record_delivery_failure(self.gate_path(), "delivery module unavailable")
        data = self.gate()
        self.assertEqual(data["overall_status"], "failed")
        self.assertEqual(data["archive_status"], "ok")
        for channel in ("email", "weixin"):
            self.assertEqual(data["delivery"][channel]["status"], "failed")
            self.assertIn("delivery module unavailable", data["delivery"][channel]["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
