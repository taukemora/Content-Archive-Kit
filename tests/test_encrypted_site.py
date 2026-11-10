"""Behavior tests for the encrypted static reading site (implementation A).

The tests are deliberately independent of the implementation's helpers: they
re-derive the published protocol from the frozen design in
docs/plans/encrypted-pages.md and use the raw ``cryptography`` primitives.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from common import write_entry  # noqa: E402

PASS = "correct horse battery staple"
WRONG = "wrong horse battery staple"
ITERATIONS = 600_000
PROTOCOL = "CA-ENC-1"
OBJECT_RE = re.compile(r"^objects/([0-9a-f]{64})\.([0-9a-f]{32})\.bin$")


# ---------------------------------------------------------------------------
# Independent protocol re-implementation (frozen design)
# ---------------------------------------------------------------------------

def _lp(value: bytes) -> bytes:
    return len(value).to_bytes(4, "big") + value


def aad(archive_id: str, purpose: str, oid: str) -> bytes:
    return b"".join(
        _lp(part)
        for part in (
            PROTOCOL.encode(),
            archive_id.encode(),
            purpose.encode(),
            oid.encode(),
        )
    )


def b64d(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


def derive_dek(site: dict, passphrase: str) -> bytes:
    kdf, wrap = site["kdf"], site["wrap"]
    kek = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=kdf["key_length"],
        salt=b64d(kdf["salt"]),
        iterations=kdf["iterations"],
    ).derive(passphrase.encode("utf-8"))
    return AESGCM(kek).decrypt(
        b64d(wrap["nonce"]),
        b64d(wrap["ciphertext"]),
        aad(site["archive_id"], "wrap", "dek"),
    )


def address_key(dek: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"",
        info=b"CA-ENC-1/address",
    ).derive(dek)


def decrypt_file(path: Path, dek: bytes, archive_id: str, purpose: str, oid: str) -> bytes:
    blob = path.read_bytes()
    if blob[:4] != b"CAE1":
        raise ValueError("bad magic")
    nonce, ciphertext = blob[4:16], blob[16:]
    return AESGCM(dek).decrypt(nonce, ciphertext, aad(archive_id, purpose, oid))


def object_id(path: Path) -> str:
    return OBJECT_RE.match(path.relative_to(path.parents[1]).as_posix()).group(1)


def object_oid_from_rel(rel: str) -> str:
    m = OBJECT_RE.match(rel)
    if not m:
        raise AssertionError(f"not an object path: {rel}")
    return m.group(1)


# ---------------------------------------------------------------------------
# Synthetic archive
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "id", "published", "title", "original_title", "source", "source_type",
    "category", "subcategory", "context", "tags", "url", "summary_chars",
    "images", "status", "content_note", "relevance", "path", "archived",
    "language", "collection", "summary",
]

TAXONOMY = """version: 1
categories:
  - slug: coding
    label: 编程与开发
  - slug: tech
    label: 科技与 AI
"""


class EncryptedSiteBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(dir=str(_support.SCRATCH))
        self.addCleanup(self.tmpdir.cleanup)
        self.base = Path(self.tmpdir.name)
        self.root = self.base / "archive"
        self.output = self.base / "site-out"
        self.keys = self.base / "keys"
        self.root.mkdir()
        (self.root / "taxonomy.yaml").write_text(TAXONOMY, encoding="utf-8")
        self.rows = []
        self.add_entry(
            "2026-01-01-alpha",
            "2026-01-01-alpha",
            "加密标题甲",
            "摘要甲：这是第一篇文章的摘要。",
            "正文甲包含独特检索词 ALPHANEEDLE，以及第二段内容。" * 3,
            image=b"\x89PNG\r\n\x1a\nALPHAIMAGE",
        )
        self.add_entry(
            "2026-01-02-beta",
            "2026-01-02-beta",
            "加密标题乙",
            "摘要乙：这是第二篇文章的摘要。",
            "正文乙包含独特检索词 BETANEEDLE，用于区分对象。" * 3,
        )
        self.write_csv()

    # -- fixtures ---------------------------------------------------------
    def add_entry(self, slug, entry_id, title, summary, body, image=None):
        entry_dir = self.root / "library" / "coding" / slug
        entry_dir.mkdir(parents=True, exist_ok=True)
        if image is not None:
            images = entry_dir / "images"
            images.mkdir(exist_ok=True)
            (images / "cover.png").write_bytes(image)
            body = body + "\n\n![封面](images/cover.png)\n"
        fm = {
            "id": entry_id,
            "title": title,
            "original_title": title,
            "source": "测试源",
            "source_type": "web",
            "author": "作者",
            "published": "2026-01-01T08:00:00+08:00",
            "archived": "2026-01-02T09:00:00+08:00",
            "collection": "manual",
            "url": f"https://example.com/SECRETURL/{entry_id}",
            "category": "coding",
            "context": "work",
            "tags": ["ALPHATAG", "测试"],
            "status": "archived",
            "summary_chars": 10,
            "content_hash": "sha256:" + "0" * 64,
        }
        write_entry(entry_dir / "entry.md", fm, body)
        self.rows.append(
            {
                "id": entry_id,
                "published": "2026-01-01",
                "title": title,
                "original_title": title,
                "source": "测试源",
                "source_type": "web",
                "category": "coding",
                "subcategory": "",
                "context": "work",
                "tags": "ALPHATAG, 测试",
                "url": f"https://example.com/SECRETURL/{entry_id}",
                "summary_chars": 10,
                "images": 1 if image else 0,
                "status": "archived",
                "content_note": "",
                "relevance": "",
                "path": f"library/coding/{slug}/entry.md",
                "archived": "2026-01-02T09:00:00+08:00",
                "language": "zh",
                "collection": "manual",
                "summary": summary,
            }
        )

    def write_csv(self):
        with (self.root / "index.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for row in self.rows:
                writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})

    def init_key(self, path: Path | None = None, passphrase: str = PASS) -> Path:
        from encrypted_site import initialize_key

        path = path or (self.keys / "site-key.json")
        initialize_key(path, passphrase)
        return path

    def build(self, key_path: Path | None = None, output: Path | None = None) -> dict:
        from encrypted_site import build_site

        key = Path(key_path) if key_path is not None else self.key_path
        if key_path is None and not key.exists():
            self.init_key(key)
        return build_site(self.root, output or self.output, key)

    # -- published helpers ------------------------------------------------
    @property
    def key_path(self) -> Path:
        return self.keys / "site-key.json"

    def site(self) -> dict:
        return json.loads((self.output / "site.json").read_text(encoding="utf-8"))

    def dek(self) -> tuple[bytes, dict]:
        site = self.site()
        return derive_dek(site, PASS), site

    def article_payloads(self) -> dict[str, dict]:
        dek, site = self.dek()
        index_file = self.output / site["index"]["file"]
        index = json.loads(
            decrypt_file(index_file, dek, site["archive_id"], "index", object_oid_from_rel(site["index"]["file"]))
        )
        out = {}
        for article in index["articles"]:
            rel = article["body"]["file"]
            raw = decrypt_file(self.output / rel, dek, site["archive_id"], "article", object_oid_from_rel(rel))
            out[article["title"]] = {"article": article, "html": raw.decode("utf-8")}
        return out


class TestInitializeKey(EncryptedSiteBase):
    def test_creates_owner_only_key_and_returns_only_public_params(self):
        from encrypted_site import initialize_key

        key = self.keys / "nested" / "site-key.json"
        params = initialize_key(key, PASS)
        self.assertTrue(key.is_file())
        self.assertFalse(key.is_symlink())
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(key.parent.stat().st_mode), 0o700)
        raw = key.read_text(encoding="utf-8")
        self.assertNotIn(PASS, raw)
        blob = json.loads(raw)
        self.assertEqual(blob["protocol"], PROTOCOL)
        self.assertEqual(blob["wrap"]["cipher"], "AES-256-GCM")
        self.assertEqual(blob["kdf"]["iterations"], ITERATIONS)
        self.assertEqual(len(b64d(blob["kdf"]["salt"])), 16)
        self.assertEqual(len(b64d(blob["wrap"]["nonce"])), 12)
        self.assertEqual(len(b64d(blob["wrap"]["ciphertext"])), 32 + 16)
        # The public return value must not expose the DEK anywhere.
        self.assertEqual(set(params), {"protocol", "archive_id", "kdf", "wrap"})
        serialized = json.dumps(params)
        self.assertNotIn("dek", serialized.lower())
        self.assertNotIn("passphrase", serialized.lower())
        # The wrapped DEK really is unwrappable with the passphrase.
        dek = derive_dek({"archive_id": params["archive_id"], **params}, PASS)
        self.assertEqual(len(dek), 32)

    def test_refuses_overwrite_symlink_and_weak_passphrase(self):
        from encrypted_site import initialize_key

        key = self.init_key()
        before = key.read_bytes()
        with self.assertRaises(Exception):
            initialize_key(key, "another strong passphrase")
        self.assertEqual(key.read_bytes(), before)

        target = self.keys / "target.json"
        target.write_text("{}", encoding="utf-8")
        link = self.keys / "link.json"
        link.symlink_to(target)
        with self.assertRaises(Exception):
            initialize_key(link, PASS)
        self.assertEqual(target.read_text(encoding="utf-8"), "{}")

        dangling = self.keys / "dangling.json"
        dangling.symlink_to(self.keys / "missing.json")
        with self.assertRaises(Exception):
            initialize_key(dangling, PASS)

        for bad in ("", "short", "           "):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    initialize_key(self.keys / "weak.json", bad)

    def test_verify_key_never_returns_the_dek_and_detects_wrap_tamper(self):
        from encrypted_site import verify_key

        key = self.init_key()
        params = verify_key(key, PASS)
        self.assertEqual(set(params), {"protocol", "archive_id", "kdf", "wrap"})
        self.assertNotIn("dek", json.dumps(params).lower())
        with self.assertRaises(Exception):
            verify_key(key, WRONG)

        blob = json.loads(key.read_text(encoding="utf-8"))
        raw = bytearray(b64d(blob["wrap"]["ciphertext"]))
        raw[0] ^= 1
        blob["wrap"]["ciphertext"] = base64.b64encode(bytes(raw)).decode()
        key.write_text(json.dumps(blob), encoding="utf-8")
        with self.assertRaises(Exception):
            verify_key(key, PASS)


class TestPublishedSurface(EncryptedSiteBase):
    def test_whitelist_contains_only_programme_and_ciphertext(self):
        result = self.build()
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["changed"])
        self.assertEqual(result["entries"], 2)
        self.assertGreater(result["objects_written"], 0)
        self.assertEqual(result["objects_reused"], 0)

        files = result["files"]
        self.assertEqual(files, sorted(files))
        self.assertEqual(len(files), len(set(files)))
        self.assertTrue({"index.html", "site.json", ".nojekyll", "reader.js"} <= set(files))
        for rel in files:
            self.assertFalse(rel.startswith(("/", "\\")), rel)
            self.assertNotIn("..", Path(rel).parts)
            self.assertNotIn("\\", rel)
            self.assertTrue((self.output / rel).is_file(), rel)
            if rel not in {"index.html", "site.json", ".nojekyll", "reader.js"}:
                self.assertRegex(rel, OBJECT_RE)
        self.assertEqual(
            {rel for rel in files if rel.startswith("objects/")},
            {rel for rel in files if "/" in rel and rel.startswith("objects/")},
        )
        for forbidden in (
            "library/", "index.csv", "index.md", "entry.md", "raw.html.gz",
            ".state/", "taxonomy.yaml", "scripts/", "keys/",
        ):
            self.assertFalse(any(forbidden in rel for rel in files), forbidden)

    def test_no_plaintext_metadata_body_url_or_image_bytes_in_published_files(self):
        self.build()
        needles = [
            "加密标题甲".encode(),
            "摘要甲：这是第一篇文章的摘要。".encode(),
            "ALPHANEEDLE".encode(),
            "BETANEEDLE".encode(),
            "SECRETURL".encode(),
            "ALPHATAG".encode(),
            b"ALPHAIMAGE",
            b"library/coding",
        ]
        for rel in self.build()["files"]:
            blob = (self.output / rel).read_bytes()
            for needle in needles:
                self.assertNotIn(needle, blob, f"plaintext {needle!r} leaked into {rel}")

    def test_offline_build_html_still_has_no_fetch_or_external_reader(self):
        from build_html import build_html

        labels = {"coding": "编程与开发"}
        html = build_html(self.root, self.rows, labels)
        self.assertIn('id="archive-data"', html)
        self.assertNotIn("fetch(", html)
        self.assertNotIn("reader.js", html)
        self.assertIn("connect-src 'none'", html)

    def test_encrypted_entry_html_has_strict_csp_and_no_article_data(self):
        self.build()
        html = (self.output / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-ca-encrypted="1"', html)
        self.assertIn('<script src="reader.js" defer></script>', html)
        self.assertIn("connect-src 'self'", html)
        self.assertIn("img-src 'self' blob: data:;", html)
        self.assertNotIn("unsafe-inline; script-src", html)
        csp = re.search(r'Content-Security-Policy" content="([^"]+)"', html).group(1)
        script_src = re.search(r"script-src ([^;]+);", csp).group(1)
        self.assertNotIn("unsafe-inline", script_src)
        self.assertNotIn("unsafe-eval", script_src)
        self.assertIn("'self'", script_src)
        self.assertRegex(script_src, r"'sha256-[A-Za-z0-9+/=]+'")
        # The inline core must still be byte-identical to the hash whitelisted.
        scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
        self.assertTrue(scripts)
        digest = base64.b64encode(hashlib.sha256(scripts[0].encode("utf-8")).digest()).decode()
        self.assertIn(f"'sha256-{digest}'", script_src)
        self.assertNotIn("加密标题甲", html)
        self.assertNotIn("ALPHANEEDLE", html)
        self.assertNotIn("SECRETURL", html)
        self.assertIn("<script id=\"archive-data\" type=\"application/json\">\n[]\n", html)
        self.assertIn("<script id=\"labels-data\" type=\"application/json\">\n{}\n", html)

    def test_encrypted_entry_html_keeps_trusted_inline_favicon(self):
        import urllib.parse
        import xml.etree.ElementTree as ET

        self.build()
        html = (self.output / "index.html").read_text(encoding="utf-8")
        match = re.search(
            r'<link rel="icon" type="image/svg\+xml" href="([^"]+)"', html
        )
        self.assertIsNotNone(match, "encrypted page is missing its favicon link")
        href = match.group(1)
        self.assertTrue(href.startswith("data:image/svg+xml,"))
        svg = urllib.parse.unquote(href.split(",", 1)[1])
        root = ET.fromstring(svg)
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
        self.assertIn("img-src 'self' blob: data:;", html)
        script_src = re.search(r"script-src ([^;]+);", html).group(1)
        self.assertNotIn("data:", script_src)
        self.assertNotIn("unsafe-eval", script_src)

    def test_article_data_uri_is_not_rewritten_into_reader_image(self):
        from encrypted_site import _rewrite_body_images

        body = '<p><img src="data:image/png;base64,AAAA" alt="x"></p>'

        class NoStore:
            def put(self, *args, **kwargs):
                raise AssertionError("article data: URI must not be stored")

        result = _rewrite_body_images(
            body,
            root=self.root,
            entry_dir=self.root,
            article_id="2026-01-01-alpha",
            store=NoStore(),
        )
        self.assertEqual(result, body)
        self.assertNotIn("data-ca-src", result)

    def test_site_json_is_minimal_and_strict(self):
        self.build()
        site = self.site()
        self.assertEqual(
            set(site),
            {"format", "protocol", "archive_id", "kdf", "wrap", "index", "version"},
        )
        self.assertEqual(site["protocol"], PROTOCOL)
        self.assertEqual(site["kdf"]["iterations"], ITERATIONS)
        self.assertEqual(site["kdf"]["key_length"], 32)
        self.assertEqual(site["kdf"]["hash"], "sha256")
        self.assertEqual(len(b64d(site["kdf"]["salt"])), 16)
        self.assertEqual(len(b64d(site["wrap"]["nonce"])), 12)
        self.assertRegex(site["archive_id"], r"^[0-9a-f]{32}$")
        self.assertRegex(site["version"], r"^[0-9a-f]{32}$")
        self.assertRegex(site["index"]["file"], OBJECT_RE)
        self.assertNotIn("entries", site)
        serialized = (self.output / "site.json").read_text(encoding="utf-8")
        self.assertNotIn(str(self.base), serialized)
        self.assertNotIn("root", serialized.lower())
        self.assertNotIn("passphrase", serialized.lower())
        self.assertNotIn("key_file", serialized)


class TestProtocol(EncryptedSiteBase):
    def test_python_can_decrypt_every_object_with_spec_aad(self):
        self.build()
        dek, site = self.dek()
        payloads = self.article_payloads()
        self.assertEqual(set(payloads), {"加密标题甲", "加密标题乙"})
        alpha = payloads["加密标题甲"]["html"]
        self.assertIn("ALPHANEEDLE", alpha)
        self.assertIn("data-ca-src=\"objects/", alpha)
        self.assertIn("data-ca-mime=\"image/png\"", alpha)
        self.assertNotIn("library/coding", alpha)
        # The index object authenticates as "index"; each body object as
        # "article" under the public oid referenced by the encrypted index.
        index_rel = site["index"]["file"]
        index = json.loads(
            decrypt_file(
                self.output / index_rel, dek, site["archive_id"], "index",
                object_oid_from_rel(index_rel),
            )
        )
        for article in index["articles"]:
            rel = article["body"]["file"]
            self.assertEqual(object_oid_from_rel(rel), article["id"])
            decrypt_file(self.output / rel, dek, site["archive_id"], "article", article["id"])
        image_rel = re.search(r'data-ca-src="(objects/[^"]+)"', alpha).group(1)
        decrypt_file(
            self.output / image_rel, dek, site["archive_id"], "image",
            object_oid_from_rel(image_rel),
        )

    def test_wrong_passphrase_tampered_wrap_and_tampered_objects_fail_closed(self):
        self.build()
        site = self.site()
        with self.assertRaises(Exception):
            derive_dek(site, WRONG)

        tampered = json.loads(json.dumps(site))
        raw = bytearray(b64d(tampered["wrap"]["ciphertext"]))
        raw[0] ^= 0x01
        tampered["wrap"]["ciphertext"] = base64.b64encode(bytes(raw)).decode()
        with self.assertRaises(Exception):
            derive_dek(tampered, PASS)

        dek = derive_dek(site, PASS)
        files = [rel for rel in self.build()["files"] if rel.startswith("objects/")]
        for rel in files:
            path = self.output / rel
            blob = bytearray(path.read_bytes())
            blob[-1] ^= 0x01
            path.write_bytes(bytes(blob))
            with self.subTest(rel=rel):
                with self.assertRaises(Exception):
                    decrypt_file(path, dek, site["archive_id"], "article", object_oid_from_rel(rel))
            path.write_bytes(bytes(blob[:-1]) + bytes([blob[-1] ^ 0x01]))

    def test_aad_binds_purpose_archive_and_object_id(self):
        self.build()
        dek, site = self.dek()
        rel = site["index"]["file"]
        oid = object_oid_from_rel(rel)
        path = self.output / rel
        self.assertTrue(decrypt_file(path, dek, site["archive_id"], "index", oid))
        for purpose in ("article", "image", "wrap"):
            with self.subTest(purpose=purpose):
                with self.assertRaises(Exception):
                    decrypt_file(path, dek, site["archive_id"], purpose, oid)
        other_archive = "0" * 32 if site["archive_id"] != "0" * 32 else "1" * 32
        with self.assertRaises(Exception):
            decrypt_file(path, dek, other_archive, "index", oid)
        with self.assertRaises(Exception):
            decrypt_file(path, dek, site["archive_id"], "index", "f" * 64)

    def test_nonce_header_and_truncation_tamper_fail_closed(self):
        self.build()
        dek, site = self.dek()
        rel = site["index"]["file"]
        oid = object_oid_from_rel(rel)
        path = self.output / rel
        good = path.read_bytes()
        variants = {
            "nonce": good[:4] + bytes([good[4] ^ 1]) + good[5:],
            "magic": b"XXXX" + good[4:],
            "truncated": good[:-1],
            "empty": b"",
        }
        for name, blob in variants.items():
            with self.subTest(name=name):
                path.write_bytes(blob)
                with self.assertRaises(Exception):
                    decrypt_file(path, dek, site["archive_id"], "index", oid)
        path.write_bytes(good)

    def test_objects_cannot_be_swapped_between_articles(self):
        self.build()
        dek, site = self.dek()
        files = [
            a["body"]["file"]
            for a in json.loads(
                decrypt_file(
                    self.output / site["index"]["file"], dek, site["archive_id"], "index",
                    object_oid_from_rel(site["index"]["file"]),
                )
            )["articles"]
        ]
        self.assertEqual(len(files), 2)
        first, second = (self.output / f for f in files)
        second_bytes = second.read_bytes()
        second.write_bytes(first.read_bytes())
        with self.assertRaises(Exception):
            decrypt_file(second, dek, site["archive_id"], "article", object_oid_from_rel(files[1]))
        second.write_bytes(second_bytes)

    def test_changed_content_uses_a_fresh_nonce(self):
        first = self.build()
        alpha = self.article_payloads()["加密标题甲"]["article"]["body"]["file"]
        old_nonce = (self.output / alpha).read_bytes()[4:16]
        entry = self.root / "library/coding/2026-01-01-alpha/entry.md"
        entry.write_text(
            entry.read_text(encoding="utf-8").replace("ALPHANEEDLE", "ALPHANEEDLE-2"),
            encoding="utf-8",
        )
        self.build()
        new_alpha = self.article_payloads()["加密标题甲"]["article"]["body"]["file"]
        self.assertNotEqual(new_alpha, alpha)
        new_nonce = (self.output / new_alpha).read_bytes()[4:16]
        self.assertNotEqual(new_nonce, old_nonce)
        self.assertTrue((self.output / alpha).exists(), "old ciphertext should stay available")

    def test_image_bytes_round_trip(self):
        self.build()
        dek, site = self.dek()
        payload = self.article_payloads()["加密标题甲"]["html"]
        rel = re.search(r'data-ca-src="(objects/[^"]+)"', payload).group(1)
        blob = decrypt_file(self.output / rel, dek, site["archive_id"], "image", object_oid_from_rel(rel))
        self.assertEqual(blob, b"\x89PNG\r\n\x1a\nALPHAIMAGE")


class TestIncremental(EncryptedSiteBase):
    def snapshot(self) -> dict[str, tuple[int, int]]:
        return {
            rel: ((self.output / rel).stat().st_mtime_ns, len((self.output / rel).read_bytes()))
            for rel in self.build()["files"]
        }

    def test_second_build_is_byte_and_mtime_noop(self):
        first = self.build()
        before = {rel: (self.output / rel).read_bytes() for rel in first["files"]}
        before_stat = {rel: (self.output / rel).stat().st_mtime_ns for rel in first["files"]}
        second = self.build()
        self.assertFalse(second["changed"])
        self.assertEqual(second["version"], first["version"])
        self.assertEqual(second["objects_written"], 0)
        self.assertGreater(second["objects_reused"], 0)
        self.assertEqual(second["files"], first["files"])
        for rel in first["files"]:
            self.assertEqual((self.output / rel).read_bytes(), before[rel], rel)
            self.assertEqual((self.output / rel).stat().st_mtime_ns, before_stat[rel], rel)

    def test_build_never_modifies_source_files(self):
        source_files = sorted(
            p for p in self.root.rglob("*") if p.is_file()
        )
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in source_files}
        self.build()
        self.build()
        after = {
            p.relative_to(self.root).as_posix(): p.read_bytes()
            for p in sorted(self.root.rglob("*"))
            if p.is_file()
        }
        self.assertEqual(after, before)

    def test_content_change_only_touches_related_objects(self):
        first = self.build()
        first_by_id = self.article_payloads()
        beta_files = {
            "body": first_by_id["加密标题乙"]["article"]["body"]["file"],
        }
        beta_body_before = (self.output / beta_files["body"]).read_bytes()

        # Rewrite alpha body only.
        entry = self.root / "library/coding/2026-01-01-alpha/entry.md"
        text = entry.read_text(encoding="utf-8").replace("ALPHANEEDLE", "ALPHANEEDLE-CHANGED")
        entry.write_text(text, encoding="utf-8")
        second = self.build()
        self.assertTrue(second["changed"])
        self.assertNotEqual(second["version"], first["version"])
        total_objects = len([rel for rel in first["files"] if rel.startswith("objects/")])
        self.assertEqual(second["objects_written"] + second["objects_reused"], total_objects)
        self.assertLessEqual(second["objects_written"], 3)
        after_by_id = self.article_payloads()
        self.assertIn("ALPHANEEDLE-CHANGED", after_by_id["加密标题甲"]["html"])
        self.assertEqual(after_by_id["加密标题乙"]["article"]["body"]["file"], beta_files["body"])
        self.assertEqual((self.output / beta_files["body"]).read_bytes(), beta_body_before)

    def test_reverting_content_reuses_preserved_old_ciphertext(self):
        first = self.build()
        entry = self.root / "library/coding/2026-01-01-alpha/entry.md"
        original_text = entry.read_text(encoding="utf-8")
        entry.write_text(original_text.replace("ALPHANEEDLE", "TEMP"), encoding="utf-8")
        self.build()
        entry.write_text(original_text, encoding="utf-8")
        third = self.build()
        # The old ciphertext objects were preserved, so the revert reuses them
        # instead of re-encrypting (random nonces would change bytes).
        self.assertEqual(third["objects_written"], 0)
        self.assertEqual(third["version"], first["version"])
        self.assertEqual(third["files"], first["files"])

    def test_corrupt_or_tampered_cache_is_rejected_not_trusted(self):
        first = self.build()
        article_rel = next(
            a["article"]["body"]["file"]
            for a in self.article_payloads().values()
            if a["article"]["title"] == "加密标题甲"
        )
        target = self.output / article_rel
        good = target.read_bytes()
        site_before = (self.output / "site.json").read_bytes()

        target.write_bytes(good[:-1] + bytes([good[-1] ^ 0xFF]))
        with self.assertRaises(Exception):
            self.build()
        self.assertEqual((self.output / "site.json").read_bytes(), site_before)

        target.write_bytes(b"CAE1" + b"\x00" * 40)
        with self.assertRaises(Exception):
            self.build()
        self.assertEqual((self.output / "site.json").read_bytes(), site_before)

        # A file at the expected name with plaintext content is not trusted.
        target.write_bytes(b"plaintext pretending to be ciphertext")
        with self.assertRaises(Exception):
            self.build()

    def test_unknown_files_never_enter_the_publish_whitelist(self):
        self.build()
        (self.output / "extra.txt").write_text("not part of the site", encoding="utf-8")
        (self.output / "objects" / "junk.txt").write_text("junk", encoding="utf-8")
        result = self.build()
        self.assertNotIn("extra.txt", result["files"])
        self.assertNotIn("objects/junk.txt", result["files"])
        for rel in result["files"]:
            self.assertTrue(rel == ".nojekyll" or rel in {"index.html", "site.json", "reader.js"} or OBJECT_RE.match(rel))

    def test_missing_object_is_safely_reencrypted(self):
        first = self.build()
        article_rel = next(
            a["article"]["body"]["file"]
            for a in self.article_payloads().values()
            if a["article"]["title"] == "加密标题甲"
        )
        (self.output / article_rel).unlink()
        second = self.build()
        self.assertTrue(second["changed"])
        self.assertGreaterEqual(second["objects_written"], 1)
        self.assertIn("ALPHANEEDLE", self.article_payloads()["加密标题甲"]["html"])

    def test_failed_build_keeps_previous_site_json(self):
        self.build()
        site_before = (self.output / "site.json").read_bytes()
        html_before = (self.output / "index.html").read_bytes()
        (self.root / "library/coding/2026-01-02-beta/entry.md").unlink()
        with self.assertRaises(Exception):
            self.build()
        self.assertEqual((self.output / "site.json").read_bytes(), site_before)
        self.assertEqual((self.output / "index.html").read_bytes(), html_before)


class TestPathsAndSecrets(EncryptedSiteBase):
    def test_key_inside_root_or_output_is_rejected(self):
        inside_root = self.root / "keys/site-key.json"
        initialize_key = self.init_key(inside_root)
        with self.assertRaises(Exception):
            self.build(key_path=initialize_key)

        outside = self.init_key()
        with self.assertRaises(Exception):
            self.build(key_path=outside, output=self.root / "out")

    def test_output_overlapping_source_or_parent_is_rejected(self):
        key = self.init_key()
        for output in (self.root, self.root / "out", self.base):
            with self.subTest(output=output):
                with self.assertRaises(Exception):
                    self.build(key_path=key, output=output)

    def test_output_and_key_symlinks_are_rejected(self):
        key = self.init_key()
        real_out = self.base / "real-out"
        real_out.mkdir()
        link_out = self.base / "link-out"
        link_out.symlink_to(real_out)
        with self.assertRaises(Exception):
            self.build(key_path=key, output=link_out)

        key_link = self.base / "key-link.json"
        key_link.symlink_to(key)
        with self.assertRaises(Exception):
            self.build(key_path=key_link, output=self.output)

    def test_symlinked_entry_and_image_are_rejected(self):
        key = self.init_key()
        # Entry itself is a symlink (path still ends in entry.md).
        entry = self.root / "library/coding/2026-01-01-alpha/entry.md"
        backup = entry.with_name("entry.md.real")
        entry.rename(backup)
        entry.symlink_to(self.root / "library/coding/2026-01-02-beta/entry.md")
        try:
            with self.assertRaises(Exception):
                self.build(key_path=key)
        finally:
            entry.unlink()
            backup.rename(entry)

        # Image symlink that resolves inside the entry dir is still refused.
        images = self.root / "library/coding/2026-01-01-alpha/images"
        (images / "alias.png").symlink_to(images / "cover.png")
        text = entry.read_text(encoding="utf-8").replace("images/cover.png", "images/alias.png")
        entry.write_text(text, encoding="utf-8")
        with self.assertRaises(Exception):
            self.build(key_path=key)

    def test_entry_path_traversal_is_rejected(self):
        key = self.init_key()
        self.rows[0]["path"] = "../outside/entry.md"
        self.write_csv()
        with self.assertRaises(Exception):
            self.build(key_path=key)
        self.rows[0]["path"] = "/etc/passwd"
        self.write_csv()
        with self.assertRaises(Exception):
            self.build(key_path=key)

    def test_key_file_permissions_and_shape_are_enforced(self):
        key = self.init_key()
        os.chmod(key, 0o644)
        with self.assertRaises(Exception):
            self.build(key_path=key)

        os.chmod(key, 0o600)
        blob = json.loads(key.read_text(encoding="utf-8"))
        blob["kdf"]["iterations"] = 1
        key.write_text(json.dumps(blob), encoding="utf-8")
        with self.assertRaises(Exception):
            self.build(key_path=key)

        blob["kdf"]["iterations"] = ITERATIONS
        del blob["dek"]
        key.write_text(json.dumps(blob), encoding="utf-8")
        with self.assertRaises(Exception):
            self.build(key_path=key)

    def test_out_of_bounds_image_reference_is_not_exported(self):
        key = self.init_key()
        entry = self.root / "library/coding/2026-01-01-alpha/entry.md"
        entry.write_text(
            entry.read_text(encoding="utf-8") + "\n\n![bad](../../../../index.csv)\n",
            encoding="utf-8",
        )
        result = self.build(key_path=key)
        payload = self.article_payloads()["加密标题甲"]["html"]
        self.assertNotIn("index.csv", payload)
        self.assertEqual(len([f for f in result["files"] if f.startswith("objects/")]), 4)

    def test_missing_index_csv_fails(self):
        key = self.init_key()
        (self.root / "index.csv").unlink()
        with self.assertRaises(Exception):
            self.build(key_path=key)


class TestWebCryptoInterop(EncryptedSiteBase):
    """The reader uses browser Web Crypto; Node 26 exposes the same API."""

    def test_node_webcrypto_unwraps_and_decrypts_everything(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        self.build()
        payload = self.article_payloads()["加密标题甲"]["html"]
        image_rel = re.search(r'data-ca-src="(objects/[^"]+)"', payload).group(1)
        probe = self.base / "probe.json"
        probe.write_text(
            json.dumps(
                {
                    "dir": str(self.output),
                    "passphrase": PASS,
                    "wrong": WRONG,
                    "needles": ["ALPHANEEDLE", "摘要甲：这是第一篇文章的摘要。"],
                    "imageRel": image_rel,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [node, str(ROOT / "tests" / "browser_encrypted_interop.mjs"), str(probe)],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        out = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["articles"], 2)
        self.assertGreaterEqual(out["images"], 1)
        self.assertTrue(out["wrongPassphraseRejected"])
        self.assertTrue(out["tamperedWrapRejected"])
        self.assertTrue(out["tamperedObjectRejected"])


class TestReaderStaticSafety(unittest.TestCase):
    def setUp(self):
        self.js = (SCRIPTS / "encrypted_reader.js").read_text(encoding="utf-8")

    def test_uses_webcrypto_and_never_persists_secrets(self):
        self.assertIn("crypto.subtle", self.js)
        self.assertIn("PBKDF2", self.js)
        self.assertIn("AES-GCM", self.js)
        self.assertIn("HKDF", self.js)
        for forbidden in ("localStorage", "sessionStorage", "indexedDB", "serviceWorker", "eval(", "new Function", "document.write"):
            self.assertNotIn(forbidden, self.js, forbidden)

    def test_cancels_async_work_and_revokes_blob_urls_on_exit(self):
        for needle in (
            "AbortController",
            "URL.revokeObjectURL",
            "pagehide",
            "pageshow",
            "history.replaceState",
            "epoch",
            "lock",
        ):
            self.assertIn(needle, self.js)
        self.assertNotRegex(self.js, r"https?://[^'\"]+", "reader must not reference remote origins")

    def test_offline_template_has_no_network_or_storage(self):
        template = (SCRIPTS / "archive_page.html").read_text(encoding="utf-8")
        self.assertNotIn("fetch(", template)
        self.assertNotIn("localStorage", template)
        self.assertNotIn("sessionStorage", template)


class TestBrowserAcceptance(EncryptedSiteBase):
    """Optional real-Chromium run (set CA_RUN_BROWSER_TESTS=1).

    The parent agent provides an isolated Playwright environment; the fixture
    here is synthetic and never touches production keys or the real library.
    """

    @unittest.skipUnless(os.environ.get("CA_RUN_BROWSER_TESTS") == "1", "set CA_RUN_BROWSER_TESTS=1")
    def test_real_chromium_unlock_search_exit_and_tamper(self):
        # No baked-in interpreter: point CA_PLAYWRIGHT_PYTHON at a Playwright env.
        python = os.environ.get("CA_PLAYWRIGHT_PYTHON") or sys.executable
        if not Path(python).is_file():
            self.skipTest("playwright python not available: set CA_PLAYWRIGHT_PYTHON")
        probe = subprocess.run(
            [python, "-c", "import playwright.sync_api"], capture_output=True, text=True
        )
        if probe.returncode != 0:
            self.skipTest(f"playwright is not installed for {python}: {probe.stderr.strip()}")
        self.build()
        script = str(ROOT / "tests" / "browser_encrypted_reader.py")

        def run(directory: Path, mode: str, tamper_rel: str = "") -> None:
            cmd = [python, script, "--dir", str(directory), "--passphrase", PASS, "--mode", mode]
            if tamper_rel:
                cmd += ["--tamper-rel", tamper_rel]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertTrue(json.loads(proc.stdout.strip().splitlines()[-1])["ok"], proc.stdout)

        run(self.output, "normal")

        payload = self.article_payloads()["加密标题甲"]["html"]
        article_rel = self.article_payloads()["加密标题甲"]["article"]["body"]["file"]
        image_rel = re.search(r'data-ca-src="(objects/[^"]+)"', payload).group(1)

        for name, mode, rel in (
            ("site-tamper-index", "tamper", ""),
            ("site-tamper-params", "tamper-site", ""),
            ("site-tamper-article", "tamper-article", article_rel),
            ("site-tamper-image", "tamper-image", image_rel),
        ):
            target = self.base / name
            shutil.copytree(self.output, target)
            run(target, mode, rel)


if __name__ == "__main__":
    unittest.main()
