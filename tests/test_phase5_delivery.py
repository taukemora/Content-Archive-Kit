#!/usr/bin/env python3
"""Phase 5 dual-delivery tests: email full digest + ≤200-char Weixin summary.

Contract: ``docs/plans/phase5-dual-delivery.md``.

Nothing here talks to the outside world:

* email goes to a loopback SMTP sink started inside the test process;
* Weixin goes to a fake ``hermes`` executable that records argv and returns a
  canned ``--json`` envelope;
* every project lives in its own throwaway tree under the platform temporary
  directory;
* the last test class re-hashes any instance ``.state`` files to prove the
  suite never touched them.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from email import message_from_bytes, policy
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))

import deliver_daily as dd  # noqa: E402

PY = _support.PYTHON
SCRATCH = _support.scratch_dir("phase5-delivery-")
COPY = ("common.py", "build_digest.py", "daily_gate.py", "deliver_daily.py", "reconcile_seen.py")
STUB = "#!/usr/bin/env python3\nprint('stub: ok')\nraise SystemExit(0)\n"
INDEX_FIELDS = [
    "id", "published", "title", "original_title", "source", "source_type",
    "category", "subcategory", "context", "tags", "url", "summary_chars",
    "images", "status", "content_note", "relevance", "path", "archived",
    "language", "summary",
]

PROD_GATE = ROOT / ".state" / "runs" / "daily-gate.json"
PROD_SEEN = ROOT / ".state" / "seen.json"


def _digest(path: Path) -> str | None:
    return _support.state_digest(path)


_PROD_SNAPSHOT = {str(p): _digest(p) for p in (PROD_GATE, PROD_SEEN)}


FAKE_HERMES = '''#!/usr/bin/env python3
"""Stand-in for `hermes send -t <target> --json <text>` (tests only)."""
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
if mode == "ratelimit_once" and n == 1:
    print(json.dumps({"success": False, "error": "rate limited; cooldown active for 30.0s"}))
    raise SystemExit(1)
if mode == "fail_always":
    print(json.dumps({"success": False, "error": "weixin bridge rejected the message"}))
    raise SystemExit(1)
print(json.dumps({"success": True, "message_id": "m-%d" % n}))
'''


# ------------------------------------------------------------- SMTP test sink


class _SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:  # noqa: C901 - tiny protocol switchboard
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
                server.auth_seen.append(command.split(" ")[0])
                self.wfile.write(b"235 2.7.0 Authentication successful\r\n")
            elif upper.startswith("RCPT TO"):
                server.rcpts.append(command.split(":", 1)[-1].strip())
                self.wfile.write(b"250 2.1.5 Ok\r\n")
            elif upper.startswith("MAIL FROM"):
                server.mail_from.append(command.split(":", 1)[-1].strip())
                self.wfile.write(b"250 2.1.0 Ok\r\n")
            elif upper.startswith("DATA"):
                self.wfile.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
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
        self.mail_from: list[str] = []
        self.auth_seen: list[str] = []
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

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

    def parts(self, msg):
        plain, html = "", ""
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.is_multipart():
                continue
            if ctype == "text/plain":
                plain = part.get_content()
            elif ctype == "text/html":
                html = part.get_content()
        return plain, html


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


# --------------------------------------------------------------- sandbox base


class SandboxCase(unittest.TestCase):
    """Throwaway project tree; the production library/.state is never touched."""

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
                    "# test-only credentials, never the production ones",
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
        self._write_env_file(self.sink.port)

    def tearDown(self) -> None:
        try:
            self.sink.stop()
        finally:
            shutil.rmtree(self.root, ignore_errors=True)

    # --- fixtures -----------------------------------------------------
    def _write_env_file(self, port: int) -> None:
        path = self.root / "env.test"
        text = path.read_text(encoding="utf-8").replace("PORT_PLACEHOLDER", str(port))
        path.write_text(text, encoding="utf-8")

    def write_index(self, rows: list[dict]) -> None:
        with open(self.root / "index.csv", "w", encoding="utf-8-sig", newline="") as handle:
            writer = _csv_writer(handle)
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in INDEX_FIELDS})

    def write_gate(self, **over) -> dict:
        data = {
            "started_at": "2026-10-27T10:00:00+08:00",
            "fetch_generated_at": "2026-10-27T10:00:00+08:00",
            "sources": [
                {"id": "openai", "label": "OpenAI", "status": "ok", "selected": 3, "skipped": 4},
                {"id": "anthropic", "label": "Anthropic", "status": "ok", "selected": 2, "skipped": 4},
            ],
            "not_ok": [],
            "needs_attention": [],
            "failed_candidates": [],
            "global_dropped": 0,
            "pending_total": 0,
            "status": "ok",
        }
        data.update(over)
        (self.root / ".state" / "runs" / "daily-gate.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return data

    def write_fetch(self, urls: list[str], **over) -> dict:
        data = {
            "generated_at": "2026-10-27T10:00:00+08:00",
            "candidates": [
                {"url": url, "title": f"candidate {i}"} for i, url in enumerate(urls, 1)
            ],
            "sources": [
                {"id": "openai", "label": "OpenAI", "status": "ok", "selected": 3, "skipped": 4},
                {"id": "anthropic", "label": "Anthropic", "status": "ok", "selected": 2, "skipped": 4},
            ],
            "skipped_total": 8,
            "global_dropped": 0,
            "pending_total": 0,
        }
        data.update(over)
        (self.root / ".state" / "runs" / "latest-fetch.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return data

    def seen_entries(self, urls: list[str]) -> None:
        from common import dedupe_key

        data = {"entries": {}}
        for url in urls:
            key = dedupe_key(url)
            data["entries"][key] = {"id": key, "path": "library/tech/x/entry.md"}
        (self.root / ".state" / "seen.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def index_rows(self, urls: list[str], *, archived: str = "2026-10-27T10:30:00+08:00") -> list[dict]:
        rows = []
        for i, url in enumerate(urls, 1):
            rows.append(
                {
                    "id": f"2026-10-27-item-{i}",
                    "published": "2026-10-27",
                    "title": f"第 {i} 篇：OpenAI 发布新一代推理模型与成本优化方案 {i}",
                    "source": "OpenAI",
                    "status": "archived",
                    "url": url,
                    "archived": archived,
                    "path": f"library/tech/2026-10-27-item-{i}/entry.md",
                    "summary": "测试用摘要。",
                }
            )
        return rows

    def gate(self) -> dict:
        return json.loads((self.root / ".state" / "runs" / "daily-gate.json").read_text(encoding="utf-8"))

    def hermes_calls(self) -> list[dict]:
        path = self.root / "hermes_calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def delivery_log(self) -> str:
        path = self.root / "delivery.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    # --- runners ------------------------------------------------------
    def env(self, **extra) -> dict:
        env = os.environ.copy()
        for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            env.pop(key, None)  # never let the real proxy leak into a loopback test
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

    def run_deliver(self, *args: str, **env_extra) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PY, "-B", str(self.root / "scripts" / "deliver_daily.py"), *args],
            capture_output=True, text=True, cwd=str(self.root), env=self.env(**env_extra), timeout=180,
        )

    def run_gate(self, *args: str, **env_extra) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PY, "-B", str(self.root / "scripts" / "daily_gate.py"), *args],
            capture_output=True, text=True, cwd=str(self.root), env=self.env(**env_extra), timeout=180,
        )


def _csv_writer(handle):
    import csv

    writer = csv.DictWriter(handle, fieldnames=INDEX_FIELDS)
    writer.writeheader()
    return writer


# ------------------------------------------------- 0. config/env default rules


class TestConfigDefaults(SandboxCase):
    def test_env_file_parsing_tolerates_quotes_export_and_comments(self):
        path = self.root / "env.raw"
        path.write_text(
            "# comment\n"
            'EMAIL_ADDRESS="quoted@test.local"\n'
            "export EMAIL_SMTP_HOST=mx.test.local\n"
            "EMAIL_SMTP_PORT='2525'\n"
            "BROKEN_LINE\n"
            "\n",
            encoding="utf-8",
        )
        env = dd.read_env_file(path)
        self.assertEqual(env["EMAIL_ADDRESS"], "quoted@test.local")
        self.assertEqual(env["EMAIL_SMTP_HOST"], "mx.test.local")
        self.assertEqual(env["EMAIL_SMTP_PORT"], "2525")
        self.assertNotIn("BROKEN_LINE", env)
        self.assertEqual(dd.read_env_file(self.root / "nope.env"), {})

    def test_smtp_security_defaults_follow_the_port(self):
        self.assertEqual(dd.smtp_settings({})["port"], 587)
        self.assertEqual(dd.smtp_settings({})["security"], "starttls")
        self.assertEqual(
            dd.smtp_settings({"EMAIL_SMTP_PORT": "465"})["security"], "tls"
        )
        self.assertEqual(
            dd.smtp_settings({"EMAIL_SMTP_PORT": "465", "EMAIL_SMTP_SECURITY": "starttls"})["security"],
            "starttls",
        )
        self.assertEqual(
            dd.smtp_settings({"EMAIL_SMTP_PORT": "587", "EMAIL_SMTP_SECURITY": "none"})["security"],
            "none",
        )
        settings = dd.smtp_settings({"EMAIL_ADDRESS": "a@b.c", "EMAIL_PASSWORD": "pw"})
        self.assertEqual(settings["username"], "a@b.c")
        self.assertEqual(settings["mail_from"], "a@b.c")

    def test_default_targets(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CA_EMAIL_TO", None)
            os.environ.pop("CA_WEIXIN_TARGET", None)
            self.assertFalse(
                dd.default_email_to({}),
                "delivery must not ship a built-in recipient; set CA_EMAIL_TO instead",
            )
            self.assertFalse(
                dd.default_weixin_target({}),
                "delivery must not ship a built-in Weixin target; set CA_WEIXIN_TARGET instead",
            )
            self.assertEqual(
                dd.default_weixin_target({"WEIXIN_HOME_CHANNEL": "home@im.wechat"}),
                "weixin:home@im.wechat",
            )
            os.environ["CA_EMAIL_TO"] = "other@example.com"
            os.environ["CA_WEIXIN_TARGET"] = "weixin:override"
            self.assertEqual(dd.default_email_to({}), "other@example.com")
            self.assertEqual(dd.default_weixin_target({}), "weixin:override")

    def test_proxy_resolution_rules(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ["HTTPS_PROXY"] = "http://192.0.2.10:7893"
            self.assertEqual(dd.resolve_proxy("example.com"), "http://192.0.2.10:7893")
            self.assertEqual(dd.resolve_proxy("127.0.0.1"), "")
            os.environ["CA_SMTP_PROXY"] = ""
            self.assertEqual(dd.resolve_proxy("example.com"), "", "empty override forces a direct dial")
            os.environ["CA_SMTP_PROXY"] = "http://192.0.2.11:8080"
            self.assertEqual(dd.resolve_proxy("example.com"), "http://192.0.2.11:8080")


# --------------------------------------------------- 1. the 200-char hard cap



class TestWeixinHardCap(unittest.TestCase):
    """`len(text) <= 200` must hold for every shape of input, not just today's."""

    MAX = dd.WEIXIN_MAX_CHARS

    def test_no_new_items(self):
        text = dd.build_weixin_summary("10-27", [], new_total=0)
        self.assertIn("📚 收藏夹日报 10-27", text)
        self.assertIn("新增 0 篇", text)
        self.assertIn("今日无新增", text)
        self.assertTrue(text.endswith("完整日报已发送至邮箱。"))
        self.assertLessEqual(len(text), self.MAX)

    def test_three_titles_fit_verbatim(self):
        titles = ["模型一发布", "Anthropic Claude 更新", "第三篇：混合 English 标题"]
        text = dd.build_weixin_summary("10-27", titles, new_total=3, skipped=8)
        for index, title in enumerate(titles, 1):
            self.assertIn(f"{index}. {title}", text)
        self.assertNotIn("…等", text)
        self.assertIn("新增 3 篇｜跳过 8｜异常源 0", text)
        self.assertLessEqual(len(text), self.MAX)

    def test_ten_long_bilingual_titles_with_anomalies(self):
        long_titles = [
            f"第{i}篇：OpenAI 发布超长中英混合标题 Ultra Long Bilingual Headline Number {i} "
            f"with extra trailing words to blow the budget"
            for i in range(10)
        ]
        for title in long_titles:
            self.assertGreaterEqual(len(title), 80)
        text = dd.build_weixin_summary(
            "10-27",
            long_titles,
            new_total=10,
            skipped=12,
            not_ok_labels=["Anthropic", "OpenAI"],
            pending=3,
        )
        self.assertLessEqual(len(text), self.MAX)
        self.assertIn("📚 收藏夹日报 10-27", text)
        self.assertIn("新增 10 篇｜跳过 12｜异常源 2｜待补 3", text)
        self.assertIn("…等 10 篇", text)
        self.assertIn("异常源：Anthropic、OpenAI", text)
        self.assertTrue(text.endswith("完整日报已发送至邮箱。"))

    def test_long_multiline_traceback_failure_summary(self):
        traceback_text = (
            "Traceback (most recent call last):\n"
            '  File "<archive>/scripts/build_digest.py", line 138\n'
            "    raise RuntimeError('index.csv exploded: ' + 'boom ' * 500)\n"
            "RuntimeError: index.csv exploded: " + "boom " * 900
        )
        text = dd.build_weixin_failure_summary("10-27", step="build_digest", reason=traceback_text)
        self.assertLessEqual(len(text), self.MAX)
        self.assertTrue(text.startswith("⚠️ 收藏夹日报异常 10-27"))
        self.assertIn("阶段：build_digest", text)
        self.assertIn("原因：", text)
        self.assertTrue(text.endswith("详细诊断已发送至邮箱。"))
        reason_line = [ln for ln in text.splitlines() if ln.startswith("原因：")][0]
        self.assertNotIn("\n", reason_line)
        self.assertIn("Traceback", reason_line)

    def test_empty_and_absurd_failure_inputs_stay_bounded(self):
        cases = [
            ("10-27", "", ""),
            ("10-27", "", "   "),
            ("10-27", "x" * 5000, "y" * 20000),
            ("10-27" * 40, "step " * 500, "reason " * 500),
        ]
        for label, step, reason in cases:
            text = dd.build_weixin_failure_summary(label, step=step, reason=reason)
            self.assertLessEqual(len(text), self.MAX, f"{label!r}/{len(step)}/{len(reason)}")
            self.assertTrue(text.endswith("详细诊断已发送至邮箱。"))

    def test_exhaustive_success_matrix(self):
        for count in (0, 1, 3, 6, 10, 12):
            for title_len in (5, 20, 60, 120, 200):
                for labels in ([], ["Anthropic"], ["Anthropic", "OpenAI", "Google"]):
                    for pending in (0, 3):
                        for budget in (self.MAX, 160, 120, 80, 50):
                            titles = ["汉" * title_len + f" #{i}" for i in range(count)]
                            text = dd.build_weixin_summary(
                                "10-27",
                                titles,
                                new_total=count,
                                skipped=8,
                                not_ok_labels=labels,
                                pending=pending,
                                max_chars=budget,
                            )
                            self.assertLessEqual(
                                len(text), budget,
                                f"count={count} len={title_len} labels={len(labels)} "
                                f"pending={pending} budget={budget}",
                            )
                            if budget >= 60:
                                self.assertTrue(
                                    text.endswith("完整日报已发送至邮箱。"), text
                                )
                                self.assertTrue(
                                    text.startswith("📚 收藏夹日报 10-27"), text
                                )

    def test_exhaustive_failure_matrix(self):
        for reason_len in (0, 1, 50, 199, 800, 5000):
            for step_len in (0, 8, 60, 200):
                for budget in (self.MAX, 150, 100, 70):
                    text = dd.build_weixin_failure_summary(
                        "10-27",
                        step="s" * step_len,
                        reason="r" * reason_len,
                        max_chars=budget,
                    )
                    self.assertLessEqual(len(text), budget, f"{reason_len}/{step_len}/{budget}")
                    if budget >= 60:
                        self.assertTrue(text.endswith("详细诊断已发送至邮箱。"), text)


# ------------------------------------------------ 2. end-to-end dual delivery


class TestEndToEndDualChannel(SandboxCase):
    def test_ok_run_delivers_email_and_weixin(self):
        urls = [f"https://example.com/post-{i}" for i in range(1, 6)]
        self.write_fetch(urls)
        self.write_gate()
        self.write_index(self.index_rows(urls))

        res = self.run_deliver()
        self.assertEqual(res.returncode, 0, f"{res.stdout}\n{res.stderr}")
        self.assertNotIn("CRON_FAILURE", res.stderr)

        # ---- email
        self.assertEqual(len(self.sink.messages), 1, "exactly one email must be sent")
        msg = self.sink.parsed()[0]
        self.assertEqual(str(msg["To"]), "archive@example.com")
        subject = str(msg["Subject"])
        self.assertIn("内容收藏夹日报", subject)
        self.assertIn(dd.now().strftime("%Y-%m-%d"), subject)
        self.assertIn("新增 5 篇", subject)
        self.assertNotIn("含异常提醒", subject)
        self.assertEqual(msg.get_content_type(), "multipart/alternative")

        plain, html = self.sink.parts(msg)
        self.assertTrue(plain.strip())
        self.assertNotIn("**", plain, "text/plain must be free of Markdown bold markers")
        self.assertIn("第 1 篇", plain)
        self.assertIn("<html", html.lower())
        self.assertIn('<a href="https://example.com/post-1"', html)
        self.assertIn("内容收藏夹日报", html)
        self.assertEqual(sorted(self.sink.rcpts), ["<archive@example.com>"])

        # ---- weixin
        calls = self.hermes_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["target"], "weixin:test-target")
        wx_text = calls[0]["text"]
        self.assertLessEqual(len(wx_text), 200)
        self.assertIn("新增 5 篇", wx_text)
        self.assertTrue(wx_text.endswith("完整日报已发送至邮箱。"))
        self.assertNotIn("摘要：", wx_text, "Weixin must never carry the long digest body")

        # ---- state + log
        gate = self.gate()
        self.assertEqual(gate["delivery"]["email"]["status"], "ok")
        self.assertEqual(gate["delivery"]["weixin"]["status"], "ok")
        log = self.delivery_log()
        self.assertIn("email=ok", log)
        self.assertIn("weixin=ok", log)
        self.assertNotIn("test-only-password", log)
        self.assertNotIn("test-only-password", res.stdout + res.stderr)

    def test_dry_run_sends_nothing_and_writes_nothing(self):
        urls = [f"https://example.com/post-{i}" for i in range(1, 4)]
        self.write_fetch(urls)
        self.write_gate()
        self.write_index(self.index_rows(urls))
        before = self.gate()

        res = self.run_deliver("--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        for key in (
            "mode", "email_to", "email_subject", "email_plain_chars",
            "weixin_target", "weixin_text", "weixin_chars",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["mode"], "ok")
        self.assertEqual(payload["email_to"], "archive@example.com")
        self.assertIn("新增 3 篇", payload["email_subject"])
        self.assertLessEqual(payload["weixin_chars"], 200)
        self.assertEqual(payload["weixin_chars"], len(payload["weixin_text"]))
        self.assertEqual(self.sink.messages, [])
        self.assertEqual(self.hermes_calls(), [])
        self.assertEqual(self.gate(), before, "dry-run must not touch daily-gate.json")
        self.assertNotIn("delivery", self.gate())

    def test_failure_mode_mail_and_weixin(self):
        self.write_fetch(["https://example.com/post-1"])
        self.write_gate(
            status="failed",
            error="build_digest failed:\nTraceback (most recent call last):\n"
                  "RuntimeError: index.csv exploded",
            failed_candidates=[{"url": "https://example.com/broken", "reason": "web_extract 502"}],
            unaccounted_candidates=[{"url": "https://example.com/lost", "title": "悬空条目"}],
            sources=[
                {"id": "openai", "label": "OpenAI", "status": "ok", "selected": 1},
                {"id": "anthropic", "label": "Anthropic", "status": "error", "error": "502 Bad Gateway"},
            ],
        )

        res = self.run_deliver()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(self.sink.messages), 1)
        msg = self.sink.parsed()[0]
        self.assertIn("内容收藏夹日报异常", str(msg["Subject"]))
        plain, html = self.sink.parts(msg)
        self.assertIn("失败阶段：build_digest", plain)
        self.assertIn("index.csv exploded", plain)
        self.assertIn("https://example.com/broken", plain)
        self.assertIn("https://example.com/lost", plain)
        self.assertIn("Anthropic", plain)
        self.assertIn("502 Bad Gateway", plain)
        self.assertIn("<html", html.lower())

        calls = self.hermes_calls()
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(len(calls[0]["text"]), 200)
        self.assertIn("收藏夹日报异常", calls[0]["text"])
        self.assertIn("详细诊断已发送至邮箱。", calls[0]["text"])
        # A short failure reason fits, but it must stay a single flattened line.
        self.assertEqual(len(calls[0]["text"].splitlines()), 4)
        self.assertLess(len(calls[0]["text"]), len(plain))

        gate = self.gate()
        self.assertEqual(gate["delivery"]["email"]["status"], "ok")
        self.assertEqual(gate["delivery"]["weixin"]["status"], "ok")


# ------------------------------------------- 3. no cross-fallback, independence


class TestNoCrossFallback(SandboxCase):
    def _prime(self, count: int = 3) -> list[str]:
        urls = [f"https://example.com/post-{i}" for i in range(1, count + 1)]
        self.write_fetch(urls)
        self.write_gate()
        self.write_index(self.index_rows(urls))
        return urls

    def test_weixin_session_not_ready_fails_fast_without_second_email(self):
        self._prime()
        started = time.monotonic()
        res = self.run_deliver(FAKE_HERMES_MODE="fail_prepare")
        elapsed = time.monotonic() - started

        self.assertEqual(res.returncode, 1)
        self.assertIn("[CRON_FAILURE] weixin delivery failed", res.stderr)
        self.assertNotIn("[CRON_FAILURE] email delivery failed", res.stderr)
        # No blind retry sleep: the whole run stays far below the 35s cooldown.
        self.assertLess(elapsed, 20.0, f"unexpected sleep for a non-transient error ({elapsed:.1f}s)")
        # Exactly one attempt, and never a "sorry" email as a fallback for Weixin.
        self.assertEqual(len(self.hermes_calls()), 1)
        self.assertEqual(len(self.sink.messages), 1, "Weixin failure must not trigger a 2nd email")
        self.assertNotIn("微信", self.sink.parsed()[0].get_body(preferencelist=("plain",)).get_content())

        gate = self.gate()
        self.assertEqual(gate["delivery"]["email"]["status"], "ok")
        self.assertEqual(gate["delivery"]["weixin"]["status"], "failed")
        self.assertIn("prepare failed", gate["delivery"]["weixin"]["detail"])

    def test_transient_rate_limit_is_retried_once_and_succeeds(self):
        self._prime()
        res = self.run_deliver(FAKE_HERMES_MODE="ratelimit_once", CA_WEIXIN_RETRY_DELAY="0")
        self.assertEqual(res.returncode, 0, res.stderr)
        calls = self.hermes_calls()
        self.assertEqual(len(calls), 2, "exactly one retry after a rate limit")
        self.assertEqual(calls[0]["text"], calls[1]["text"])
        self.assertLessEqual(len(calls[1]["text"]), 200)
        self.assertEqual(len(self.sink.messages), 1)
        self.assertEqual(self.gate()["delivery"]["weixin"]["status"], "ok")

    def test_email_failure_keeps_weixin_short_and_returns_1(self):
        self._prime()
        dead_port = _free_port()  # nothing is listening there
        env_file = self.root / "env.test"
        text = env_file.read_text(encoding="utf-8").replace(
            f"EMAIL_SMTP_PORT={self.sink.port}", f"EMAIL_SMTP_PORT={dead_port}"
        )
        env_file.write_text(text, encoding="utf-8")

        res = self.run_deliver()
        self.assertEqual(res.returncode, 1)
        self.assertIn("[CRON_FAILURE] email delivery failed", res.stderr)
        self.assertNotIn("[CRON_FAILURE] weixin delivery failed", res.stderr)
        self.assertEqual(self.sink.messages, [])

        calls = self.hermes_calls()
        self.assertEqual(len(calls), 1, "email failure must not change the Weixin payload")
        self.assertLessEqual(len(calls[0]["text"]), 200)
        self.assertIn("完整日报已发送至邮箱。", calls[0]["text"])
        self.assertNotIn("摘要：", calls[0]["text"])
        self.assertNotIn("Traceback", calls[0]["text"])

        gate = self.gate()
        self.assertEqual(gate["delivery"]["email"]["status"], "failed")
        self.assertEqual(gate["delivery"]["weixin"]["status"], "ok")


# ---------------------------------------------------------- 4. idempotency


class TestIdempotentResend(SandboxCase):
    def test_rerun_skips_ok_channel_and_only_resends_weixin(self):
        urls = [f"https://example.com/post-{i}" for i in range(1, 4)]
        self.write_fetch(urls)
        self.write_gate()
        self.write_index(self.index_rows(urls))

        first = self.run_deliver(FAKE_HERMES_MODE="fail_always")
        self.assertEqual(first.returncode, 1, first.stderr)
        self.assertEqual(len(self.sink.messages), 1)
        gate = self.gate()
        self.assertEqual(gate["delivery"]["email"]["status"], "ok")
        self.assertEqual(gate["delivery"]["weixin"]["status"], "failed")

        second = self.run_deliver()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(self.sink.messages), 1, "an already-sent email must not be re-sent")
        payload = json.loads(second.stdout)
        self.assertTrue(payload["email"]["skipped"])
        self.assertFalse(payload["weixin"].get("skipped", False))
        self.assertEqual(len(self.hermes_calls()), 2)  # 1 failed + 1 successful resend
        self.assertLessEqual(len(self.hermes_calls()[-1]["text"]), 200)
        self.assertEqual(self.gate()["delivery"]["weixin"]["status"], "ok")
        self.assertEqual(self.gate()["delivery"]["email"]["status"], "ok")

    def test_force_resends_both_channels(self):
        urls = [f"https://example.com/post-{i}" for i in range(1, 3)]
        self.write_fetch(urls)
        self.write_gate()
        self.write_index(self.index_rows(urls))

        self.assertEqual(self.run_deliver().returncode, 0)
        self.assertEqual(len(self.sink.messages), 1)
        forced = self.run_deliver("--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual(len(self.sink.messages), 2)
        self.assertEqual(len(self.hermes_calls()), 2)


# --------------------------------------------- 5. daily finish --deliver wiring


class TestDailyStartDeliverOnFail(SandboxCase):
    def test_start_failure_without_flag_sends_nothing(self):
        res = self.run_gate("start")
        self.assertEqual(res.returncode, 1)
        self.assertEqual(self.gate()["status"], "failed")
        self.assertEqual(self.sink.messages, [])
        self.assertEqual(self.hermes_calls(), [])

    def test_start_failure_with_flag_pushes_both_channels(self):
        res = self.run_gate("start", "--deliver-on-fail")
        self.assertEqual(res.returncode, 1)
        self.assertEqual(self.gate()["status"], "failed")
        self.assertEqual(len(self.sink.messages), 1)
        msg = self.sink.parsed()[0]
        self.assertIn("内容收藏夹日报异常", str(msg["Subject"]))
        plain, _ = self.sink.parts(msg)
        self.assertIn("失败阶段：daily start", plain)
        self.assertIn("failed to load fetch output", plain)
        calls = self.hermes_calls()
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(len(calls[0]["text"]), 200)
        self.assertIn("收藏夹日报异常", calls[0]["text"])
        self.assertEqual(self.gate()["delivery"]["email"]["status"], "ok")


class TestDailyFinishDeliver(SandboxCase):
    def _prime_started_run(self, urls: list[str]) -> None:
        gen = "2026-10-27T10:00:00+08:00"
        self.write_fetch(urls)
        self.write_gate(
            status="started",
            started_at=dd.now().isoformat(timespec="seconds"),
            fetch_generated_at=gen,
        )
        fetch = json.loads((self.root / ".state" / "runs" / "latest-fetch.json").read_text(encoding="utf-8"))
        fetch["generated_at"] = gen
        (self.root / ".state" / "runs" / "latest-fetch.json").write_text(
            json.dumps(fetch, ensure_ascii=False), encoding="utf-8"
        )
        self.write_index(self.index_rows(urls))
        self.seen_entries(urls)

    def test_success_delivers_digest_email_and_short_summary(self):
        urls = [f"https://example.com/post-{i}" for i in range(1, 3)]
        self._prime_started_run(urls)

        res = self.run_gate("finish", "--deliver")
        self.assertEqual(res.returncode, 0, f"{res.stdout}\n{res.stderr}")
        self.assertIn("内容收藏夹", res.stdout)  # digest still printed
        self.assertEqual(len(self.sink.messages), 1)
        subject = str(self.sink.parsed()[0]["Subject"])
        self.assertIn("新增 2 篇", subject)

        calls = self.hermes_calls()
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(len(calls[0]["text"]), 200)

        gate = self.gate()
        self.assertEqual(gate["status"], "ok")
        self.assertEqual(gate["delivery"]["email"]["status"], "ok")
        self.assertEqual(gate["delivery"]["weixin"]["status"], "ok")

    def test_finish_validation_failure_triggers_failure_notification(self):
        urls = ["https://example.com/post-1", "https://example.com/post-2"]
        self._prime_started_run(urls)
        # Forget the second candidate: finish must fail it as unaccounted.
        self.seen_entries(urls[:1])

        res = self.run_gate("finish", "--deliver")
        self.assertEqual(res.returncode, 1)
        self.assertIn("[CRON_FAILURE] unaccounted_candidates failed", res.stderr)
        gate = self.gate()
        self.assertEqual(gate["status"], "failed")
        self.assertIn("unaccounted_candidates", gate["error"])

        self.assertEqual(len(self.sink.messages), 1)
        subject = str(self.sink.parsed()[0]["Subject"])
        self.assertIn("内容收藏夹日报异常", subject)
        plain, _ = self.sink.parts(self.sink.parsed()[0])
        self.assertIn("unaccounted_candidates", plain)
        self.assertIn("https://example.com/post-2", plain)

        calls = self.hermes_calls()
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(len(calls[0]["text"]), 200)
        self.assertIn("收藏夹日报异常", calls[0]["text"])
        self.assertEqual(gate["delivery"]["weixin"]["status"], "ok")

    def test_corrupt_gate_reports_failure_with_explicit_reason(self):
        self.write_fetch(["https://example.com/post-1"])
        (self.root / ".state" / "runs" / "daily-gate.json").write_text("{not json", encoding="utf-8")

        res = self.run_gate("finish", "--deliver")
        self.assertEqual(res.returncode, 1)
        self.assertIn("failed to parse daily-gate.json", res.stderr)
        self.assertEqual(len(self.sink.messages), 1)
        msg = self.sink.parsed()[0]
        self.assertIn("内容收藏夹日报异常", str(msg["Subject"]))
        plain, _ = self.sink.parts(msg)
        self.assertIn("daily finish", plain)
        calls = self.hermes_calls()
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(len(calls[0]["text"]), 200)

    def test_finish_without_deliver_sends_nothing(self):
        urls = ["https://example.com/post-1"]
        self._prime_started_run(urls)
        res = self.run_gate("finish")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.sink.messages, [])
        self.assertEqual(self.hermes_calls(), [])
        self.assertNotIn("delivery", self.gate())

    def test_missing_gate_still_notifies_with_explicit_reason(self):
        self.write_fetch(["https://example.com/post-1"])  # no daily-gate.json at all

        res = self.run_gate("finish", "--deliver")
        self.assertEqual(res.returncode, 1)
        self.assertIn("missing daily-gate.json", res.stderr)
        self.assertEqual(len(self.sink.messages), 1)
        msg = self.sink.parsed()[0]
        self.assertIn("内容收藏夹日报异常", str(msg["Subject"]))
        plain, _ = self.sink.parts(msg)
        self.assertIn("missing daily-gate.json", plain)
        calls = self.hermes_calls()
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(len(calls[0]["text"]), 200)
        self.assertIn("详细诊断已发送至邮箱。", calls[0]["text"])


# ------------------------------------- 5b. HTTP CONNECT proxy support (SMTP)


class _ConnectProxyHandler(socketserver.StreamRequestHandler):
    """Minimal CONNECT proxy. It always relays to ``server.upstream``."""

    def handle(self) -> None:
        line = self.rfile.readline().decode("latin-1", "replace").strip()
        while True:
            header = self.rfile.readline()
            if header in (b"\r\n", b"\n", b""):
                break
        server = self.server
        server.requests.append(line)
        if not line.upper().startswith("CONNECT"):
            self.wfile.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        if server.reject:
            self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            self.wfile.flush()
            return
        upstream = socket.create_connection(server.upstream, timeout=10)
        self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        self.wfile.flush()

        def pump(src, dst):
            try:
                while True:
                    chunk = src.recv(65536)
                    if not chunk:
                        break
                    dst.sendall(chunk)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        threading.Thread(target=pump, args=(upstream, self.connection), daemon=True).start()
        pump(self.connection, upstream)
        upstream.close()


class ConnectProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, upstream: tuple[str, int], reject: bool = False) -> None:
        super().__init__(("127.0.0.1", 0), _ConnectProxyHandler)
        self.upstream = upstream
        self.reject = reject
        self.requests: list[str] = []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def stop(self) -> None:
        try:
            self.shutdown()
        finally:
            self.server_close()


class TestSmtpProxyTunnel(unittest.TestCase):
    """The proxy hop must use CONNECT, and TLS must verify the mail host name."""

    def setUp(self) -> None:
        self.sink = SmtpSink()

    def tearDown(self) -> None:
        self.sink.stop()

    def _send(self, *, host: str, proxy: str) -> tuple[bool, str]:
        return dd.send_email_smtp(
            host=host,
            port=self.sink.port,
            security="none",
            username="sender@test.local",
            password="test-only-password",
            mail_from="sender@test.local",
            to_addr="archive@example.com",
            subject="代理隧道测试",
            plain="plain body",
            html="<p>html body</p>",
            proxy=proxy,
            timeout=10,
        )

    def test_non_loopback_target_goes_through_connect(self):
        proxy = ConnectProxy(("127.0.0.1", self.sink.port))
        try:
            ok, detail = self._send(host="smtp.test.invalid", proxy=proxy.url)
        finally:
            proxy.stop()
        self.assertTrue(ok, detail)
        self.assertEqual(len(proxy.requests), 1)
        self.assertTrue(proxy.requests[0].startswith("CONNECT smtp.test.invalid:"), proxy.requests)
        self.assertEqual(len(self.sink.messages), 1)
        self.assertEqual(str(self.sink.parsed()[0]["Subject"]), "代理隧道测试")

    def test_loopback_target_bypasses_the_proxy(self):
        proxy = ConnectProxy(("127.0.0.1", self.sink.port))
        try:
            ok, detail = self._send(host="127.0.0.1", proxy=proxy.url)
        finally:
            proxy.stop()
        self.assertTrue(ok, detail)
        self.assertEqual(proxy.requests, [], "loopback mail servers must be dialled directly")
        self.assertEqual(len(self.sink.messages), 1)

    def test_proxy_rejection_is_reported_not_swallowed(self):
        proxy = ConnectProxy(("127.0.0.1", self.sink.port), reject=True)
        try:
            ok, detail = self._send(host="smtp.test.invalid", proxy=proxy.url)
        finally:
            proxy.stop()
        self.assertFalse(ok)
        self.assertIn("proxy CONNECT rejected", detail)
        self.assertIn("502", detail)
        self.assertEqual(self.sink.messages, [])

    def test_tunnel_client_verifies_the_mail_host_not_the_proxy(self):
        sock = socket.create_connection(("127.0.0.1", self.sink.port), timeout=5)
        try:
            client = dd._TunnelSMTP("mx.example.com", 587, sock)
            # smtplib.starttls() passes self._host as server_hostname.
            self.assertEqual(client._host, "mx.example.com")
            self.assertIs(client.sock, sock)
        finally:
            sock.close()


# ------------------------------------- 6. production dry-run (read-only, real data)


class TestFixtureDerivedNewCount(SandboxCase):
    """The subject count must come from the run's candidates, never a constant."""

    def test_subject_counts_only_this_runs_candidates(self):
        urls = [f"https://example.com/post-{i}" for i in range(1, 5)]
        manual = self.index_rows(["https://example.com/manual-bookmark"])[0]
        manual["title"] = "并发手工收藏（不属于本 run）"
        manual["archived"] = "2026-10-27T10:45:00+08:00"
        manual["published"] = "2026-10-28"
        self.write_fetch(urls)
        self.write_gate()
        self.write_index(self.index_rows(urls) + [manual])

        res = self.run_deliver("--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        expected = len(urls)
        self.assertIn(f"新增 {expected} 篇", payload["email_subject"])
        self.assertEqual(payload["new_count"], expected)


@unittest.skipUnless(
    os.environ.get("CA_RUN_INSTANCE_TESTS") == "1",
    "instance-only: needs a configured archive (set CA_RUN_INSTANCE_TESTS=1)",
)
class TestProductionDryRun(unittest.TestCase):
    """Read-only dry run against a real archive instance.

    Skipped in a public checkout: there is no archive to preview. A private
    instance runs it with ``CA_RUN_INSTANCE_TESTS=1``.
    """

    def test_preview_is_read_only_and_internally_consistent(self):
        before = {str(p): _digest(p) for p in (PROD_GATE, PROD_SEEN)}
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        res = subprocess.run(
            [str(ROOT / "bin" / "ca"), "daily", "deliver", "--dry-run"],
            capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=300,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("CRON_FAILURE", res.stderr)
        payload = json.loads(res.stdout)
        self.assertTrue(payload.get("dry_run"))
        self.assertEqual(payload["mode"], "ok")
        # The recipient is whatever this instance configured; never a value baked
        # into the toolkit or into the test.
        self.assertIn("@", payload["email_to"])

        # Internal consistency, not today's volatile production count.
        subject = payload["email_subject"]
        self.assertIn("内容收藏夹日报", subject)
        match = re.search(r"新增 (\d+) 篇", subject)
        self.assertIsNotNone(match, subject)
        self.assertEqual(int(match.group(1)), payload["new_count"])
        self.assertGreaterEqual(payload["new_count"], 0)
        self.assertIn(f"新增 {payload['new_count']} 篇", payload["weixin_text"])
        self.assertGreater(payload["email_plain_chars"], 0)
        self.assertLessEqual(payload["weixin_chars"], 200)
        self.assertEqual(payload["weixin_chars"], len(payload["weixin_text"]))
        self.assertIn("完整日报已发送至邮箱。", payload["weixin_text"])
        self.assertTrue(payload["weixin_target"].startswith("weixin:"))
        self.assertTrue(payload["smtp_host_configured"])

        after = {str(p): _digest(p) for p in (PROD_GATE, PROD_SEEN)}
        self.assertEqual(before, after, "dry-run must not modify production state")


class ZZProductionStateGuard(unittest.TestCase):
    """Runs last (alphabetical): the suite must not have touched instance state."""

    def test_instance_state_files_unchanged(self):
        for path, expected in _PROD_SNAPSHOT.items():
            self.assertEqual(_digest(Path(path)), expected, f"{path} was modified by the tests")


if __name__ == "__main__":
    unittest.main(verbosity=2)
