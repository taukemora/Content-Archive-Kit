#!/usr/bin/env python3
"""Acceptance cover for maintaining an already-populated archive.

Two independent behaviours are exercised end to end through the copied real
``bin/ca`` launcher and the copied real ``scripts/`` implementation:

1. a target that already exists is found by ``search``, updated in place by
   ``finalize`` (Chinese title, validated summary, category/context/tags), then
   reindexed, searched again and audited;
2. an existing target is refreshed with ``add --force`` (new body plus a new raw
   snapshot), finalized, reindexed, searched and audited, and the previous copy
   is proven to be soft-replaced into ``trash/`` rather than duplicated.

The baseline is a complete synthetic archive (entries, raw snapshots, local
images, seen state and a previously built index).  Each test copies that
baseline and works only on the copy, so the pristine source archive can be
compared byte for byte afterwards.

Offline guarantees: ``add`` is driven with ``--content-file``/``--raw-file``
inside the working archive's ``.state/`` and the fixture URL uses a public IP
literal, so ``assert_public_url`` returns before any resolver or HTTP call.
``publish.yaml`` is absent, and ``CA_SKIP_AUTO_PUBLISH`` is stripped from the
child environment instead of being set, so the publish hook takes its
"unconfigured" branch rather than being globally disabled.
"""
from __future__ import annotations

import base64
import csv
import gzip
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
sys.path.insert(0, str(ROOT / "scripts"))
import common as ca_common  # noqa: E402

PYTHON = _support.PYTHON
SCRATCH = _support.SCRATCH

# A public, routable IP literal: ``assert_public_url`` recognises it as an IP
# address and returns before ``socket.getaddrinfo``, so the acceptance path has
# no DNS or HTTP dependency at all.
HOST_IP = "93.184.216.34"
SOURCE = "Synthetic Vendor Alpha"

TARGET_URL = ca_common.dedupe_key(f"https://{HOST_IP}/archive/legacy-target")
TARGET_ID = "2026-10-01-legacy-synthetic-archive-target"
LEGACY_TITLE = "旧版合成标题：离线验收基线"
NEW_TITLE = "新版合成标题：离线验收更新"
REVISION_TITLE = "修订版合成标题：离线验收更新"

LEGACY_SUMMARY = (
    "这是一段用于离线验收的合成摘要，描述目标条目在基线状态下的旧版正文与旧版分类。"
    "摘要只包含虚构的测试信息，不涉及任何真实文章、真实作者或真实机构。"
    "长度控制在一百到一百五十个汉字之间，使短文章的长度校验可以通过，"
    "同时为更新后的标题、摘要和分类对比留下明确差异。"
)
NEW_SUMMARY = (
    "这是一段用于离线验收的合成摘要，描述目标条目在更新之后的正文与分类。"
    "更新只改动了标题、摘要和分类等必要字段，正文全文保持原样，"
    "以便验证既有归档在维护流程中不会被无关内容牵连。"
    "摘要仍然只包含虚构的测试信息，长度同样控制在一百到一百五十个汉字之间。"
)
UNRELATED_SUMMARY = (
    "这是无关条目使用的合成摘要，用来确认更新流程没有改动它所在的目录和全部文件。"
    "摘要内容完全虚构，只包含测试用的描述，不涉及任何真实文章或真实机构。"
    "长度同样控制在一百到一百五十个汉字之间，保证审计在短文章规则下通过。"
    "整个过程不访问网络，也不会写入任何真实归档。"
)

LEGACY_BODY_TOKEN = "LEGACY-BODY-TOKEN"
REVISED_BODY_TOKEN = "REVISED-BODY-TOKEN"
UNRELATED_TOKENS = ("UNRELATED-ALPHA-TOKEN", "UNRELATED-BETA-TOKEN")

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

ENTRIES = (
    {
        "id": TARGET_ID,
        "title": LEGACY_TITLE,
        "original_title": "Legacy Synthetic Archive Target",
        "url": TARGET_URL,
        "category": "tech",
        "context": "work",
        "tags": ["synthetic", "legacy"],
        "published": "2026-10-01T09:00:00+08:00",
        "archived": "2026-10-02T08:15:00+08:00",
        "summary": LEGACY_SUMMARY,
        "token": LEGACY_BODY_TOKEN,
        "relevance": "high",
        "target": True,
    },
    {
        "id": "2026-09-12-unrelated-society-note",
        "title": "无关条目甲：社会观察合成稿",
        "original_title": "Unrelated Synthetic Society Note",
        "url": ca_common.dedupe_key(f"https://{HOST_IP}/archive/unrelated-society"),
        "category": "society",
        "context": "reference",
        "tags": ["synthetic", "unrelated"],
        "published": "2026-09-12T10:30:00+08:00",
        "archived": "2026-09-13T07:45:00+08:00",
        "summary": UNRELATED_SUMMARY,
        "token": UNRELATED_TOKENS[0],
        "relevance": "",
        "target": False,
    },
    {
        "id": "2026-09-20-unrelated-tech-note",
        "title": "无关条目乙：技术笔记合成稿",
        "original_title": "Unrelated Synthetic Tech Note",
        "url": ca_common.dedupe_key(f"https://{HOST_IP}/archive/unrelated-tech"),
        "category": "tech",
        "context": "learning",
        "tags": ["synthetic", "unrelated"],
        "published": "2026-09-20T14:00:00+08:00",
        "archived": "2026-09-21T09:05:00+08:00",
        "summary": UNRELATED_SUMMARY,
        "token": UNRELATED_TOKENS[1],
        "relevance": "",
        "target": False,
    },
)

TARGET = ENTRIES[0]
UNRELATED = [spec for spec in ENTRIES if not spec["target"]]


def entry_rel_dir(spec: dict) -> str:
    return f"library/{spec['category']}/{spec['id']}"


def _sha256_file(path: Path) -> str:
    return sha256(Path(path).read_bytes()).hexdigest()


def tree_digest(root: Path) -> dict[str, str]:
    """Relative POSIX path -> SHA-256 for every file under ``root``."""
    root = Path(root)
    return {
        p.relative_to(root).as_posix(): _sha256_file(p)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def seen_records(root: Path) -> dict:
    data = json.loads((Path(root) / ".state" / "seen.json").read_text(encoding="utf-8"))
    return data.get("entries", {})


def seen_semantics(record: dict) -> dict:
    """The fields that identify a seen entry, ignoring rewritten bookkeeping."""
    return {
        "id": record.get("id"),
        "path": record.get("path"),
        "first_seen": record.get("first_seen"),
    }


def entry_body(spec: dict) -> str:
    return (
        f"# {spec['title']}\n"
        "\n"
        f"> 来源：{SOURCE} · 作者：合成作者 · 发布：{spec['published'][:10]} · [原文]({spec['url']})\n"
        "\n"
        "## 摘要\n"
        "\n"
        f"{spec['summary']}\n"
        "\n"
        "## 全文\n"
        "\n"
        "合成正文第一段：这段文字完全虚构，只用于离线验收，不包含真实文章内容。\n"
        "![合成示意图](images/figure.png)\n"
        f"合成正文第二段：本条的正文标记是 {spec['token']}，用于确认更新前后的正文差异。\n"
    )


class ExistingArchiveAcceptance(unittest.TestCase):
    def setUp(self):
        self.baseline = self._build_baseline()
        self.baseline_digest = tree_digest(self.baseline)

        self.work = _support.scratch_dir("ca-existing-work-")
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        shutil.copytree(self.baseline, self.work, dirs_exist_ok=True)

        self.unrelated_before = {
            spec["id"]: tree_digest(self.work / entry_rel_dir(spec)) for spec in UNRELATED
        }
        records = seen_records(self.work)
        self.seen_before = {
            spec["id"]: seen_semantics(records[ca_common.dedupe_key(spec["url"])])
            for spec in UNRELATED
        }

        self.assertTrue(
            (self.work / "bin" / "ca").is_file(), "copied launcher bin/ca is missing"
        )
        self.assertFalse(
            (self.work / "publish.yaml").exists(), "fixture must not configure publication"
        )
        sources = ca_common.load_yaml(self.work / "sources.yaml")
        self.assertEqual(
            [src for src in sources.get("sources") or [] if src.get("enabled")],
            [],
            "fixture must ship only disabled watched sources",
        )
        for spec in ENTRIES:
            entry_dir = self.work / entry_rel_dir(spec)
            for name in ("entry.md", "raw.html.gz", "images/figure.png"):
                self.assertTrue(
                    (entry_dir / name).is_file(),
                    f"baseline entry {spec['id']} is missing {name}",
                )

    # ------------------------------------------------------------------ setup

    def _build_baseline(self) -> Path:
        """Create the pristine synthetic archive; never touched by a test."""
        baseline = _support.scratch_dir("ca-existing-baseline-")
        self.addCleanup(shutil.rmtree, baseline, ignore_errors=True)

        shutil.copytree(
            ROOT / "scripts", baseline / "scripts", ignore=shutil.ignore_patterns("__pycache__")
        )
        shutil.copytree(ROOT / "bin", baseline / "bin")
        # Shipped examples: every watched source is disabled and no publish.yaml exists.
        for name in ("taxonomy.yaml", "sources.yaml", "media.yaml"):
            shutil.copy2(_support.template(name), baseline / name)

        (baseline / ".state").mkdir(parents=True, exist_ok=True)
        seen: dict[str, dict] = {}
        for spec in ENTRIES:
            entry_dir = baseline / entry_rel_dir(spec)
            entry_dir.mkdir(parents=True)

            body = entry_body(spec)
            frontmatter = {
                "id": spec["id"],
                "title": spec["title"],
                "original_title": spec["original_title"],
                "title_status": "translated",
                "source": SOURCE,
                "source_type": "tech_blog",
                "author": "合成作者",
                "published": spec["published"],
                "date_source": "feed",
                "date_precision": "day",
                "published_unknown": False,
                "published_original": "",
                "source_display_date": "",
                "archived": spec["archived"],
                "url": spec["url"],
                "canonical_url": spec["url"],
                "category": spec["category"],
                "subcategory": None,
                "context": spec["context"],
                "collection": "manual",
                "tags": list(spec["tags"]),
                "summary_chars": ca_common.count_summary_chars(spec["summary"]),
                "status": "archived",
                "content_note": "",
                "content_hash": ca_common.compute_content_hash(body),
                "raw_file": "raw.html.gz",
                "images": 1,
                "image_note": "",
                "extraction": "web_extract",
                "language": "zh",
                "confidence": 1.0,
                "model": "",
                "relevance": spec["relevance"],
            }
            ca_common.write_entry(entry_dir / "entry.md", frontmatter, body)

            (entry_dir / "raw.html.gz").write_bytes(
                gzip.compress(
                    f"<html><body>{spec['token']} synthetic raw snapshot</body></html>".encode(
                        "utf-8"
                    )
                )
            )
            (entry_dir / "images").mkdir()
            (entry_dir / "images" / "figure.png").write_bytes(PNG_1PX)

            seen[spec["url"]] = {
                "id": spec["id"],
                "path": str(Path(entry_rel_dir(spec)) / "entry.md"),
                "first_seen": spec["archived"],
            }

        (baseline / ".state" / "seen.json").write_text(
            json.dumps({"entries": seen}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        # Build the index with the real CLI so the archive starts in the same
        # shape an existing installation would be in.
        result = self._ca(baseline, "index")
        publication = json.loads(result.stdout)["publication"]
        self.assertEqual(publication["status"], "skipped", publication)
        self.assertIn("publish.yaml", publication["reason"])
        return baseline

    # --------------------------------------------------------------- plumbing

    def _ca(self, root: Path, *args: str) -> subprocess.CompletedProcess:
        # CA_SKIP_AUTO_PUBLISH is stripped rather than set: the publish hook must
        # reach its "publish.yaml missing" branch, not a global bypass.
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("PYTHONPATH", "CA_SKIP_AUTO_PUBLISH")
        }
        env.update(
            {
                "CA_PYTHON": str(PYTHON),
                "CA_SCRATCH_ROOT": str(SCRATCH),
                "CA_TEST_SCRATCH": str(SCRATCH),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        result = subprocess.run(
            [str(Path(root) / "bin" / "ca"), *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"bin/ca {' '.join(args)} failed ({result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        return result

    def _json(self, root: Path, *args: str):
        return json.loads(self._ca(root, *args).stdout)

    def _search(self, *args: str) -> list:
        return self._json(self.work, "search", *args, "--json")

    def _find_target_entry(self) -> Path:
        matches = [p for p in (self.work / "library").rglob("entry.md") if p.parent.name == TARGET_ID]
        self.assertEqual(len(matches), 1, f"expected one active entry {TARGET_ID}, got {matches}")
        return matches[0]

    def _fulltext(self, path: Path) -> str:
        _, body = ca_common.read_entry(path)
        self.assertIn("## 全文", body)
        return body.split("## 全文", 1)[1]

    # ------------------------------------------------------------ assertions

    def _assert_target_reflects(self, title: str, summary: str, category: str) -> Path:
        """The active target must carry the new values and the new category path."""
        path = self._find_target_entry()
        self.assertEqual(
            path.parent.parent.name, category, "target was not filed under the new category"
        )
        frontmatter, body = ca_common.read_entry(path)
        self.assertEqual(frontmatter["title"], title)
        self.assertEqual(frontmatter["category"], category)
        self.assertIn(summary, body)
        self.assertEqual(
            frontmatter["summary_chars"], ca_common.count_summary_chars(summary)
        )
        headings = [text for _, text in ca_common.atx_h1_lines(body)]
        self.assertEqual(headings, [title])
        return path

    def _assert_unrelated_unchanged(self) -> None:
        for spec in UNRELATED:
            rel = entry_rel_dir(spec)
            path = self.work / rel
            self.assertTrue(path.is_dir(), f"unrelated entry directory disappeared: {rel}")
            self.assertEqual(
                tree_digest(path),
                self.unrelated_before[spec["id"]],
                f"unrelated entry files changed: {rel}",
            )

    def _assert_seen_unchanged(self) -> None:
        records = seen_records(self.work)
        for spec in UNRELATED:
            key = ca_common.dedupe_key(spec["url"])
            self.assertIn(key, records, f"unrelated seen record disappeared: {key}")
            self.assertEqual(
                seen_semantics(records[key]),
                self.seen_before[spec["id"]],
                f"unrelated seen record changed: {key}",
            )

    def _assert_source_archive_untouched(self) -> None:
        self.assertEqual(
            tree_digest(self.baseline),
            self.baseline_digest,
            "the pristine source archive was modified by the acceptance run",
        )

    def _assert_archive_consistent(self, expected_count: int) -> None:
        """Active entries, index rows and seen records agree on count/id/url/path."""
        paths = sorted((self.work / "library").rglob("entry.md"))
        self.assertEqual(len(paths), expected_count, paths)

        entries: dict[str, tuple] = {}
        for path in paths:
            frontmatter, body = ca_common.read_entry(path)
            entries[path.relative_to(self.work).as_posix()] = (frontmatter, body)

        with open(self.work / "index.csv", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), expected_count, rows)

        records = seen_records(self.work)
        self.assertEqual(len(records), expected_count, records)

        entry_ids = {frontmatter["id"] for frontmatter, _ in entries.values()}
        entry_urls = {
            ca_common.dedupe_key(frontmatter.get("canonical_url") or frontmatter["url"])
            for frontmatter, _ in entries.values()
        }

        self.assertEqual(set(entries), {row["path"] for row in rows})
        self.assertEqual(set(entries), {record.get("path") for record in records.values()})
        self.assertEqual(entry_ids, {row["id"] for row in rows})
        self.assertEqual(entry_ids, {record.get("id") for record in records.values()})
        self.assertEqual(entry_urls, set(records))
        self.assertEqual(entry_urls, {row["url"] for row in rows})

        for row in rows:
            frontmatter, _ = entries[row["path"]]
            self.assertEqual(row["url"], frontmatter["url"])
            self.assertEqual(row["title"], frontmatter["title"])
            self.assertEqual(row["category"], frontmatter["category"])
            self.assertEqual(row["summary_chars"], str(frontmatter["summary_chars"]))

    # ------------------------------------------------------------------ tests

    def test_existing_target_update_via_finalize_reindexes_and_audits(self):
        legacy_path = self._find_target_entry()
        legacy_fulltext = self._fulltext(legacy_path)

        # 1. The existing archive is searchable before any maintenance.
        found = self._search(LEGACY_TITLE)
        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0]["id"], TARGET_ID)
        self.assertEqual(found[0]["title"], LEGACY_TITLE)
        self.assertEqual(found[0]["url"], TARGET_URL)
        self.assertIn(LEGACY_BODY_TOKEN, self._search(LEGACY_BODY_TOKEN, "--full")[0]["snippet"])

        # 2. Finalize the necessary editorial fields; the category change also
        #    relocates the entry directory and rewrites its seen path.
        self._json(
            self.work,
            "finalize",
            TARGET_ID,
            "--title-zh",
            NEW_TITLE,
            "--summary",
            NEW_SUMMARY,
            "--category",
            "business",
            "--context",
            "learning",
            "--tags",
            "synthetic,updated",
        )

        path = self._assert_target_reflects(NEW_TITLE, NEW_SUMMARY, "business")
        self.assertFalse(legacy_path.exists(), "the old category path must not survive the move")
        self.assertEqual(self._fulltext(path), legacy_fulltext, "finalize must not rewrite the body")

        text = path.read_text(encoding="utf-8")
        self.assertNotIn(LEGACY_TITLE, text, "the old display title must be replaced everywhere")
        self.assertNotIn(LEGACY_SUMMARY, text, "the old summary must be replaced")
        self.assertIn(LEGACY_BODY_TOKEN, text, "the archived body itself must be preserved")
        self.assertFalse(
            (self.work / "trash").exists(),
            "an in-place finalize must not soft-delete the entry it updates",
        )

        # 3. Reindex and search: new values are visible, old values are gone.
        self._json(self.work, "index")
        rows = self._search(NEW_TITLE)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["category"], "business")
        self.assertEqual(rows[0]["path"], path.relative_to(self.work).as_posix())
        self.assertEqual(self._search(LEGACY_TITLE), [], "the old title still matches search")
        self.assertEqual(len(self._search(LEGACY_BODY_TOKEN, "--full")), 1)

        index_text = (self.work / "index.csv").read_text(encoding="utf-8-sig")
        self.assertIn(NEW_TITLE, index_text)
        self.assertNotIn(LEGACY_TITLE, index_text)
        self.assertNotIn(LEGACY_SUMMARY, index_text)
        html_text = (self.work / "index.html").read_text(encoding="utf-8")
        self.assertIn(NEW_TITLE, html_text)
        self.assertNotIn(LEGACY_TITLE, html_text)

        # 4. Audit the whole archive.
        report = self._json(self.work, "audit")
        self.assertTrue(report["ok"], report["issues"])
        self.assertEqual(report["entries"], len(ENTRIES))
        self.assertEqual(report["issues"], [])

        self._assert_archive_consistent(len(ENTRIES))
        self._assert_unrelated_unchanged()
        self._assert_seen_unchanged()
        self._assert_source_archive_untouched()

    def test_existing_target_force_refresh_soft_replaces_and_reaudits(self):
        legacy_path = self._find_target_entry()
        legacy_dir = legacy_path.parent
        legacy_entry_bytes = legacy_path.read_bytes()
        legacy_raw_bytes = (legacy_dir / "raw.html.gz").read_bytes()
        legacy_seen = seen_records(self.work)[TARGET_URL]["first_seen"]

        self.assertEqual(self._search(LEGACY_BODY_TOKEN, "--full")[0]["id"], TARGET_ID)

        # Synthetic revision inputs live inside the working archive's .state/,
        # the only project-local location the CLI accepts for user files.
        inputs = self.work / ".state" / "fixtures"
        inputs.mkdir(parents=True)
        content_file = inputs / "revision.md"
        content_file.write_text(
            "合成修订正文第一段：这是用于验证强制更新流程的合成正文，内容完全虚构。\n"
            f"合成修订正文第二段：修订标记 {REVISED_BODY_TOKEN} 只存在于这一版正文里。\n",
            encoding="utf-8",
        )
        raw_file = inputs / "revision-raw.html.gz"
        raw_file.write_bytes(gzip.compress(b"<html><body>revision raw snapshot</body></html>"))

        result = self._json(
            self.work,
            "add",
            TARGET_URL,
            "--force",
            "--content-file",
            str(content_file.relative_to(self.work)),
            "--raw-file",
            str(raw_file.relative_to(self.work)),
            "--original-title",
            TARGET["original_title"],
            "--published",
            TARGET["published"],
            "--language",
            "zh",
        )
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["id"], TARGET_ID)
        self.assertEqual(Path(result["path"]), legacy_path, "the entry must be refreshed in place")

        # Soft replacement: exactly one trashed copy holding the old entry, raw
        # snapshot and local image; nothing was deleted.
        trash_dirs = [p for p in (self.work / "trash").iterdir() if p.is_dir()]
        self.assertEqual(len(trash_dirs), 1, trash_dirs)
        trashed = trash_dirs[0]
        self.assertRegex(trashed.name, r"^\d{8}-\d{6}-")
        self.assertTrue(trashed.name.endswith(TARGET_ID), trashed.name)
        self.assertEqual((trashed / "entry.md").read_bytes(), legacy_entry_bytes)
        self.assertEqual((trashed / "raw.html.gz").read_bytes(), legacy_raw_bytes)
        self.assertEqual((trashed / "images" / "figure.png").read_bytes(), PNG_1PX)

        # The refreshed entry carries the new body and inherits the metadata the
        # caller did not resupply.
        refreshed_text = legacy_path.read_text(encoding="utf-8")
        self.assertIn(REVISED_BODY_TOKEN, refreshed_text)
        self.assertNotIn(LEGACY_BODY_TOKEN, refreshed_text)
        refreshed, _ = ca_common.read_entry(legacy_path)
        self.assertEqual(refreshed["category"], TARGET["category"])
        self.assertEqual(refreshed["context"], TARGET["context"])
        self.assertEqual(refreshed["collection"], "manual")
        self.assertEqual(refreshed["tags"], list(TARGET["tags"]))
        self.assertEqual(refreshed["relevance"], TARGET["relevance"])
        self.assertEqual(refreshed["raw_file"], "revision-raw.html.gz")
        self.assertEqual((legacy_dir / "revision-raw.html.gz").read_bytes(), raw_file.read_bytes())
        self.assertFalse((legacy_dir / "raw.html.gz").exists(), "the old raw snapshot must not linger")

        # No duplicate active URL: one live entry, one index row, one seen key.
        active = [
            p
            for p in (self.work / "library").rglob("entry.md")
            if ca_common.read_entry(p)[0].get("url") == TARGET_URL
        ]
        self.assertEqual(len(active), 1, active)
        records = seen_records(self.work)
        self.assertEqual(records[TARGET_URL]["id"], TARGET_ID)
        self.assertEqual(
            sum(1 for record in records.values() if record.get("id") == TARGET_ID), 1
        )
        self.assertEqual(records[TARGET_URL]["first_seen"], legacy_seen)

        # Finalize the refreshed entry, then reindex, search and audit.
        self._json(
            self.work,
            "finalize",
            TARGET_ID,
            "--title-zh",
            REVISION_TITLE,
            "--summary",
            NEW_SUMMARY,
            "--tags",
            "synthetic,revised",
        )
        path = self._assert_target_reflects(REVISION_TITLE, NEW_SUMMARY, TARGET["category"])
        self.assertEqual(path, legacy_path)

        self._json(self.work, "index")
        rows = self._search(REVISION_TITLE)
        self.assertEqual(len(rows), 1, rows)
        with open(self.work / "index.csv", encoding="utf-8-sig", newline="") as handle:
            index_rows = list(csv.DictReader(handle))
        self.assertEqual(sum(1 for row in index_rows if row["url"] == TARGET_URL), 1)
        self.assertIn(REVISED_BODY_TOKEN, self._search(REVISED_BODY_TOKEN, "--full")[0]["snippet"])
        self.assertEqual(self._search(LEGACY_TITLE), [], "the old title still matches search")
        self.assertEqual(
            self._search(LEGACY_BODY_TOKEN, "--full"), [], "the old body still matches full-text search"
        )

        text = path.read_text(encoding="utf-8")
        self.assertNotIn(LEGACY_BODY_TOKEN, text)
        self.assertNotIn(LEGACY_SUMMARY, text)
        index_text = (self.work / "index.csv").read_text(encoding="utf-8-sig")
        self.assertIn(REVISION_TITLE, index_text)
        self.assertNotIn(LEGACY_TITLE, index_text)
        self.assertNotIn(LEGACY_BODY_TOKEN, index_text)

        report = self._json(self.work, "audit")
        self.assertTrue(report["ok"], report["issues"])
        self.assertEqual(report["entries"], len(ENTRIES))

        self._assert_archive_consistent(len(ENTRIES))
        self._assert_unrelated_unchanged()
        self._assert_seen_unchanged()
        self._assert_source_archive_untouched()

    def test_negative_controls_detect_skipped_update_and_corrupted_copy(self):
        """Prove the key assertions fail when the maintenance step is skipped.

        The mutations happen only inside this test's throwaway working copy; the
        pristine source archive is re-hashed at the end.
        """
        # (a) Without the update, the "new value is present" probe must fail and
        #     search must not surface the new title.
        with self.assertRaises(AssertionError):
            self._assert_target_reflects(NEW_TITLE, NEW_SUMMARY, "business")
        self.assertEqual(self._search(NEW_TITLE), [])
        self.assertEqual(self._search(LEGACY_TITLE)[0]["title"], LEGACY_TITLE)

        # (b) A byte-level change to one unrelated entry must be detected.
        victim_spec = UNRELATED[0]
        victim = self.work / entry_rel_dir(victim_spec) / "images" / "figure.png"
        victim.write_bytes(victim.read_bytes() + b"\x00")
        with self.assertRaises(AssertionError):
            self._assert_unrelated_unchanged()

        # (c) A rewritten unrelated seen record must be detected.
        seen_file = self.work / ".state" / "seen.json"
        data = json.loads(seen_file.read_text(encoding="utf-8"))
        key = ca_common.dedupe_key(UNRELATED[1]["url"])
        data["entries"][key]["id"] = "rewritten-unrelated-id"
        seen_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with self.assertRaises(AssertionError):
            self._assert_seen_unchanged()

        self._assert_source_archive_untouched()


if __name__ == "__main__":
    unittest.main()
