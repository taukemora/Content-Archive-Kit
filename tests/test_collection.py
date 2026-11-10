#!/usr/bin/env python3
"""Collection provenance (manual/auto) tests in an isolated sandbox ROOT.

Every project tree is written under the platform temporary directory; the production
``library/``, ``.state/seen.json`` and ``index.csv`` are never touched and no
network call is made (fetch/SSRF checks are patched).
"""
from __future__ import annotations

import csv
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
SCRIPTS = ROOT / "scripts"
SCRATCH = _support.SCRATCH
sys.path.insert(0, str(SCRIPTS))

import archive_url  # noqa: E402
import common  # noqa: E402
import search  # noqa: E402

URL = "https://blog.google/technology/ai/collection-fixture"
URL_ALT = "https://blog.google/technology/ai/collection-fixture-mirror"
BODY = (
    "Collection provenance paragraph one with enough characters for the extractor "
    "and for the content hash to be considered meaningful rather than accidental."
)


def split_frontmatter(text: str) -> tuple[list[str], str]:
    assert text.startswith("---\n"), "entry.md is missing its frontmatter"
    end = text.index("\n---", 4)
    return text[4:end].split("\n"), text[end:]


class FakeResponse:
    def __init__(self, html: str):
        self.text = html
        self.content = html.encode("utf-8")
        self.headers = {"Content-Type": "text/html; charset=utf-8"}


class SandboxCase(unittest.TestCase):
    def setUp(self):
        self._dirs: list[Path] = []

    def tearDown(self):
        for d in self._dirs:
            shutil.rmtree(d, ignore_errors=True)

    def temp_dir(self, prefix: str) -> Path:
        d = Path(tempfile.mkdtemp(dir=str(SCRATCH), prefix=prefix))
        self._dirs.append(d)
        return d

    def make_project(self) -> Path:
        base = self.temp_dir("collection_project_")
        for name in ("taxonomy.yaml", "sources.yaml"):
            shutil.copy2(_support.template(name), base / name)
        (base / "library" / "tech").mkdir(parents=True)
        (base / ".state").mkdir()
        (base / ".state" / "seen.json").write_text('{"entries": {}}', encoding="utf-8")
        return base

    def html(self, body: str = BODY, title: str = "collection fixture",
             image: bool = False) -> str:
        img = '<p><img src="https://blog.example/pic.png" alt="pic"></p>' if image else ""
        return (
            f"<html><head><title>{title}</title></head><body><article>"
            f"<h1>{title}</h1><p>{body}</p>{img}"
            "</article></body></html>"
        )


class CollectionCase(SandboxCase):
    def run_add(self, project: Path, extra: list[str], body: str = BODY,
                url: str = URL, image: bool = False) -> tuple[int, dict, str]:
        argv = [
            "archive_url.py", url,
            "--title-zh", "collection fixture",
            "--category", "tech",
            "--context", "reference",
        ] + extra
        out = io.StringIO()
        with mock.patch.object(archive_url, "ROOT", project), mock.patch.object(
            common, "ROOT", project
        ), mock.patch.object(archive_url, "assert_public_url", lambda u: None), mock.patch.object(
            archive_url, "fetch", lambda *a, **k: FakeResponse(self.html(body, image=image))
        ), mock.patch.object(sys, "argv", argv), redirect_stdout(out):
            code = archive_url.main()
        payload = json.loads(out.getvalue() or "{}")
        return code, payload, out.getvalue()

    def read_entry(self, project: Path) -> tuple[dict, str, Path]:
        paths = sorted((project / "library").rglob("entry.md"))
        self.assertEqual(len(paths), 1, f"expected exactly one entry, got {paths}")
        fm, body = common.read_entry(paths[0])
        return fm, body, paths[0]

    # ------------------------------------------------------------------ new
    def test_new_entry_defaults_to_manual(self):
        project = self.make_project()
        code, payload, _ = self.run_add(project, [])
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["collection"], "manual")
        fm, _, _ = self.read_entry(project)
        self.assertEqual(fm["collection"], "manual")

    def test_new_entry_explicit_auto(self):
        project = self.make_project()
        code, payload, _ = self.run_add(project, ["--collection", "auto"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["collection"], "auto")
        fm, _, _ = self.read_entry(project)
        self.assertEqual(fm["collection"], "auto")

    def test_invalid_collection_value_rejected(self):
        project = self.make_project()
        with self.assertRaises(SystemExit) as ctx:
            self.run_add(project, ["--collection", "unknown"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(list((project / "library").rglob("entry.md")), [])

    # -------------------------------------------------------------- url dup
    def test_url_duplicate_default_add_promotes_auto_to_manual(self):
        project = self.make_project()
        code, _, _ = self.run_add(project, ["--collection", "auto"])
        self.assertEqual(code, 0)
        _, _, entry_path = self.read_entry(project)
        before = entry_path.read_text(encoding="utf-8")
        seen_before = (project / ".state" / "seen.json").read_bytes()

        code, payload, _ = self.run_add(project, [])
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "duplicate")
        self.assertIn("entry", payload)  # existing duplicate contract
        self.assertIn("url", payload)
        self.assertEqual(payload["collection"], "manual")
        self.assertTrue(payload["promoted"])

        after = entry_path.read_text(encoding="utf-8")
        fm_lines_before, body_before = split_frontmatter(before)
        fm_lines_after, body_after = split_frontmatter(after)
        self.assertEqual(body_after, body_before)
        changed = [
            (a, b) for a, b in zip(fm_lines_after, fm_lines_before) if a != b
        ]
        self.assertEqual(changed, [("collection: manual", "collection: auto")])
        self.assertEqual(
            [ln for ln in fm_lines_after if not ln.startswith("collection:")],
            [ln for ln in fm_lines_before if not ln.startswith("collection:")],
        )
        self.assertEqual((project / ".state" / "seen.json").read_bytes(), seen_before)
        self.assertEqual(list(entry_path.parent.glob("*.tmp")), [])

    def test_url_duplicate_explicit_manual_promotes_auto(self):
        project = self.make_project()
        self.run_add(project, ["--collection", "auto"])
        code, payload, _ = self.run_add(project, ["--collection", "manual"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["collection"], "manual")
        fm, _, _ = self.read_entry(project)
        self.assertEqual(fm["collection"], "manual")

    def test_url_duplicate_explicit_auto_keeps_auto(self):
        project = self.make_project()
        self.run_add(project, ["--collection", "auto"])
        code, payload, _ = self.run_add(project, ["--collection", "auto"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["collection"], "auto")
        self.assertFalse(payload["promoted"])
        fm, _, _ = self.read_entry(project)
        self.assertEqual(fm["collection"], "auto")

    def test_url_duplicate_explicit_auto_never_downgrades_manual(self):
        project = self.make_project()
        self.run_add(project, [])
        code, payload, _ = self.run_add(project, ["--collection", "auto"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["collection"], "manual")
        fm, _, _ = self.read_entry(project)
        self.assertEqual(fm["collection"], "manual")

    def test_url_duplicate_promotes_legacy_entry_without_collection(self):
        project = self.make_project()
        self.run_add(project, ["--collection", "auto"])
        _, _, entry_path = self.read_entry(project)
        text = entry_path.read_text(encoding="utf-8")
        entry_path.write_text(
            "\n".join(ln for ln in text.split("\n") if not ln.startswith("collection:")),
            encoding="utf-8",
        )
        before_body = common.read_entry(entry_path)[1]
        code, payload, _ = self.run_add(project, [])
        self.assertEqual(code, 0)
        self.assertEqual(payload["collection"], "manual")
        fm, body, _ = self.read_entry(project)
        self.assertEqual(fm["collection"], "manual")
        self.assertEqual(body, before_body)

    # ------------------------------------------------------- content hash dup
    def test_content_hash_duplicate_default_add_promotes_auto(self):
        project = self.make_project()
        self.run_add(project, ["--collection", "auto"], url=URL)
        code, payload, _ = self.run_add(project, [], url=URL_ALT)
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "duplicate")
        self.assertEqual(payload["matched_by"], "content_hash")
        self.assertEqual(payload["collection"], "manual")
        self.assertTrue(payload["promoted"])
        fm, _, _ = self.read_entry(project)
        self.assertEqual(fm["collection"], "manual")

    def test_content_hash_duplicate_explicit_auto_keeps_auto(self):
        project = self.make_project()
        self.run_add(project, ["--collection", "auto"], url=URL)
        code, payload, _ = self.run_add(project, ["--collection", "auto"], url=URL_ALT)
        self.assertEqual(code, 0)
        self.assertEqual(payload["collection"], "auto")
        self.assertFalse(payload["promoted"])
        fm, _, _ = self.read_entry(project)
        self.assertEqual(fm["collection"], "auto")

    # ----------------------------------------------------------------- force
    def test_force_inherits_auto(self):
        project = self.make_project()
        self.run_add(project, ["--collection", "auto"])
        code, payload, _ = self.run_add(project, ["--force"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["collection"], "auto")
        fm, _, _ = self.read_entry(project)
        self.assertEqual(fm["collection"], "auto")

    def test_force_inherits_manual(self):
        project = self.make_project()
        self.run_add(project, [])
        code, payload, _ = self.run_add(project, ["--force"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["collection"], "manual")
        fm, _, _ = self.read_entry(project)
        self.assertEqual(fm["collection"], "manual")

    def test_force_explicit_auto_never_downgrades_manual(self):
        project = self.make_project()
        self.run_add(project, [])
        code, payload, _ = self.run_add(project, ["--force", "--collection", "auto"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["collection"], "manual")
        fm, _, _ = self.read_entry(project)
        self.assertEqual(fm["collection"], "manual")

    def test_force_explicit_manual_promotes_auto(self):
        project = self.make_project()
        self.run_add(project, ["--collection", "auto"])
        code, payload, _ = self.run_add(project, ["--force", "--collection", "manual"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["collection"], "manual")
        fm, _, _ = self.read_entry(project)
        self.assertEqual(fm["collection"], "manual")

    # --------------------------------------------------------- race (in-lock)
    def test_in_lock_duplicate_promotes_winner_without_touching_body(self):
        project = self.make_project()
        entry_id = f"{common.now().strftime('%Y-%m-%d')}-collection-fixture"
        winner = project / "library" / "tech" / entry_id
        calls: list[Path] = []

        def hook(content, base_url, images_dir, referer=None, **kwargs):
            calls.append(images_dir.parent)
            winner.mkdir(parents=True, exist_ok=True)
            archive_url.write_entry(
                winner / "entry.md",
                {
                    "id": entry_id,
                    "title": "collection fixture",
                    "collection": "auto",
                    "status": "archived",
                },
                "# collection fixture\n\nWINNER-BODY\n",
            )
            (winner / "raw.html.gz").write_bytes(b"winner-raw-bytes")
            seen = common.load_seen()
            seen.setdefault("entries", {})[archive_url.dedupe_key(URL)] = {
                "id": entry_id,
                "path": str((winner / "entry.md").relative_to(project)),
                "first_seen": common.iso(),
            }
            common.save_seen(seen)
            return content, 0, 0, []

        argv = [
            "archive_url.py", URL,
            "--title-zh", "collection fixture",
            "--category", "tech",
            "--context", "reference",
        ]
        out = io.StringIO()
        with mock.patch.object(archive_url, "ROOT", project), mock.patch.object(
            common, "ROOT", project
        ), mock.patch.object(archive_url, "assert_public_url", lambda u: None), mock.patch.object(
            archive_url, "fetch", lambda *a, **k: FakeResponse(self.html(image=True))
        ), mock.patch.object(archive_url, "localize_images", hook), mock.patch.object(
            sys, "argv", argv
        ), redirect_stdout(out):
            code = archive_url.main()
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "duplicate")
        self.assertEqual(payload["collection"], "manual")
        self.assertTrue(payload["promoted"])
        self.assertTrue(calls, "localize_images hook did not run")
        fm, body = common.read_entry(winner / "entry.md")
        self.assertEqual(fm["collection"], "manual")
        self.assertEqual(fm["status"], "archived")
        self.assertEqual(body.strip(), "# collection fixture\n\nWINNER-BODY")
        self.assertEqual((winner / "raw.html.gz").read_bytes(), b"winner-raw-bytes")

    # ------------------------------------------------------- untrusted paths
    def test_duplicate_ignores_seen_path_outside_library(self):
        project = self.make_project()
        outside = self.temp_dir("collection_outside_")
        target = outside / "entry.md"
        target.write_text("---\nid: outside\ncollection: auto\n---\n\n# outside\n", encoding="utf-8")
        before = target.read_bytes()
        seen = {
            "entries": {
                archive_url.dedupe_key(URL): {
                    "id": "outside",
                    "path": str(target),
                    "first_seen": common.iso(),
                }
            }
        }
        (project / ".state" / "seen.json").write_text(json.dumps(seen), encoding="utf-8")
        code, payload, _ = self.run_add(project, [])
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "duplicate")
        self.assertFalse(payload.get("promoted", False))
        self.assertEqual(target.read_bytes(), before)

    def test_force_ignores_seen_path_outside_library(self):
        project = self.make_project()
        outside = self.temp_dir("collection_outside_force_")
        victim = outside / "entry.md"
        victim.write_text("---\nid: victim\ncollection: auto\n---\n\n# victim\n", encoding="utf-8")
        before = victim.read_bytes()
        seen = {
            "entries": {
                archive_url.dedupe_key(URL): {
                    "id": "victim",
                    "path": str(victim),
                    "first_seen": common.iso(),
                }
            }
        }
        (project / ".state" / "seen.json").write_text(json.dumps(seen), encoding="utf-8")
        code, payload, _ = self.run_add(project, ["--force"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "ok")
        # Unknown provenance for an untrusted record: default manual, never touch the victim.
        self.assertEqual(payload["collection"], "manual")
        self.assertEqual(victim.read_bytes(), before)
        fm, _, _ = self.read_entry(project)
        self.assertEqual(fm["collection"], "manual")

    def test_duplicate_ignores_seen_path_with_parent_traversal(self):
        project = self.make_project()
        outside = project.parent / f"{project.name}-escape"
        outside.mkdir()
        self._dirs.append(outside)
        target = outside / "entry.md"
        target.write_text("---\nid: escaped\ncollection: auto\n---\n\n# escaped\n", encoding="utf-8")
        before = target.read_bytes()
        rel = f"library/tech/../../{outside.name}/entry.md"
        seen = {
            "entries": {
                archive_url.dedupe_key(URL): {
                    "id": "escaped",
                    "path": rel,
                    "first_seen": common.iso(),
                }
            }
        }
        (project / ".state" / "seen.json").write_text(json.dumps(seen), encoding="utf-8")
        code, payload, _ = self.run_add(project, [])
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "duplicate")
        self.assertFalse(payload.get("promoted", False))
        self.assertEqual(target.read_bytes(), before)

    def test_duplicate_promotion_does_not_deadlock(self):
        project = self.make_project()
        self.run_add(project, ["--collection", "auto"])
        result: dict = {}

        def worker():
            try:
                result["value"] = self.run_add(project, [])
            except Exception as e:  # pragma: no cover - failure path
                result["error"] = e

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(20)
        self.assertFalse(t.is_alive(), "duplicate promotion blocked on a nested state lock")
        self.assertNotIn("error", result)
        code, payload, _ = result["value"]
        self.assertEqual(code, 0)
        self.assertEqual(payload["collection"], "manual")


class SearchCollectionCase(SandboxCase):
    FIELDS = [
        "id", "published", "title", "original_title", "source", "source_type",
        "category", "subcategory", "context", "tags", "url", "summary_chars",
        "images", "status", "content_note", "relevance", "path", "archived",
        "language", "summary", "collection",
    ]

    def write_index(self, project: Path, rows: list[dict], fields=None) -> None:
        fields = fields or self.FIELDS
        with open(project / "index.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in fields})

    def run_search(self, project: Path, extra: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(search, "ROOT", project), mock.patch.object(
            sys, "argv", ["search.py"] + extra
        ), redirect_stdout(out):
            code = search.main()
        return code, out.getvalue()

    def rows(self) -> list[dict]:
        return [
            {"id": "m1", "title": "手动条目", "published": "2026-10-01",
             "category": "tech", "context": "work", "path": "library/tech/m1/entry.md",
             "collection": "manual"},
            {"id": "a1", "title": "自动条目", "published": "2026-10-02",
             "category": "tech", "context": "work", "path": "library/tech/a1/entry.md",
             "collection": "auto"},
        ]

    def test_search_filters_manual(self):
        project = self.make_project()
        self.write_index(project, self.rows())
        code, output = self.run_search(project, ["--collection", "manual", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(output)
        self.assertEqual([r["id"] for r in data], ["m1"])

    def test_search_filters_auto(self):
        project = self.make_project()
        self.write_index(project, self.rows())
        code, output = self.run_search(project, ["--collection", "auto", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(output)
        self.assertEqual([r["id"] for r in data], ["a1"])

    def test_search_rejects_invalid_collection(self):
        project = self.make_project()
        self.write_index(project, self.rows())
        with self.assertRaises(SystemExit) as ctx:
            self.run_search(project, ["--collection", "unknown"])
        self.assertEqual(ctx.exception.code, 2)

    def test_search_reports_missing_collection_column(self):
        project = self.make_project()
        self.write_index(project, self.rows(), fields=[f for f in self.FIELDS if f != "collection"])
        code, output = self.run_search(project, ["--collection", "manual"])
        self.assertEqual(code, 1)
        self.assertIn("collection", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
