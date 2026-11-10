#!/usr/bin/env python3
"""Encrypted static-site exporter for ContentArchive.

This module owns only the local, offline side of the encrypted GitHub Pages
reading room frozen in ``docs/plans/encrypted-pages.md``:

* ``initialize_key(key_path, passphrase)`` creates an owner-only key file that
  wraps a random 256-bit DEK with PBKDF2-HMAC-SHA256 + AES-256-GCM.
* ``build_site(root, output_dir, key_path)`` reads ``index.csv`` / ``entry.md``
  / safe local images and writes a publishable directory containing only the
  reader programme, ``site.json`` and authenticated ciphertext objects.

It never talks to git, the network, credentials or the real archive state.

Protocol (CA-ENC-1)
-------------------
* DEK: 32 random bytes, stored only in the owner-only key file outside the
  project.  The same file carries the PBKDF2/AES-GCM wrapped copy that is
  published in ``site.json`` for browsers (build_site receives no passphrase).
* Wrap: PBKDF2-HMAC-SHA256(salt=16B, 600000) -> AES-256-GCM(nonce=12B,
  tag=128b); AAD = LP(protocol) | LP(archive_id) | LP("wrap") | LP("dek").
* Objects: AES-256-GCM with a fresh random nonce per encryption; AAD =
  LP(protocol) | LP(archive_id) | LP(purpose) | LP(public oid).  Purposes are
  ``index``, ``article`` and ``image``.
* Names: ``objects/<oid>.<rev>.bin`` where oid/rev are keyed HMAC-SHA256 values
  over the logical id and plaintext digest, so titles/paths/plaintext hashes
  are never published.
* Reuse: an existing object is reused only after decrypting it and comparing
  the plaintext byte-for-byte; a mismatch or authentication failure refuses
  the build instead of trusting the filename.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac as hmaclib
import html as htmlmod
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import urllib.parse
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from build_html import ALLOWED_IMAGE_EXTS, _is_safe_svg_file, build_html  # noqa: E402

PROTOCOL = "CA-ENC-1"
KEY_FORMAT = "contentarchive-encrypted-site-key"
SITE_FORMAT = "contentarchive-encrypted-site"
INDEX_FORMAT = "contentarchive-encrypted-index"

PBKDF2_ITERATIONS = 600_000
KDF_KEY_LENGTH = 32
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32
TAG_BITS = 128

OBJECT_MAGIC = b"CAE1"
CIPHER_OVERHEAD = len(OBJECT_MAGIC) + NONCE_BYTES + TAG_BITS // 8

MIN_PASSPHRASE_CHARS = 12

MAX_KEY_FILE_BYTES = 64 * 1024
MAX_INDEX_CSV_BYTES = 32 * 1024 * 1024
MAX_TAXONOMY_BYTES = 1024 * 1024
MAX_ENTRIES = 20_000
MAX_PLAINTEXT_BYTES = 64 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_READER_JS_BYTES = 2 * 1024 * 1024

ARCHIVE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
OID_RE = re.compile(r"^[0-9a-f]{64}$")
REV_RE = re.compile(r"^[0-9a-f]{32}$")
VERSION_RE = re.compile(r"^[0-9a-f]{32}$")
OBJECT_REL_RE = re.compile(r"^objects/([0-9a-f]{64})\.([0-9a-f]{32})\.bin$")

MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
}

PUBLIC_STATIC = ("index.html", "site.json", ".nojekyll", "reader.js")

_TAG_RE = re.compile(r"<(img|a)\b([^>]*)>", re.I)
_ATTR_RE = re.compile(r'\b(src|href)="([^"]*)"', re.I)
_ARCHIVE_DATA_RE = re.compile(
    r'<script id="archive-data" type="application/json">\s*(.*?)\s*</script>', re.S
)
_APP_SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)
_CSP_META_RE = re.compile(
    r'(<meta http-equiv="Content-Security-Policy" content=")[^"]*(")', re.I
)
_PLACEHOLDER_RE = re.compile(r"\{\{(?:ARCHIVE_DATA|LABELS_DATA|BUILD_TIME)\}\}")


class EncryptedSiteError(RuntimeError):
    """Base class for fail-closed exporter errors."""


class KeyFileError(EncryptedSiteError):
    """Key file is missing, malformed, or not owner-only."""


class BuildError(EncryptedSiteError):
    """Input or output path violates the frozen build contract."""


class CacheIntegrityError(EncryptedSiteError):
    """An existing ciphertext object cannot be authenticated as reused data."""


# ---------------------------------------------------------------------------
# Protocol primitives
# ---------------------------------------------------------------------------

def _lp(value: bytes) -> bytes:
    return len(value).to_bytes(4, "big") + value


def _aad(archive_id: str, purpose: str, oid: str) -> bytes:
    """Length-prefixed AAD: protocol, archive_id, purpose, object id.

    Purpose is one of ``wrap``, ``index``, ``article`` or ``image``.  The
    object id is the public HMAC address, never a title/path/plaintext hash.
    """
    return b"".join(
        _lp(part)
        for part in (
            PROTOCOL.encode("ascii"),
            archive_id.encode("ascii"),
            purpose.encode("ascii"),
            oid.encode("ascii"),
        )
    )


def _b64e(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64d(value: Any, *, limit: int = MAX_KEY_FILE_BYTES) -> bytes:
    if not isinstance(value, str) or len(value) > limit * 2:
        raise KeyFileError("invalid base64 field")
    try:
        return base64.b64decode(value, validate=True)
    except Exception as exc:  # noqa: BLE001 - normalise to one fail-closed error
        raise KeyFileError("invalid base64 field") from exc


def _derive_kek(passphrase: str, salt: bytes, iterations: int) -> bytes:
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KDF_KEY_LENGTH,
        salt=salt,
        iterations=iterations,
    ).derive(passphrase.encode("utf-8"))


def _derive_address_key(dek: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"",
        info=b"CA-ENC-1/address",
    ).derive(dek)


def _validate_passphrase(passphrase: Any) -> str:
    if not isinstance(passphrase, str):
        raise ValueError("passphrase must be a string")
    if len(passphrase) < MIN_PASSPHRASE_CHARS or not passphrase.strip():
        raise ValueError(f"passphrase must be at least {MIN_PASSPHRASE_CHARS} characters")
    return passphrase


def _public_params(blob: dict) -> dict:
    return {
        "protocol": blob["protocol"],
        "archive_id": blob["archive_id"],
        "kdf": dict(blob["kdf"]),
        "wrap": dict(blob["wrap"]),
    }


# ---------------------------------------------------------------------------
# Key file
# ---------------------------------------------------------------------------

def initialize_key(key_path: Path, passphrase: str) -> dict:
    """Create a new owner-only key file, refusing to overwrite anything.

    Returns only the public wrapping parameters (no DEK, no passphrase).
    """
    _validate_passphrase(passphrase)
    path = Path(key_path)
    link = _symlink_component(path)
    if link is not None:
        raise KeyFileError(f"key path must not traverse a symbolic link: {link}")

    parent = path.parent
    if parent.is_symlink():
        raise KeyFileError("key directory must not be a symbolic link")
    if parent.exists():
        if not parent.is_dir():
            raise KeyFileError("key directory is not a directory")
    else:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(parent, 0o700)
        except OSError as exc:
            raise KeyFileError(f"cannot restrict key directory permissions: {exc}") from exc

    if path.exists():
        raise KeyFileError("key file already exists")

    archive_id = secrets.token_hex(16)
    dek = secrets.token_bytes(KEY_BYTES)
    salt = secrets.token_bytes(SALT_BYTES)
    nonce = secrets.token_bytes(NONCE_BYTES)
    kek = _derive_kek(passphrase, salt, PBKDF2_ITERATIONS)
    wrapped = AESGCM(kek).encrypt(nonce, dek, _aad(archive_id, "wrap", "dek"))

    blob = {
        "format": KEY_FORMAT,
        "protocol": PROTOCOL,
        "archive_id": archive_id,
        # The DEK lives only in this owner-only file outside the project.  The
        # wrapped copy below is what gets published in site.json for browsers.
        "dek": _b64e(dek),
        "kdf": {
            "name": "PBKDF2-HMAC-SHA256",
            "hash": "sha256",
            "iterations": PBKDF2_ITERATIONS,
            "salt": _b64e(salt),
            "key_length": KDF_KEY_LENGTH,
        },
        "wrap": {
            "cipher": "AES-256-GCM",
            "nonce": _b64e(nonce),
            "ciphertext": _b64e(wrapped),
            "tag_length": TAG_BITS,
        },
    }
    raw = json.dumps(blob, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise KeyFileError("key file already exists") from exc
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise

    return _public_params(blob)


def _strict_key_blob(key_path: Path) -> dict:
    path = Path(key_path)
    if path.is_symlink():
        raise KeyFileError("key path must not be a symbolic link")
    try:
        st = path.stat()
    except OSError as exc:
        raise KeyFileError(f"key file is not readable: {exc}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise KeyFileError("key path is not a regular file")
    if st.st_size > MAX_KEY_FILE_BYTES:
        raise KeyFileError("key file is too large")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise KeyFileError("key file must be owner-only (0600)")
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise KeyFileError(f"key file is not valid JSON: {exc}") from exc
    if not isinstance(blob, dict):
        raise KeyFileError("key file root must be an object")
    if blob.get("format") != KEY_FORMAT or blob.get("protocol") != PROTOCOL:
        raise KeyFileError("unsupported key file format")
    archive_id = blob.get("archive_id")
    if not isinstance(archive_id, str) or not ARCHIVE_ID_RE.match(archive_id):
        raise KeyFileError("invalid archive id")

    kdf = blob.get("kdf")
    wrap = blob.get("wrap")
    if not isinstance(kdf, dict) or not isinstance(wrap, dict):
        raise KeyFileError("invalid key parameters")
    if (
        kdf.get("name") != "PBKDF2-HMAC-SHA256"
        or kdf.get("hash") != "sha256"
        or kdf.get("iterations") != PBKDF2_ITERATIONS
        or kdf.get("key_length") != KDF_KEY_LENGTH
    ):
        raise KeyFileError("unsupported KDF parameters")
    salt = _b64d(kdf.get("salt"))
    if len(salt) != SALT_BYTES:
        raise KeyFileError("invalid KDF salt")
    if wrap.get("cipher") != "AES-256-GCM" or wrap.get("tag_length") != TAG_BITS:
        raise KeyFileError("unsupported wrapping parameters")
    nonce = _b64d(wrap.get("nonce"))
    ciphertext = _b64d(wrap.get("ciphertext"))
    if len(nonce) != NONCE_BYTES or len(ciphertext) != KEY_BYTES + TAG_BITS // 8:
        raise KeyFileError("invalid wrapped key length")
    dek = _b64d(blob.get("dek"))
    if len(dek) != KEY_BYTES:
        raise KeyFileError("invalid DEK length")
    return blob


def _load_key(key_path: Path) -> tuple[bytes, str, dict]:
    blob = _strict_key_blob(key_path)
    return _b64d(blob["dek"]), blob["archive_id"], _public_params(blob)


def _unwrap_key(key_path: Path, passphrase: str) -> tuple[bytes, str, dict]:
    """Read the key file and unwrap the DEK.

    ``build_site`` has no passphrase by design; it uses the key file as the
    local secret.  This helper exists so tests/the publisher can validate a key
    file without duplicating parsing.
    """
    blob = _strict_key_blob(key_path)
    archive_id = blob["archive_id"]
    kdf, wrap = blob["kdf"], blob["wrap"]
    salt = _b64d(kdf.get("salt"))
    nonce = _b64d(wrap.get("nonce"))
    ciphertext = _b64d(wrap.get("ciphertext"))
    kek = _derive_kek(_validate_passphrase(passphrase), salt, PBKDF2_ITERATIONS)
    try:
        dek = AESGCM(kek).decrypt(nonce, ciphertext, _aad(archive_id, "wrap", "dek"))
    except InvalidTag as exc:
        raise KeyFileError("wrong passphrase or corrupt wrapped key") from exc
    if len(dek) != KEY_BYTES:
        raise KeyFileError("invalid DEK length")
    if not hmaclib.compare_digest(dek, _b64d(blob["dek"])):
        raise KeyFileError("wrapped key does not match the local build key")
    return dek, archive_id, _public_params(blob)



# ---------------------------------------------------------------------------
# Paths and small files
# ---------------------------------------------------------------------------

def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _real(path: Path) -> Path:
    return Path(os.path.realpath(str(path)))


def _symlink_component(path: Path) -> Path | None:
    """Return the first existing symlink component of an absolute path."""
    current = Path(os.path.abspath(str(path)))
    while True:
        if current.is_symlink():
            return current
        if current.parent == current:
            return None
        current = current.parent


def _prepare_paths(root: Path, output_dir: Path, key_path: Path) -> tuple[Path, Path, Path]:
    root_in = Path(root)
    if not root_in.exists() or not root_in.is_dir():
        raise BuildError("root must be an existing directory")
    root_real = root_in.resolve()

    out_in = Path(output_dir)
    out_link = _symlink_component(out_in)
    if out_link is not None:
        raise BuildError(f"output path must not traverse a symbolic link: {out_link}")
    out_real = _real(out_in)
    if out_real.exists() and not out_real.is_dir():
        raise BuildError("output path is not a directory")

    key_in = Path(key_path)
    key_link = _symlink_component(key_in)
    if key_link is not None:
        raise KeyFileError(f"key path must not traverse a symbolic link: {key_link}")
    key_real = _real(key_in)

    if out_real == root_real or _is_within(out_real, root_real) or _is_within(root_real, out_real):
        raise BuildError("output directory must not overlap the source archive")
    if (
        key_real == root_real
        or _is_within(key_real, root_real)
        or _is_within(key_real, out_real)
        or key_real == out_real
    ):
        raise BuildError("key file must live outside the source and output directories")
    return root_real, out_real, key_in


def _assert_regular_source(path: Path, *, max_bytes: int, label: str) -> None:
    if path.is_symlink():
        raise BuildError(f"{label} must not be a symbolic link")
    try:
        st = path.stat()
    except OSError as exc:
        raise BuildError(f"{label} is not readable: {exc}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise BuildError(f"{label} is not a regular file")
    if st.st_size > max_bytes:
        raise BuildError(f"{label} is too large")


def _atomic_write_bytes(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _write_if_changed(path: Path, data: bytes) -> bool:
    if path.is_symlink():
        raise BuildError(f"refusing to write through symbolic link: {path.name}")
    if path.exists():
        if not path.is_file():
            raise BuildError(f"refusing to replace non-file output: {path.name}")
        if path.read_bytes() == data:
            return False
    _atomic_write_bytes(path, data)
    return True


def _labels_for(root: Path) -> dict[str, str]:
    path = root / "taxonomy.yaml"
    if not path.exists():
        return {}
    _assert_regular_source(path, max_bytes=MAX_TAXONOMY_BYTES, label="taxonomy.yaml")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        raise BuildError(f"invalid taxonomy.yaml: {exc}") from exc
    labels: dict[str, str] = {}
    for category in data.get("categories") or []:
        if isinstance(category, dict) and isinstance(category.get("slug"), str):
            label = category.get("label")
            labels[category["slug"]] = label if isinstance(label, str) else category["slug"]
    return labels


def _read_rows(root: Path) -> list[dict]:
    path = root / "index.csv"
    _assert_regular_source(path, max_bytes=MAX_INDEX_CSV_BYTES, label="index.csv")
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    if len(rows) > MAX_ENTRIES:
        raise BuildError("too many entries")
    seen: set[str] = set()
    for row in rows:
        entry_id = str(row.get("id") or "")
        rel = str(row.get("path") or "")
        if not entry_id or len(entry_id) > 256:
            raise BuildError("entry is missing a valid id")
        if entry_id in seen:
            raise BuildError(f"duplicate entry id: {entry_id}")
        seen.add(entry_id)
        if not rel or len(rel) > 1024:
            raise BuildError(f"entry {entry_id!r} is missing a path")
        _validate_entry_path(root, entry_id, rel)
    return rows


def _validate_entry_path(root: Path, entry_id: str, rel: str) -> Path:
    if "\\" in rel or "\x00" in rel:
        raise BuildError(f"invalid entry path for {entry_id!r}")
    pure = PurePosixPath(rel)
    if pure.is_absolute() or any(part in ("..", "") for part in pure.parts):
        raise BuildError(f"entry path escapes the archive: {rel!r}")
    if len(pure.parts) < 3 or pure.parts[0] != "library" or pure.name != "entry.md":
        raise BuildError(f"entry path must be library/<category>/<entry>/entry.md: {rel!r}")
    target = root / pure
    if os.path.realpath(str(target)) != str(target):
        raise BuildError(f"entry path must not traverse symbolic links: {rel!r}")
    library = (root / "library").resolve()
    if not _is_within(target.resolve(), library):
        raise BuildError(f"entry path escapes library: {rel!r}")
    if not target.is_file():
        raise BuildError(f"entry file does not exist: {rel!r}")
    return target


def _reject_entry_symlinks(entry_dir: Path) -> None:
    """Refuse any symlink inside an exported entry directory.

    ``build_html`` resolves image links before rendering, which would hide an
    alias from the exporter.  Walking the (small) entry directory keeps the
    frozen "no symlinked export resources" rule observable.
    """
    for dirpath, dirnames, filenames in os.walk(entry_dir, followlinks=False):
        for name in dirnames + filenames:
            candidate = Path(dirpath) / name
            if candidate.is_symlink():
                raise BuildError(f"entry directory contains a symbolic link: {candidate.name}")


def _resolve_image(root: Path, entry_dir: Path, raw_path: str) -> tuple[Path, str]:
    if "\\" in raw_path or "\x00" in raw_path:
        raise BuildError("invalid image path")
    try:
        decoded = urllib.parse.unquote(raw_path)
    except Exception as exc:  # noqa: BLE001
        raise BuildError("invalid image path encoding") from exc
    if not decoded.startswith("library/"):
        raise BuildError(f"image is outside library: {raw_path!r}")
    pure = PurePosixPath(decoded)
    if pure.is_absolute() or any(part in ("..", "") for part in pure.parts):
        raise BuildError(f"image path escapes library: {raw_path!r}")
    target = root / pure
    if os.path.realpath(str(target)) != str(target):
        raise BuildError(f"image path must not traverse symbolic links: {raw_path!r}")
    if not target.is_file():
        raise BuildError(f"image does not exist: {raw_path!r}")
    resolved = target.resolve()
    if not _is_within(resolved, entry_dir.resolve()):
        raise BuildError(f"image is outside its entry directory: {raw_path!r}")
    if not _is_within(resolved, (root / "library").resolve()):
        raise BuildError(f"image is outside library: {raw_path!r}")
    ext = target.suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTS or ext not in MIME_BY_EXT:
        raise BuildError(f"unsupported image type: {ext}")
    size = target.stat().st_size
    if size <= 0 or size > MAX_IMAGE_BYTES:
        raise BuildError("image size is outside the allowed range")
    if ext == ".svg" and not _is_safe_svg_file(target):
        raise BuildError("unsafe SVG image")
    return target, MIME_BY_EXT[ext]


# ---------------------------------------------------------------------------
# Ciphertext object store
# ---------------------------------------------------------------------------

class _ObjectStore:
    def __init__(self, output_dir: Path, dek: bytes, archive_id: str, addr_key: bytes):
        self.output_dir = output_dir
        self.dek = dek
        self.archive_id = archive_id
        self.addr_key = addr_key
        self.objects_dir = output_dir / "objects"
        if self.objects_dir.exists():
            if self.objects_dir.is_symlink() or not self.objects_dir.is_dir():
                raise BuildError("objects path must be a real directory")
        else:
            self.objects_dir.mkdir(mode=0o755)
        self.files: list[str] = []
        self.written = 0
        self.reused = 0
        self._cache: dict[tuple[str, str], dict] = {}
        self._nonces: set[bytes] = set()

    # -- addressing -------------------------------------------------------
    def oid(self, purpose: str, logical_id: str) -> str:
        msg = b"CA-ENC-1/oid\x00" + purpose.encode("ascii") + b"\x00" + logical_id.encode("utf-8")
        return hmaclib.new(self.addr_key, msg, hashlib.sha256).hexdigest()

    def rev(self, purpose: str, oid: str, plaintext: bytes) -> str:
        digest = hashlib.sha256(plaintext).digest()
        msg = (
            b"CA-ENC-1/rev\x00"
            + purpose.encode("ascii")
            + b"\x00"
            + oid.encode("ascii")
            + b"\x00"
            + digest
        )
        return hmaclib.new(self.addr_key, msg, hashlib.sha256).hexdigest()[:32]

    # -- crypto -----------------------------------------------------------
    def _encrypt(self, purpose: str, oid: str, plaintext: bytes) -> bytes:
        for _ in range(8):
            nonce = secrets.token_bytes(NONCE_BYTES)
            if nonce not in self._nonces:
                break
        else:  # pragma: no cover - cryptographically impossible
            raise BuildError("could not generate a unique nonce")
        self._nonces.add(nonce)
        ciphertext = AESGCM(self.dek).encrypt(nonce, plaintext, _aad(self.archive_id, purpose, oid))
        return OBJECT_MAGIC + nonce + ciphertext

    def _decrypt(self, blob: bytes, purpose: str, oid: str) -> bytes:
        if len(blob) < CIPHER_OVERHEAD or blob[:4] != OBJECT_MAGIC:
            raise CacheIntegrityError("object header is invalid")
        nonce, ciphertext = blob[4:16], blob[16:]
        try:
            return AESGCM(self.dek).decrypt(nonce, ciphertext, _aad(self.archive_id, purpose, oid))
        except InvalidTag as exc:
            raise CacheIntegrityError("object authentication failed") from exc

    # -- public API -------------------------------------------------------
    def put(self, purpose: str, logical_id: str, plaintext: bytes) -> dict:
        if not isinstance(plaintext, (bytes, bytearray)):
            raise BuildError("object plaintext must be bytes")
        plaintext = bytes(plaintext)
        if len(plaintext) > MAX_PLAINTEXT_BYTES:
            raise BuildError("object plaintext is too large")
        cache_key = (purpose, logical_id)
        if cache_key in self._cache:
            return self._cache[cache_key]

        oid = self.oid(purpose, logical_id)
        rev = self.rev(purpose, oid, plaintext)
        rel = f"objects/{oid}.{rev}.bin"
        path = self.output_dir / rel
        if path.is_symlink():
            raise CacheIntegrityError(f"object must not be a symbolic link: {rel}")
        if path.exists():
            if not path.is_file():
                raise CacheIntegrityError(f"object is not a regular file: {rel}")
            if path.stat().st_size > MAX_PLAINTEXT_BYTES + CIPHER_OVERHEAD:
                raise CacheIntegrityError(f"object is too large: {rel}")
            previous = path.read_bytes()
            if self._decrypt(previous, purpose, oid) != plaintext:
                raise CacheIntegrityError(
                    f"object at a content-addressed name holds different plaintext: {rel}"
                )
            data = previous
            self.reused += 1
        else:
            data = self._encrypt(purpose, oid, plaintext)
            _atomic_write_bytes(path, data, mode=0o644)
            self.written += 1
        info = {"file": rel, "oid": oid, "rev": rev, "bytes": len(plaintext)}
        self._cache[cache_key] = info
        self.files.append(rel)
        return info


# ---------------------------------------------------------------------------
# HTML body rewriting
# ---------------------------------------------------------------------------

def _rewrite_body_images(
    body_html: str,
    *,
    root: Path,
    entry_dir: Path,
    article_id: str,
    store: _ObjectStore,
) -> str:
    def repl(match: re.Match) -> str:
        tag = match.group(1).lower()
        attrs = match.group(2)
        attr_match = _ATTR_RE.search(attrs)
        if not attr_match:
            return match.group(0)
        attr = attr_match.group(1).lower()
        value = htmlmod.unescape(attr_match.group(2))
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme:
            return match.group(0)
        if not parsed.path.startswith("library/"):
            return match.group(0)

        target, mime = _resolve_image(root, entry_dir, parsed.path)
        payload = target.read_bytes()
        logical = f"{article_id}\x00{urllib.parse.unquote(parsed.path)}"
        info = store.put("image", logical, payload)
        marker = "data-ca-src" if tag == "img" else "data-ca-href"
        replacement = f'{marker}="{info["file"]}" data-ca-mime="{mime}"'
        new_attrs = attrs.replace(attr_match.group(0), replacement)
        if tag == "a":
            new_attrs += ' href="#"'
        return f"<{match.group(1)}{new_attrs}>"

    return _TAG_RE.sub(repl, body_html)


# ---------------------------------------------------------------------------
# Encrypted entry page
# ---------------------------------------------------------------------------

def _encrypted_csp(script_hash: str) -> str:
    return (
        "default-src 'none'; "
        f"script-src 'self' 'sha256-{script_hash}'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' blob: data:; "
        "connect-src 'self'; "
        "frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none';"
    )


def _build_entry_html(template_text: str, build_label: str) -> str:
    replacements = {
        "{{ARCHIVE_DATA}}": "[]",
        "{{LABELS_DATA}}": "{}",
        "{{BUILD_TIME}}": htmlmod.escape(build_label or ""),
    }
    text = _PLACEHOLDER_RE.sub(lambda m: replacements[m.group(0)], template_text)

    app_match = _APP_SCRIPT_RE.search(text)
    if not app_match:
        raise BuildError("archive_page.html is missing the inline reader core")
    script_hash = base64.b64encode(
        hashlib.sha256(app_match.group(1).encode("utf-8")).digest()
    ).decode("ascii")

    csp = _encrypted_csp(script_hash)
    text, count = _CSP_META_RE.subn(lambda m: m.group(1) + csp + m.group(2), text, count=1)
    if count != 1:
        raise BuildError("archive_page.html is missing its CSP meta tag")

    if '<html lang="zh-CN">' not in text:
        raise BuildError("archive_page.html has an unexpected html element")
    text = text.replace('<html lang="zh-CN">', '<html lang="zh-CN" data-ca-encrypted="1">', 1)
    text = text.replace(
        '<meta name="viewport"',
        '<meta name="referrer" content="no-referrer">\n  <meta name="viewport"',
        1,
    )
    if "</body>" not in text:
        raise BuildError("archive_page.html is missing </body>")
    text = text.replace("</body>", '  <script src="reader.js" defer></script>\n</body>', 1)
    return text


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _version(
    addr_key: bytes,
    archive_id: str,
    object_files: list[str],
    reader_bytes: bytes,
    index_html_bytes: bytes,
) -> str:
    mac = hmaclib.new(
        addr_key,
        b"CA-ENC-1/version\x00" + archive_id.encode("ascii"),
        hashlib.sha256,
    )
    for rel in sorted(object_files):
        mac.update(b"\x00" + rel.encode("ascii"))
    mac.update(b"\x00" + hashlib.sha256(reader_bytes).digest())
    mac.update(b"\x00" + hashlib.sha256(index_html_bytes).digest())
    return mac.hexdigest()[:32]


def build_site(root: Path, output_dir: Path, key_path: Path) -> dict:
    """Export the encrypted reading site without touching source files."""
    root_real, out_real, key_in = _prepare_paths(root, output_dir, key_path)
    dek, archive_id, params = _load_key(key_in)
    addr_key = _derive_address_key(dek)

    rows = _read_rows(root_real)
    labels = _labels_for(root_real)
    entry_dirs = {
        str(row.get("id") or ""): (root_real / str(row["path"])).parent for row in rows
    }
    for entry_dir in {path.resolve() for path in entry_dirs.values()}:
        _reject_entry_symlinks(entry_dir)
    try:
        offline_html = build_html(root_real, rows, labels)
    except Exception as exc:  # noqa: BLE001 - normalise to fail-closed build error
        raise BuildError(f"cannot render archive entries: {exc}") from exc

    data_match = _ARCHIVE_DATA_RE.search(offline_html)
    if not data_match:
        raise BuildError("build_html output has no archive data")
    try:
        articles = json.loads(data_match.group(1))
    except Exception as exc:  # noqa: BLE001
        raise BuildError(f"invalid archive data: {exc}") from exc
    if not isinstance(articles, list) or len(articles) > MAX_ENTRIES:
        raise BuildError("invalid article list")

    if out_real.exists():
        if out_real.is_symlink() or not out_real.is_dir():
            raise BuildError("output directory must be a real directory")
    else:
        out_real.mkdir(parents=True, exist_ok=True)

    store = _ObjectStore(out_real, dek, archive_id, addr_key)
    encrypted_articles: list[dict] = []
    for article in articles:
        if not isinstance(article, dict):
            raise BuildError("invalid article record")
        article_id = str(article.get("id") or "")
        if article_id not in entry_dirs:
            raise BuildError(f"article id has no entry path: {article_id!r}")
        body_html = article.pop("body_html", "")
        if not isinstance(body_html, str):
            raise BuildError("invalid article body")
        rewritten = _rewrite_body_images(
            body_html,
            root=root_real,
            entry_dir=entry_dirs[article_id],
            article_id=article_id,
            store=store,
        )
        body_info = store.put("article", article_id, rewritten.encode("utf-8"))
        # Public ids and URL fragments must not carry the archive slug/title.
        article["id"] = body_info["oid"]
        article["body"] = {"file": body_info["file"], "bytes": body_info["bytes"]}
        encrypted_articles.append(article)

    index_payload = {
        "format": INDEX_FORMAT,
        "protocol": PROTOCOL,
        "archive_id": archive_id,
        "labels": labels,
        "articles": encrypted_articles,
    }
    index_bytes = json.dumps(
        index_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    index_info = store.put("index", "index", index_bytes)

    template_path = SCRIPTS_DIR / "archive_page.html"
    _assert_regular_source(template_path, max_bytes=MAX_READER_JS_BYTES, label="archive_page.html")
    template_text = template_path.read_text(encoding="utf-8")

    reader_path = SCRIPTS_DIR / "encrypted_reader.js"
    _assert_regular_source(reader_path, max_bytes=MAX_READER_JS_BYTES, label="encrypted_reader.js")
    reader_bytes = reader_path.read_bytes()

    latest = ""
    for article in encrypted_articles:
        archived = str(article.get("archived") or "")
        if archived > latest:
            latest = archived
    entry_html_bytes = _build_entry_html(template_text, latest).encode("utf-8")

    version = _version(addr_key, archive_id, store.files, reader_bytes, entry_html_bytes)

    site_doc = {
        "format": SITE_FORMAT,
        "protocol": PROTOCOL,
        "archive_id": archive_id,
        "kdf": params["kdf"],
        "wrap": params["wrap"],
        "index": {"file": index_info["file"], "bytes": index_info["bytes"]},
        "version": version,
    }
    site_bytes = json.dumps(
        site_doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8") + b"\n"

    changed = store.written > 0
    changed = _write_if_changed(out_real / "reader.js", reader_bytes) or changed
    changed = _write_if_changed(out_real / "index.html", entry_html_bytes) or changed
    changed = _write_if_changed(out_real / ".nojekyll", b"") or changed
    # site.json is the publish switch and is always written last.
    changed = _write_if_changed(out_real / "site.json", site_bytes) or changed

    files = sorted([*PUBLIC_STATIC, *store.files])
    return {
        "status": "ok",
        "changed": bool(changed),
        "version": version,
        "files": files,
        "entries": len(encrypted_articles),
        "objects_written": store.written,
        "objects_reused": store.reused,
    }


def verify_key(key_path: Path, passphrase: str) -> dict:
    """Validate a key file and passphrase without returning the DEK."""
    _, _, params = _unwrap_key(key_path, passphrase)
    return params
