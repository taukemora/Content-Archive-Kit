#!/usr/bin/env python3
"""Read-only public-tree checks for Content Archive Kit.

The default command verifies the checkout or an extracted release artifact
against ``docs/public-tree-sha256.json``:

* exact file membership in both directions (``git ls-files`` for a checkout,
  a filesystem walk for an artifact without ``.git``);
* byte counts and SHA-256 digests;
* canonical relative POSIX paths, no traversal, no symlinks (leaf or ancestor),
  no non-regular files;
* private path screens (``.state``, ``library``, ``trash``, live root
  configuration, key/environment filenames) and a private-key content screen;
* syntax of shipped Python, ``bin/ca`` and JavaScript.

``--update-manifest`` is the explicit maintenance command. It validates the
same safety rules *before* writing, preserves the existing JSON metadata and
file mode, and replaces the manifest atomically. CI never runs it.

These are risk screens, not proof that a tree contains no private material:
nothing here inspects git history, prompts, or external services. Private-key
findings report paths, not matched bytes; syntax and loader diagnostics may
include source values. Use only with reviewed public code, not as a sandbox.

The shipped ``examples/*.yaml`` files are the deliberate allowance for the
live-configuration screen: they are public, schema-shaped placeholders under
``examples/``, while an instance's own root configuration is rejected.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath

# Loading the shipped example configuration executes loader modules in this
# process. The default command must stay read-only for the tree it checks, so
# disable bytecode caching before any loader is imported, regardless of the
# caller's environment (PYTHONDONTWRITEBYTECODE / PYTHONPYCACHEPREFIX).
sys.dont_write_bytecode = True

MANIFEST_REL = "docs/public-tree-sha256.json"
LAUNCHER_REL = "bin/ca"
EXAMPLE_LOADERS = (
    "scripts/common.py", "scripts/media_policy.py", "scripts/publish_site.py",
    "scripts/subscription_selection.py",
)
EXAMPLE_CONFIGS = (
    "examples/taxonomy.yaml",
    "examples/sources.yaml",
    "examples/media.yaml",
    "examples/publish.yaml",
    "examples/selection.yaml",
)

# Local, machine-specific directories an extracted artifact may contain. They
# are the only names ignored in artifact mode; everything else must be tracked
# by the manifest.
LOCAL_ONLY_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
LOCAL_ONLY_FILES = {".DS_Store"}

PRIVATE_COMPONENTS = {".state", "library", "trash", ".handoff"}
ROOT_ONLY_PRIVATE = {
    "publish.yaml",
    "compose.yaml",
    "taxonomy.yaml",
    "sources.yaml",
    "selection.yaml",
    "media.yaml",
    "index.csv",
    "index.md",
}
PRIVATE_NAMES = {"content-key.json", "archive-key.json", "site.key"}
PRIVATE_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
PRIVATE_KEY_RE = re.compile(
    rb"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----|"
    rb"-----BEGIN PGP " rb"PRIVATE KEY BLOCK-----"
)
SHA256_RE = re.compile(r"[0-9a-f]{64}")
MAX_CONTENT_SCAN = 2 * 1024 * 1024


class CheckError(Exception):
    """Operational failure: the check could not run (exit code 2)."""


def _path_problem(rel: object) -> str | None:
    if not isinstance(rel, str) or not rel:
        return "empty path"
    if any(ord(char) < 32 or ord(char) == 127 for char in rel):
        return "control character"
    if "\\" in rel:
        return "backslash separator"
    if rel.startswith("/"):
        return "absolute path"
    if re.match(r"^[A-Za-z]:", rel):
        return "drive-qualified path"
    if PurePosixPath(rel).as_posix() != rel:
        return "non-canonical path"
    if any(part in {"", ".", ".."} for part in rel.split("/")):
        return "empty, '.' or '..' component"
    return None


def _is_private(rel: str) -> bool:
    parts = rel.split("/")
    if any(part in PRIVATE_COMPONENTS for part in parts):
        return True
    name = parts[-1].lower()
    if name == ".env.example":
        return False
    if name == ".env" or name.startswith(".env."):
        return True
    if name.endswith(PRIVATE_SUFFIXES) or name in PRIVATE_NAMES:
        return True
    if name.startswith(("id_rsa", "id_ed25519", "github-ed25519")):
        return True
    return len(parts) == 1 and rel in ROOT_ONLY_PRIVATE


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_manifest(root: Path) -> tuple[dict, dict]:
    link = _symlink_ancestor(root, MANIFEST_REL)
    if link is not None:
        raise CheckError(f"{MANIFEST_REL} must not pass through a symbolic link")
    path = root / MANIFEST_REL
    if path.is_symlink():
        raise CheckError(f"{MANIFEST_REL} must not be a symbolic link")
    if not path.is_file():
        raise CheckError(f"manifest not found: {MANIFEST_REL}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise CheckError(f"cannot read {MANIFEST_REL}: {exc}") from exc
    if not isinstance(raw, dict):
        raise CheckError(f"{MANIFEST_REL} must be a JSON object")
    if raw.get("self_excluded") is not True:
        raise CheckError(f"{MANIFEST_REL} must set self_excluded to true")
    files = raw.get("files")
    if not isinstance(files, dict):
        raise CheckError(f"{MANIFEST_REL} must contain a files object")
    return raw, files


def _decode_records(raw: bytes) -> set[str]:
    return {
        record.decode("utf-8", "surrogateescape")
        for record in raw.split(b"\0")
        if record
    }


def _git(root: Path, *args: str) -> bytes:
    git = shutil.which("git")
    if git is None:
        raise CheckError("git is required to check a tree that contains .git")
    proc = subprocess.run([git, "-C", str(root), *args], capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise CheckError("git failed: " + (detail[0] if detail else f"exit {proc.returncode}"))
    return proc.stdout


def _tracked_files(root: Path) -> set[str]:
    return _decode_records(_git(root, "ls-files", "-z", "--cached"))


def _untracked_files(root: Path) -> list[str]:
    return sorted(_decode_records(_git(root, "ls-files", "-z", "--others", "--exclude-standard")))


def _artifact_files(root: Path) -> tuple[set[str], list[str]]:
    found: set[str] = set()
    findings: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        kept = []
        for name in dirnames:
            full = Path(dirpath) / name
            if name in LOCAL_ONLY_DIRS:
                continue
            if full.is_symlink():
                findings.append(f"symbolic link not allowed: {full.relative_to(root).as_posix()}")
                continue
            kept.append(name)
        dirnames[:] = sorted(kept)
        for name in filenames:
            if name in LOCAL_ONLY_FILES or name.endswith((".pyc", ".pyo")):
                continue
            found.add((Path(dirpath) / name).relative_to(root).as_posix())
    return found, findings


def _symlink_ancestor(root: Path, rel: str) -> Path | None:
    current = root
    for part in rel.split("/")[:-1]:
        current = current / part
        if current.is_symlink():
            return current
    return None


def _tracked_launcher_mode(root: Path) -> list[str]:
    raw = _git(root, "ls-files", "-s", "-z", "--", LAUNCHER_REL)
    records = [record for record in raw.split(b"\0") if record]
    if not records:
        return []
    mode = records[0].split(b" ", 1)[0].decode("ascii", "replace")
    if mode != "100755":
        return [f"{LAUNCHER_REL} must be tracked as executable mode 100755 (found {mode})"]
    return []


def _artifact_launcher_mode(root: Path) -> list[str]:
    path = root / LAUNCHER_REL
    try:
        mode = path.lstat().st_mode
    except OSError:
        return []
    if stat.S_ISLNK(mode):
        return []
    if not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        return [f"{LAUNCHER_REL} must be executable in an extracted artifact"]
    return []


def _check_entry(rel: str, entry: object, findings: list[str]) -> tuple[int, str] | None:
    if not isinstance(entry, dict):
        findings.append(f"invalid manifest entry: {rel}")
        return None
    size = entry.get("bytes")
    digest = entry.get("sha256")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        findings.append(f"invalid manifest entry: {rel}: bytes must be a non-negative integer")
        return None
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        findings.append(f"invalid manifest entry: {rel}: sha256 must be 64 lowercase hex characters")
        return None
    return size, digest


def _verify_file(root: Path, rel: str, expected: tuple[int, str] | None, findings: list[str]) -> None:
    link = _symlink_ancestor(root, rel)
    if link is not None:
        findings.append(f"path traverses a symbolic link: {rel}")
        return
    path = root / rel
    try:
        st = path.lstat()
    except FileNotFoundError:
        findings.append(f"missing from tree: {rel}")
        return
    except OSError:
        findings.append(f"unreadable path: {rel}")
        return
    if stat.S_ISLNK(st.st_mode):
        findings.append(f"symbolic link not allowed: {rel}")
        return
    if not stat.S_ISREG(st.st_mode):
        findings.append(f"not a regular file: {rel}")
        return
    try:
        data = path.read_bytes()
    except OSError:
        findings.append(f"unreadable file: {rel}")
        return
    if expected is not None:
        size, digest = expected
        if len(data) != size:
            findings.append(f"size mismatch: {rel} (manifest {size}, actual {len(data)})")
        if sha256(data).hexdigest() != digest:
            findings.append(f"sha256 mismatch: {rel}")
    if len(data) <= MAX_CONTENT_SCAN and PRIVATE_KEY_RE.search(data):
        findings.append(f"private key material found: {rel}")


def _first_line(raw: bytes) -> str:
    lines = raw.decode("utf-8", "replace").strip().splitlines()
    return lines[0] if lines else "failed"


def _safe_regular(root: Path, rel: str) -> bool:
    if _symlink_ancestor(root, rel) is not None:
        return False
    try:
        st = (root / rel).lstat()
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


def _unsafe_path(root: Path, rel: str) -> bool:
    """True when an existing path is a symlink, non-regular, or symlink-descended."""
    if _symlink_ancestor(root, rel) is not None:
        return True
    try:
        st = (root / rel).lstat()
    except OSError:
        return False
    return not stat.S_ISREG(st.st_mode)


def _check_syntax(root: Path, actual: set[str]) -> list[str]:
    findings: list[str] = []
    for rel in sorted(actual):
        if not rel.endswith(".py") or not _safe_regular(root, rel):
            continue
        try:
            source = (root / rel).read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            findings.append(f"python syntax error: {rel}: not UTF-8 text")
            continue
        try:
            compile(source, rel, "exec")
        except SyntaxError as exc:
            findings.append(f"python syntax error: {rel}: {exc.msg} (line {exc.lineno})")
        except ValueError as exc:
            findings.append(f"python syntax error: {rel}: {exc}")

    if LAUNCHER_REL in actual and _safe_regular(root, LAUNCHER_REL):
        bash = shutil.which("bash")
        if bash is None:
            findings.append("bash not found for launcher syntax check")
        else:
            proc = subprocess.run([bash, "-n", str(root / LAUNCHER_REL)], capture_output=True)
            if proc.returncode != 0:
                findings.append(f"bash syntax error: {LAUNCHER_REL}: {_first_line(proc.stderr)}")

    scripts = [rel for rel in sorted(actual) if rel.endswith((".js", ".mjs")) and _safe_regular(root, rel)]
    if scripts:
        node = shutil.which("node")
        if node is None:
            findings.append("node not found for JavaScript syntax check")
        else:
            for rel in scripts:
                proc = subprocess.run(
                    [node, "--check", str(root / rel)], cwd=str(root), capture_output=True
                )
                if proc.returncode != 0:
                    findings.append(f"javascript syntax error: {rel}: {_first_line(proc.stderr)}")
    return findings


def _load_loader(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(f"_cak_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _check_examples(root: Path) -> list[str]:
    examples = root / "examples"
    scripts = root / "scripts"
    if not examples.is_dir() or not scripts.is_dir():
        return []

    # Importing a loader executes it, so refuse to touch any source or config
    # reached through a symlink, a symlinked ancestor, or a non-regular file.
    unsafe = [rel for rel in ("examples", "scripts") if (root / rel).is_symlink()]
    unsafe.extend(
        rel for rel in (*EXAMPLE_LOADERS, *EXAMPLE_CONFIGS) if _unsafe_path(root, rel)
    )
    if unsafe:
        return [f"example path unsafe: {rel}" for rel in dict.fromkeys(unsafe)]

    findings: list[str] = []
    try:
        common = _load_loader("common", scripts / "common.py")
        media_policy = _load_loader("media_policy", scripts / "media_policy.py")
        publish_site = _load_loader("publish_site", scripts / "publish_site.py")
    except Exception as exc:  # noqa: BLE001 - report, never crash the check
        return [f"example configuration loaders unavailable: {exc}"]

    taxonomy_path = examples / "taxonomy.yaml"
    if taxonomy_path.is_file():
        try:
            taxonomy = common.load_yaml(taxonomy_path)
        except Exception as exc:  # noqa: BLE001
            findings.append(f"example taxonomy.yaml invalid: {exc}")
        else:
            categories = taxonomy.get("categories") if isinstance(taxonomy, dict) else None
            if not isinstance(categories, list) or not categories:
                findings.append("example taxonomy.yaml has no categories")
            else:
                slugs = [item.get("slug") for item in categories if isinstance(item, dict)]
                if len(slugs) != len(categories) or any(
                    not isinstance(slug, str) or not slug for slug in slugs
                ):
                    findings.append("example taxonomy.yaml has an invalid category slug")
                elif len(set(slugs)) != len(slugs):
                    findings.append("example taxonomy.yaml has duplicate category slugs")

    sources_path = examples / "sources.yaml"
    if sources_path.is_file():
        try:
            sources = common.load_yaml(sources_path).get("sources")
        except Exception as exc:  # noqa: BLE001
            findings.append(f"example sources.yaml invalid: {exc}")
        else:
            if not isinstance(sources, list) or not sources:
                findings.append("example sources.yaml has no sources")
            else:
                for source in sources:
                    if not isinstance(source, dict) or source.get("enabled") is not False:
                        name = source.get("id", "<unnamed>") if isinstance(source, dict) else "<unnamed>"
                        findings.append(f"example sources.yaml enables source: {name}")

    if (examples / "media.yaml").is_file():
        try:
            media_policy.load_media_config(examples)
        except Exception as exc:  # noqa: BLE001
            findings.append(f"example media.yaml invalid: {exc}")

    publish_path = examples / "publish.yaml"
    if publish_path.is_file():
        try:
            publish = publish_site.load_config(root, publish_path)
        except Exception as exc:  # noqa: BLE001
            findings.append(f"example publish.yaml invalid: {exc}")
        else:
            if publish.enabled is not False:
                findings.append("example publish.yaml must remain disabled")

    selection_path = examples / "selection.yaml"
    if selection_path.is_file():
        # Use the checked tree's common module, not a host-injected import path.
        # Restore the module table so a caller importing this checker is unchanged.
        previous_common = sys.modules.get("common")
        try:
            sys.modules["common"] = common
            selection_module = _load_loader(
                "subscription_selection", scripts / "subscription_selection.py"
            )
            selection = selection_module.load_policy(root, selection_path)
        except Exception as exc:  # noqa: BLE001
            findings.append(f"example selection.yaml invalid: {exc}")
        else:
            # load_policy returns None for a validated disabled policy.
            if selection is not None:
                findings.append("example selection.yaml must remain disabled")
        finally:
            if previous_common is None:
                sys.modules.pop("common", None)
            else:
                sys.modules["common"] = previous_common

    return findings


def _scan_tree(root: Path) -> tuple[set[str], list[str]]:
    findings: list[str] = []
    if (root / ".git").exists():
        actual = _tracked_files(root)
        for rel in _untracked_files(root):
            findings.append(f"untracked file not allowed: {rel}")
        findings.extend(_tracked_launcher_mode(root))
    else:
        actual, artifact_findings = _artifact_files(root)
        findings.extend(artifact_findings)
        findings.extend(_artifact_launcher_mode(root))
    actual.discard(MANIFEST_REL)
    return actual, findings


def verify_tree(root: Path) -> tuple[list[str], int]:
    """Return ``(findings, checked_file_count)`` for ``root``."""
    _manifest, files = _load_manifest(root)
    findings: list[str] = []

    if MANIFEST_REL in files:
        findings.append(f"{MANIFEST_REL} must not list itself")

    expected: dict[str, tuple[int, str] | None] = {}
    for rel in sorted(files):
        problem = _path_problem(rel)
        if problem:
            findings.append(f"unsafe path: {rel}: {problem}")
            continue
        if _is_private(rel):
            findings.append(f"private path not allowed: {rel}")
            continue
        expected[rel] = _check_entry(rel, files[rel], findings)

    actual, scan_findings = _scan_tree(root)
    findings.extend(scan_findings)
    for rel in sorted(actual):
        problem = _path_problem(rel)
        if problem:
            findings.append(f"unsafe path in tree: {rel}: {problem}")
            continue
        if _is_private(rel):
            findings.append(f"private path not allowed: {rel}")
            continue

    for rel in sorted(set(expected) - actual):
        findings.append(f"missing from tree: {rel}")
    for rel in sorted(actual - set(expected)):
        findings.append(f"not listed in manifest: {rel}")
    for rel in sorted(set(expected) & actual):
        if _path_problem(rel) is None:
            _verify_file(root, rel, expected[rel], findings)

    syntax_targets = {
        rel for rel in actual if _path_problem(rel) is None and not _is_private(rel)
    }
    findings.extend(_check_syntax(root, syntax_targets))
    # The example loaders are imported and executed; only run them after every
    # safety, membership, hash, and syntax check is clean.
    if not findings:
        findings.extend(_check_examples(root))

    return list(dict.fromkeys(findings)), len(actual)


def _update_entries(root: Path, actual: set[str], findings: list[str]) -> dict:
    entries: dict[str, dict] = {}
    for rel in sorted(actual):
        problem = _path_problem(rel)
        if problem:
            findings.append(f"unsafe path in tree: {rel}: {problem}")
            continue
        if _is_private(rel):
            findings.append(f"private path not allowed: {rel}")
            continue
        if _symlink_ancestor(root, rel) is not None:
            findings.append(f"path traverses a symbolic link: {rel}")
            continue
        path = root / rel
        try:
            st = path.lstat()
        except OSError:
            findings.append(f"missing from tree: {rel}")
            continue
        if stat.S_ISLNK(st.st_mode):
            findings.append(f"symbolic link not allowed: {rel}")
            continue
        if not stat.S_ISREG(st.st_mode):
            findings.append(f"not a regular file: {rel}")
            continue
        try:
            data = path.read_bytes()
        except OSError:
            findings.append(f"unreadable file: {rel}")
            continue
        if len(data) <= MAX_CONTENT_SCAN and PRIVATE_KEY_RE.search(data):
            findings.append(f"private key material found: {rel}")
        entries[rel] = {"bytes": len(data), "sha256": sha256(data).hexdigest()}
    return entries


def _write_manifest(target: Path, data: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix=".public-tree-", suffix=".json", dir=str(target.parent))
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, target)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def update_manifest(root: Path) -> tuple[list[str], int]:
    """Regenerate the manifest after validating the whole tree.

    Nothing is written when a safety finding exists, and the existing manifest
    metadata and file mode are preserved. ``captured_at`` records the update.
    """
    actual, findings = _scan_tree(root)
    entries = _update_entries(root, actual, findings)

    target = root / MANIFEST_REL
    if _symlink_ancestor(root, MANIFEST_REL) is not None or target.is_symlink():
        raise CheckError(f"{MANIFEST_REL} must not be a symbolic link")
    previous: dict = {}
    if target.exists():
        if not target.is_file():
            raise CheckError(f"{MANIFEST_REL} is not a regular file")
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise CheckError(f"existing {MANIFEST_REL} is not valid JSON; refusing to overwrite: {exc}") from exc
        if not isinstance(loaded, dict):
            raise CheckError(f"existing {MANIFEST_REL} is not a JSON object; refusing to overwrite")
        previous = loaded

    # Reuse the verification flow before writing: syntax first, then the real
    # example loaders. Invalid code or configuration leaves the manifest bytes
    # (or its absence) untouched.
    if not findings:
        syntax_targets = {
            rel for rel in actual if _path_problem(rel) is None and not _is_private(rel)
        }
        findings = _check_syntax(root, syntax_targets)
        if not findings:
            findings = _check_examples(root)

    if findings:
        return list(dict.fromkeys(findings)), 0

    payload = {key: value for key, value in previous.items() if key != "files"}
    payload["captured_at"] = datetime.now(timezone.utc).isoformat()
    payload["self_excluded"] = True
    payload["files"] = entries
    try:
        _write_manifest(target, payload)
    except OSError as exc:
        raise CheckError(f"cannot write {MANIFEST_REL}: {exc}") from exc
    return [], len(entries)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a public tree against docs/public-tree-sha256.json"
    )
    parser.add_argument("--root", type=Path, default=None, help="project root (default: this checkout)")
    parser.add_argument(
        "--update-manifest",
        action="store_true",
        help=f"regenerate {MANIFEST_REL} after validating the tree (maintenance only; CI never uses it)",
    )
    args = parser.parse_args(argv)
    root = (args.root or Path(__file__).resolve().parents[1]).expanduser().resolve()
    if not root.is_dir():
        print(f"repository check: ERROR: not a directory: {root}", file=sys.stderr)
        return 2
    try:
        if args.update_manifest:
            findings, written = update_manifest(root)
        else:
            findings, written = verify_tree(root)
    except CheckError as exc:
        print(f"repository check: ERROR: {exc}", file=sys.stderr)
        return 2
    for finding in findings:
        print(f"repository check: {finding}")
    if findings:
        print(f"repository check: FAILED ({len(findings)} finding(s))")
        return 1
    if args.update_manifest:
        print(f"repository check: manifest updated ({written} files)")
    else:
        print(f"repository check: OK ({written} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
