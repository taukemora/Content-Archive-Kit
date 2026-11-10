"""Shared, machine-independent helpers for the test suite.

Everything the tests need to locate is derived, never hard-coded:

* ``ROOT`` is the repository root, computed from this file's location;
* scratch project trees live under the platform temporary directory (``TMPDIR``
  is honoured, overridable with ``CA_TEST_SCRATCH``) so a test can never write
  into the checkout or into a private archive instance;
* sample configuration (``taxonomy.yaml``, ``sources.yaml``, ``media.yaml``) is
  read from ``examples/`` when the repository ships a generic sample, and from
  the archive root when a private instance keeps its own copy;
* runtime state digests tolerate a missing file, so the same guard works in a
  public checkout (no state at all) and in an instance deployment.

Tests must never read a private archive's articles, state, credentials or
machine-local configuration; fixtures are synthetic and live in
``tests/fixtures/``.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
FIXTURES = TESTS / "fixtures"

# Throwaway test projects. Never the repository, never an archive instance.
SCRATCH = Path(
    os.environ.get("CA_TEST_SCRATCH")
    or (Path(tempfile.gettempdir()) / "content-archive-kit-tests")
).resolve()
SCRATCH.mkdir(parents=True, exist_ok=True)

# Interpreter used for subprocess-level tests: an explicitly configured one, the
# checkout's own venv when present, otherwise the running interpreter.
PYTHON = (
    os.environ.get("CA_TEST_PYTHON")
    or (str(ROOT / ".venv" / "bin" / "python") if (ROOT / ".venv" / "bin" / "python").exists() else sys.executable)
)

# Sample configuration resolution order: generic public samples first, then an
# instance copy at the archive root.
_CONFIG_SEARCH = (ROOT / "examples", ROOT)


def template(name: str) -> Path:
    """Return a sample configuration file (``examples/`` first, then root)."""
    for base in _CONFIG_SEARCH:
        candidate = base / name
        if candidate.exists():
            return candidate
    searched = ", ".join(str(base / name) for base in _CONFIG_SEARCH)
    raise FileNotFoundError(f"no sample configuration {name!r}; looked in: {searched}")


def scratch_dir(prefix: str, base: Path | None = None) -> Path:
    """Create a fresh throwaway directory under :data:`SCRATCH`."""
    return Path(tempfile.mkdtemp(dir=str(base or SCRATCH), prefix=prefix))


def archive_project(
    prefix: str,
    scripts: Sequence[str] | None = None,
    *,
    configs: Sequence[str] = ("taxonomy.yaml",),
    state: bool = False,
) -> Path:
    """Create a throwaway archive root with its own ``scripts/`` and config.

    Scripts derive their project root from their own location, so a test that
    runs a script as a subprocess must run the *copy inside the sandbox*;
    otherwise the script would resolve the checkout as its archive root.

    ``scripts=None`` copies the whole ``scripts/`` directory; pass explicit
    names to copy only what a test needs.
    """
    root = scratch_dir(prefix)
    target = root / "scripts"
    if scripts is None:
        shutil.copytree(ROOT / "scripts", target, ignore=shutil.ignore_patterns("__pycache__"))
    else:
        target.mkdir(parents=True, exist_ok=True)
        for name in scripts:
            shutil.copy(ROOT / "scripts" / name, target / name)
    for name in configs:
        shutil.copy(template(name), root / name)
    if state:
        (root / ".state").mkdir(parents=True, exist_ok=True)
        (root / ".state" / "seen.json").write_text('{"entries": {}}', encoding="utf-8")
    return root


def state_digest(path: Path) -> str | None:
    """sha256 of a runtime state file, or ``None`` when the file is absent.

    Comparing the digest before and after a test proves the suite neither
    modified nor created instance state, whether or not the deployment has any.
    """
    try:
        return sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None
