#!/usr/bin/env python3
"""Publish the encrypted reading site to a GitHub Pages repository.

This module is the *only* part of the project that is allowed to talk to the
publish repository.  It never touches the archive itself: the library, index
files, seen state and daily-run records are read-only inputs.

Configuration: ``publish.yaml`` in the project root (written and owned by the
main agent).  Exactly these keys are accepted::

    enabled: false                     # bool, default false
    repository: OWNER/REPOSITORY        # the one repository this archive may push to
    branch: main
    site_url: https://OWNER.github.io/REPOSITORY/
    key_file: /absolute/outside/path/site.key
    ssh_key_file: /absolute/outside/path/id_ed25519
    known_hosts_file: /absolute/outside/path/github-known-hosts
    workspace: /absolute/outside/path/publish-workspace
    git_name: archive-publisher             # optional, commit identity
    git_email: archive-publisher@users.noreply.github.com
    pages_timeout_seconds: 20               # optional, 1..120

``repository`` is the allowlist: it must be ``OWNER/REPOSITORY`` and
``site_url`` must be exactly the GitHub Pages URL derived from it, so a stale or
mistyped value is rejected instead of publishing somewhere unexpected.  The SSH
remote is derived from ``repository`` as ``git@github.com:OWNER/REPOSITORY.git``.

Paths must be absolute, outside the project and outside each other's private
areas.  Unknown keys, YAML type coercions, malformed repositories and any
mismatched site URL are rejected instead of being silently ignored.

Workspace layout (all outside the project, mode 0700)::

    workspace/repo/        git worktree; the only .git of the publish flow
    workspace/build/       encrypted_site.build_site() output directory
    workspace/state.json   last publish result (no secrets)
    workspace/publish.lock advisory lock serialising publishers

CLI (wired by the main agent as ``bin/ca publish``)::

    publish_site.py init    # TTY-only passphrase -> external owner-only key
    publish_site.py build   # local export only, no git, no network
    publish_site.py status  # readiness + last publish state, never secrets
    publish_site.py run     # build, whitelist, commit, push, verify Pages
                            # exit 0 success/unchanged, 1 failed, 3 pushed_pending

``run`` is intentionally allowed while ``enabled: false`` so the first
launch can be built and verified manually.  It never flips ``enabled`` itself;
only the user/main agent enables automatic publishing after acceptance.

``maybe_publish(root)`` is the automatic entry point.  It returns ``skipped``
when publishing is disabled or ``CA_SKIP_AUTO_PUBLISH=1``, and ``failed`` on
any error without raising into the indexing caller.

No command accepts a passphrase as an argument or environment variable.  The
production passphrase is read through ``getpass`` from a real TTY only.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import csv
import fcntl
import getpass
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import yaml
from html.parser import HTMLParser

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The publish target is per-instance configuration (publish.yaml), never a
# constant: no upstream default repository may receive a private archive.
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,38}/[A-Za-z0-9._-]{1,100}$")
GIT_REMOTE_TEMPLATE = "git@github.com:{repository}.git"
PAGES_SITE_JSON = "site.json"

ALLOWED_CONFIG_KEYS = {
    "enabled",
    "repository",
    "branch",
    "site_url",
    "key_file",
    "ssh_key_file",
    "workspace",
    "known_hosts_file",
    "git_name",
    "git_email",
    "pages_timeout_seconds",
}
REQUIRED_CONFIG_KEYS = {
    "repository",
    "branch",
    "site_url",
    "key_file",
    "ssh_key_file",
    "workspace",
    "known_hosts_file",
}
DEFAULT_PAGES_TIMEOUT = 20.0
MAX_CONFIG_BYTES = 64 * 1024
MAX_SITE_JSON_BYTES = 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_FILES = 20000

# Capacity reporting is read-only, derived exclusively from the validated
# whitelist of the current export.  It never opens or decrypts an object.
CAPACITY_METRIC_KEYS = (
    "file_count",
    "total_bytes",
    "shell_bytes",
    "object_bytes",
    "largest_file_bytes",
)
CAPACITY_KEYS = CAPACITY_METRIC_KEYS + (
    "status",
    "message",
    "local_limit_bytes",
    "ratio",
    "percent",
    "warning_ratio",
    "critical_ratio",
)
CAPACITY_WARNING_RATIO = 0.75
CAPACITY_CRITICAL_RATIO = 0.90

# The public payload is a closed set: the four static entry files plus opaque
# AES-GCM objects whose names are content-addressed by the exporter.
CORE_PUBLIC_FILES = {"index.html", "site.json", ".nojekyll", "reader.js"}
PUBLIC_STATIC_FILES = frozenset(CORE_PUBLIC_FILES)
OBJECT_FILE_RE = re.compile(r"^objects/[0-9a-f]{64}\.[0-9a-f]{32}\.bin$")

# Mirror of the frozen protocol constants in scripts/encrypted_site.py.  The
# publisher validates the public manifest independently instead of importing
# the exporter, so a compromised/incorrect exporter cannot loosen this check.
SITE_FORMAT = "contentarchive-encrypted-site"
SITE_PROTOCOL = "CA-ENC-1"
SITE_TOP_LEVEL_KEYS = {"format", "protocol", "archive_id", "kdf", "wrap", "index", "version"}
KDF_KEYS = {"name", "hash", "iterations", "salt", "key_length"}
WRAP_KEYS = {"cipher", "nonce", "ciphertext", "tag_length"}
INDEX_KEYS = {"file", "bytes"}
PBKDF2_NAME = "PBKDF2-HMAC-SHA256"
KDF_HASH = "sha256"
PBKDF2_ITERATIONS = 600_000
KDF_KEY_LENGTH = 32
SALT_BYTES = 16
WRAP_CIPHER = "AES-256-GCM"
WRAP_NONCE_BYTES = 12
WRAP_CIPHERTEXT_BYTES = 48
WRAP_TAG_BITS = 128

ARCHIVE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
VERSION_RE = re.compile(r"^[0-9a-f]{32}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
KNOWN_HOST_TOKEN_RE = re.compile(r"^[^\s,#]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
GIT_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+$")

ALLOWED_ENTRY_STATUSES = {"archived", "partial"}
REQUIRED_INDEX_COLUMNS = {"id", "status", "title", "summary"}

GIT_AUTHOR_NAME = "Content Archive Publisher"
GIT_AUTHOR_EMAIL = "archive-publisher@users.noreply.github.com"
GIT_TIMEOUT_SECONDS = 180


class PublishError(Exception):
    """Publishing failed; the local archive is untouched."""


class ConfigError(PublishError):
    """publish.yaml is missing or does not satisfy the frozen schema."""


class SecurityError(PublishError):
    """A path, credential or whitelist rule was violated."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishConfig:
    enabled: bool
    repository: str
    branch: str
    site_url: str
    key_file: Path
    ssh_key_file: Path
    known_hosts_file: Path
    workspace: Path
    git_name: str
    git_email: str
    pages_timeout_seconds: float
    config_path: Path


def _is_within(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([str(child), str(parent)]) == str(parent)
    except ValueError:  # different drives
        return False


def _symlink_ancestor(path: Path, *, include_leaf: bool = False) -> Path | None:
    """Return the first existing symlink component of ``path`` (or its ancestors).

    ``path`` is made absolute without resolving symlinks so a redirected parent
    cannot be silently followed into the real archive, key directory or home.
    """
    current = Path(os.path.abspath(str(path)))
    if include_leaf and current.is_symlink():
        return current
    parent = current.parent
    while True:
        if parent.is_symlink():
            return parent
        if parent.parent == parent:
            return None
        parent = parent.parent


def _check_path_layout(root: Path, paths: Mapping[str, Path]) -> None:
    """Reject symlinked ancestors and resolved overlaps before any write.

    ``paths`` maps a label to an absolute path.  ``project`` is the source tree
    that must stay disjoint from the workspace, and every secret file must stay
    outside both the project and the publish workspace.
    """
    project = Path(os.path.realpath(root))
    for label, path in paths.items():
        include_leaf = label == "workspace"
        link = _symlink_ancestor(path, include_leaf=include_leaf)
        if link is not None:
            raise ConfigError(f"{label} 的路径不能经过符号链接：{link}")

    workspace = Path(os.path.realpath(paths["workspace"]))
    if (
        workspace == project
        or _is_within(workspace, project)
        or _is_within(project, workspace)
    ):
        raise ConfigError("workspace 必须与项目目录完全分离（解析符号链接后也不得重叠）")

    for label in ("key_file", "ssh_key_file", "known_hosts_file"):
        resolved = Path(os.path.realpath(paths[label]))
        if _is_within(resolved, project) or _is_within(resolved, workspace):
            raise ConfigError(f"{label} 必须位于项目和发布 workspace 之外")


def _assert_config_layout(config: PublishConfig) -> None:
    """Runtime re-check: never chmod/lock a path that resolves into the project."""
    _check_path_layout(
        Path(config.config_path).parent,
        {
            "key_file": config.key_file,
            "ssh_key_file": config.ssh_key_file,
            "known_hosts_file": config.known_hosts_file,
            "workspace": config.workspace,
        },
    )


def _settings_path(raw: Any, name: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{name} 必须是非空绝对路径字符串")
    candidate = Path(raw.strip()).expanduser()
    if not candidate.is_absolute():
        raise ConfigError(f"{name} 必须是绝对路径：{raw!r}")
    return Path(os.path.abspath(candidate))


def pages_site_url(repository: str) -> str:
    """Canonical GitHub Pages URL for ``OWNER/REPOSITORY``."""
    owner, name = repository.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/"


def load_config(root: Path, path: Path | None = None) -> PublishConfig:
    """Read and strictly validate publish.yaml.  Never reads key material."""
    root = Path(root).resolve()
    config_path = Path(path) if path is not None else root / "publish.yaml"
    if config_path.is_symlink():
        raise ConfigError("publish.yaml 不能是符号链接")
    if not config_path.is_file():
        raise ConfigError(f"publish.yaml 不存在：{config_path}")
    try:
        size = config_path.stat().st_size
    except OSError as exc:
        raise ConfigError(f"无法读取 publish.yaml：{exc}") from exc
    if size > MAX_CONFIG_BYTES:
        raise ConfigError("publish.yaml 过大")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"publish.yaml 解析失败：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("publish.yaml 顶层必须是映射")

    unknown = sorted(set(raw) - ALLOWED_CONFIG_KEYS)
    if unknown:
        raise ConfigError("publish.yaml 含未知键：" + ", ".join(map(str, unknown)))
    missing = sorted(REQUIRED_CONFIG_KEYS - set(raw))
    if missing:
        raise ConfigError("publish.yaml 缺少键：" + ", ".join(missing))

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("enabled 必须是布尔值 false/true")

    repository = raw["repository"]
    if not isinstance(repository, str):
        raise ConfigError("repository 必须是字符串")
    repository = repository.strip()
    if not REPOSITORY_RE.fullmatch(repository) or repository.lower().endswith(".git"):
        raise ConfigError(f"repository 必须是 OWNER/REPOSITORY 形式：{repository!r}")

    branch = raw["branch"]
    if not isinstance(branch, str) or not BRANCH_RE.fullmatch(branch):
        raise ConfigError(f"branch 不安全或格式无效：{branch!r}")
    if (
        branch.startswith("refs/")
        or ".." in branch
        or "//" in branch
        or branch.endswith(("/", "."))
        or branch.endswith(".lock")
        or "@{" in branch
    ):
        raise ConfigError(f"branch 不安全或格式无效：{branch!r}")

    site_url = raw["site_url"]
    if not isinstance(site_url, str):
        raise ConfigError("site_url 必须是字符串")
    expected_site_url = pages_site_url(repository)
    candidate = site_url.strip().rstrip("/") + "/"
    if candidate != expected_site_url:
        raise ConfigError(
            f"site_url 必须与 repository 对应的 Pages 地址一致：{expected_site_url}"
        )
    site_url = expected_site_url

    key_file = _settings_path(raw["key_file"], "key_file")
    ssh_key_file = _settings_path(raw["ssh_key_file"], "ssh_key_file")
    known_hosts_file = _settings_path(raw["known_hosts_file"], "known_hosts_file")
    workspace = _settings_path(raw["workspace"], "workspace")

    _check_path_layout(
        root,
        {
            "key_file": key_file,
            "ssh_key_file": ssh_key_file,
            "known_hosts_file": known_hosts_file,
            "workspace": workspace,
        },
    )
    if len({key_file, ssh_key_file, known_hosts_file}) != 3:
        raise ConfigError("key_file、ssh_key_file、known_hosts_file 必须互不相同")

    git_name = raw.get("git_name", GIT_AUTHOR_NAME)
    git_email = raw.get("git_email", GIT_AUTHOR_EMAIL)
    if not isinstance(git_name, str) or not git_name.strip() or len(git_name) > 100 or CONTROL_RE.search(git_name):
        raise ConfigError("git_name 必须是 1..100 字符且不含控制字符")
    if not isinstance(git_email, str) or not GIT_EMAIL_RE.fullmatch(git_email):
        raise ConfigError("git_email 格式无效")
    git_name = git_name.strip()

    timeout = raw.get("pages_timeout_seconds", DEFAULT_PAGES_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ConfigError("pages_timeout_seconds 必须是数字")
    timeout = float(timeout)
    if not 1.0 <= timeout <= 120.0:
        raise ConfigError("pages_timeout_seconds 必须在 1..120 之间")

    return PublishConfig(
        enabled=enabled,
        repository=repository,
        branch=branch,
        site_url=site_url,
        key_file=key_file,
        ssh_key_file=ssh_key_file,
        known_hosts_file=known_hosts_file,
        workspace=workspace,
        git_name=git_name,
        git_email=git_email,
        pages_timeout_seconds=timeout,
        config_path=config_path,
    )


# ---------------------------------------------------------------------------
# Small filesystem helpers
# ---------------------------------------------------------------------------


def _atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


def _ensure_private_dir(path: Path, label: str) -> None:
    link = _symlink_ancestor(path, include_leaf=True)
    if link is not None:
        raise SecurityError(f"{label} 的路径不能经过符号链接：{link}")
    if path.is_symlink():
        raise SecurityError(f"{label} 不能是符号链接：{path}")
    if path.exists() and not path.is_dir():
        raise SecurityError(f"{label} 不是目录：{path}")
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise SecurityError(f"{label} 不是目录：{path}")
    os.chmod(path, 0o700)


def _inspect_secret(path: Path, label: str) -> tuple[bool, str]:
    """Readiness of an owner-only secret file; never opens it for reading."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return False, "不存在"
    except OSError as exc:
        return False, f"无法访问：{exc}"
    if stat.S_ISLNK(st.st_mode):
        return False, "是符号链接，已拒绝"
    if not stat.S_ISREG(st.st_mode):
        return False, "不是普通文件"
    if stat.S_IMODE(st.st_mode) & 0o077:
        return False, "权限过宽（应为 0600，其他用户不可读写）"
    return True, "就绪"


def _require_secret(path: Path, label: str) -> None:
    ready, note = _inspect_secret(path, label)
    if not ready:
        raise SecurityError(f"{label} 不可用：{note}（{path}）")


def _sibling_free(path: Path, label: str) -> None:
    if path.is_symlink():
        raise SecurityError(f"{label} 不能是符号链接：{path}")


def _inspect_known_hosts(path: Path) -> tuple[bool, str]:
    """Check a dedicated known_hosts file without trusting the global config."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return False, "不存在"
    except OSError as exc:
        return False, f"无法访问：{exc}"
    if stat.S_ISLNK(st.st_mode):
        return False, "是符号链接，已拒绝"
    if not stat.S_ISREG(st.st_mode):
        return False, "不是普通文件"
    if st.st_size == 0:
        return False, "文件为空"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:65536]
    except OSError as exc:
        return False, f"无法读取：{exc}"
    if not _known_hosts_has_github(text):
        return False, "不含精确的 github.com 主机密钥条目"
    return True, "就绪"


def _known_hosts_has_github(text: str) -> bool:
    """Accept only a real ``github.com`` host field, never a substring match."""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if fields and fields[0].startswith("@"):
            marker = fields[0]
            fields = fields[1:]
            if marker == "@revoked":
                continue  # a revoked key is not a usable trust anchor
        if len(fields) < 2:
            continue  # a host field plus at least one key field is required
        hosts = fields[0]
        if hosts.startswith("|"):
            continue  # hashed hostnames cannot prove they cover github.com
        for token in hosts.split(","):
            token = token.strip()
            if not KNOWN_HOST_TOKEN_RE.fullmatch(token):
                continue
            if token == "github.com" or token == "[github.com]":
                return True
            if token.startswith("[github.com]:"):
                port = token.rsplit(":", 1)[-1]
                if port.isdigit() and 0 < int(port) <= 65535:
                    return True
    return False


def _require_known_hosts(config: PublishConfig) -> None:
    ready, note = _inspect_known_hosts(config.known_hosts_file)
    if not ready:
        raise SecurityError(
            f"known_hosts 不可用：{note}（{config.known_hosts_file}）；"
            "必须使用独立文件，禁止 StrictHostKeyChecking=accept-new 或全局 ssh 配置"
        )


# ---------------------------------------------------------------------------
# Result state
# ---------------------------------------------------------------------------


def _state_path(config: PublishConfig) -> Path:
    return config.workspace / "state.json"


def read_state(config: PublishConfig) -> dict | None:
    path = _state_path(config)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_state(config: PublishConfig, record: Mapping[str, Any]) -> None:
    _ensure_private_dir(config.workspace, "发布 workspace")
    payload = dict(record)
    payload.setdefault("at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    _atomic_write_bytes(
        _state_path(config),
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        mode=0o600,
    )


@contextlib.contextmanager
def _publish_lock(config: PublishConfig):
    _ensure_private_dir(config.workspace, "发布 workspace")
    lock_path = config.workspace / "publish.lock"
    if lock_path.is_symlink():
        raise SecurityError("发布锁文件不能是符号链接")
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PublishError("另一个发布进程正在进行，稍后重试") from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


# ---------------------------------------------------------------------------
# Encrypted site module (owned by another implementer; loaded lazily)
# ---------------------------------------------------------------------------


def _load_encrypted_site():
    try:
        import encrypted_site  # noqa: PLC0415 - lazy by design, sibling module
    except ImportError as exc:
        raise PublishError(
            "scripts/encrypted_site.py 不可用；无法构建加密站点"
        ) from exc
    return encrypted_site


# ---------------------------------------------------------------------------
# Whitelist validation
# ---------------------------------------------------------------------------


def _check_relative_path(raw: Any) -> str:
    """Closed whitelist: four static files plus content-addressed objects."""
    if not isinstance(raw, str) or not raw:
        raise SecurityError(f"发布白名单含非法路径：{raw!r}")
    if "\x00" in raw or "\\" in raw or len(raw) > 512:
        raise SecurityError(f"发布白名单含非法路径：{raw!r}")
    if raw in PUBLIC_STATIC_FILES:
        return raw
    if OBJECT_FILE_RE.fullmatch(raw):
        return raw
    raise SecurityError(f"发布白名单含未授权路径：{raw!r}")


def _exact_keys(node: Any, keys: set[str], where: str) -> dict:
    if not isinstance(node, dict):
        raise SecurityError(f"{where} 必须是对象")
    actual = set(node)
    if actual != keys:
        extra = sorted(actual - keys)
        missing = sorted(keys - actual)
        detail = []
        if extra:
            detail.append("多余 " + ", ".join(extra))
        if missing:
            detail.append("缺少 " + ", ".join(missing))
        raise SecurityError(f"{where} 字段不符合协议（{'；'.join(detail)}）")
    return node


def _b64_field(value: Any, length: int, where: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise SecurityError(f"{where} 必须是规范的 base64 字符串")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise SecurityError(f"{where} 不是合法 base64") from exc
    if len(raw) != length or base64.b64encode(raw).decode("ascii") != value:
        raise SecurityError(f"{where} 必须是 {length} 字节的规范 base64")


def _validate_public_site_data(data: Any) -> dict:
    """Validate the exact frozen public manifest; no arbitrary fields at all."""
    doc = _exact_keys(data, SITE_TOP_LEVEL_KEYS, "site.json")
    if doc["format"] != SITE_FORMAT:
        raise SecurityError("site.json format 不是加密站点协议")
    if doc["protocol"] != SITE_PROTOCOL:
        raise SecurityError("site.json protocol 不是 CA-ENC-1")
    archive_id = doc["archive_id"]
    if not isinstance(archive_id, str) or not ARCHIVE_ID_RE.fullmatch(archive_id):
        raise SecurityError("site.json archive_id 必须是 32 位小写十六进制")
    version = doc["version"]
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise SecurityError("site.json version 必须是 32 位小写十六进制")

    kdf = _exact_keys(doc["kdf"], KDF_KEYS, "site.json kdf")
    if kdf["name"] != PBKDF2_NAME or kdf["hash"] != KDF_HASH:
        raise SecurityError("site.json kdf 算法不符合协议")
    if type(kdf["iterations"]) is not int or kdf["iterations"] != PBKDF2_ITERATIONS:
        raise SecurityError("site.json kdf iterations 不符合协议")
    if type(kdf["key_length"]) is not int or kdf["key_length"] != KDF_KEY_LENGTH:
        raise SecurityError("site.json kdf key_length 不符合协议")
    _b64_field(kdf["salt"], SALT_BYTES, "site.json kdf.salt")

    wrap = _exact_keys(doc["wrap"], WRAP_KEYS, "site.json wrap")
    if wrap["cipher"] != WRAP_CIPHER:
        raise SecurityError("site.json wrap cipher 不符合协议")
    if type(wrap["tag_length"]) is not int or wrap["tag_length"] != WRAP_TAG_BITS:
        raise SecurityError("site.json wrap tag_length 不符合协议")
    _b64_field(wrap["nonce"], WRAP_NONCE_BYTES, "site.json wrap.nonce")
    _b64_field(wrap["ciphertext"], WRAP_CIPHERTEXT_BYTES, "site.json wrap.ciphertext")

    index = _exact_keys(doc["index"], INDEX_KEYS, "site.json index")
    if not isinstance(index["file"], str) or not OBJECT_FILE_RE.fullmatch(index["file"]):
        raise SecurityError("site.json index.file 不是密文对象路径")
    size = index["bytes"]
    if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
        raise SecurityError("site.json index.bytes 必须是非负整数且不超过上限")
    return doc


def _scan_site_json(path: Path) -> dict:
    try:
        st = path.lstat()
    except OSError as exc:
        raise SecurityError(f"site.json 不可用：{exc}") from exc
    if st.st_size > MAX_SITE_JSON_BYTES:
        raise SecurityError("site.json 超出大小上限")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise SecurityError(f"site.json 无法解析：{exc}") from exc
    return _validate_public_site_data(data)


# The published index.html is an empty encrypted shell.  These are the only
# data scripts the reader accepts, and each must carry an empty JSON value.
_INDEX_DATA_SCRIPTS = {"archive-data": list, "labels-data": dict}


class _EncryptedIndexParser(HTMLParser):
    """Collect the security-relevant shape of a published index.html."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.html_markers: list[str | None] = []
        self.ids: set[str] = set()
        self.duplicate_id: str | None = None
        self.blocks: dict[str, list[tuple[str | None, str]]] = {
            name: [] for name in _INDEX_DATA_SCRIPTS
        }
        self._current: str | None = None
        self._current_type: str | None = None
        self._chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs)

    def _start(self, tag, attrs) -> None:
        # HTML browsers keep the first duplicate attribute, while a dict keeps
        # the last. Refuse the ambiguity before interpreting ids or markers.
        names = [key.lower() for key, _ in attrs]
        if len(names) != len(set(names)):
            raise ValueError("duplicate HTML attributes")
        attributes = {
            key.lower(): ("" if value is None else value) for key, value in attrs
        }
        if tag == "html":
            self.html_markers.append(attributes.get("data-ca-encrypted"))
        ident = attributes.get("id")
        if ident is not None:
            if ident in self.ids:
                self.duplicate_id = ident
            else:
                self.ids.add(ident)
        if tag == "script" and ident in self.blocks:
            self._current = ident
            self._current_type = attributes.get("type")
            self._chunks = []

    def handle_endtag(self, tag):
        if tag == "script" and self._current is not None:
            self.blocks[self._current].append(
                (self._current_type, "".join(self._chunks))
            )
            self._current = None
            self._current_type = None
            self._chunks = []

    def handle_data(self, data):
        if self._current is not None:
            self._chunks.append(data)


def _validate_encrypted_index_html(
    raw: bytes | str, label: str = "index.html"
) -> None:
    """Fail closed unless ``raw`` is the empty encrypted entry shell.

    This is a regression gate against accidentally publishing the plaintext
    offline reader: it does not attempt to find every secret in arbitrary HTML
    (article bodies and search data must never be in this file at all).
    """
    if isinstance(raw, str):
        text = raw
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecurityError(f"{label} 不是 UTF-8") from exc
    parser = _EncryptedIndexParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - malformed HTML must reject
        raise SecurityError(f"{label} HTML 解析失败") from exc
    if parser.duplicate_id is not None:
        raise SecurityError(f"{label} 含重复 id：{parser.duplicate_id}")
    if len(parser.html_markers) != 1 or parser.html_markers[0] != "1":
        raise SecurityError(f"{label} 缺少 data-ca-encrypted=\"1\" 加密标记")
    for name, expected_type in _INDEX_DATA_SCRIPTS.items():
        blocks = parser.blocks[name]
        if len(blocks) != 1:
            raise SecurityError(f"{label} 必须恰好有一个 {name} 数据脚本")
        script_type, body = blocks[0]
        if (script_type or "").strip().lower() != "application/json":
            raise SecurityError(f"{label} 的 {name} 脚本类型必须是 application/json")
        try:
            value = json.loads(body)
        except ValueError as exc:
            raise SecurityError(f"{label} 的 {name} 数据不是有效 JSON") from exc
        if type(value) is not expected_type or value:
            kind = "空列表 []" if expected_type is list else "空对象 {}"
            raise SecurityError(f"{label} 的 {name} 必须是{kind}")


def validate_whitelist(output_dir: Path, files: Iterable[str]) -> list[str]:
    """Validate the publisher-visible file list and return it sorted.

    Only regular files below ``output_dir`` are accepted: no symlinks, no
    traversal, no plaintext/source suffixes, no ``.git`` content.
    """
    output = Path(output_dir)
    _sibling_free(output, "导出目录")
    if not output.is_dir():
        raise SecurityError(f"导出目录不存在：{output}")
    real_output = os.path.realpath(output)
    clean: list[str] = []
    seen: set[str] = set()
    total = 0
    for raw in files:
        relative = _check_relative_path(raw)
        if relative in seen:
            raise SecurityError(f"发布白名单重复路径：{relative}")
        seen.add(relative)
        candidate = output / relative
        try:
            st = candidate.lstat()
        except OSError as exc:
            raise SecurityError(f"发布白名单文件不存在：{relative}（{exc}）") from exc
        if stat.S_ISLNK(st.st_mode):
            raise SecurityError(f"发布白名单不允许符号链接：{relative}")
        if not stat.S_ISREG(st.st_mode):
            raise SecurityError(f"发布白名单只允许普通文件：{relative}")
        real = os.path.realpath(candidate)
        if not _is_within(Path(real), Path(real_output)):
            raise SecurityError(f"发布白名单文件越出导出目录：{relative}")
        if st.st_size > MAX_FILE_BYTES:
            raise SecurityError(f"发布文件过大：{relative}")
        total += st.st_size
        if total > MAX_TOTAL_BYTES:
            raise SecurityError("发布文件总大小超出上限")
        clean.append(relative)
    if len(clean) > MAX_FILES:
        raise SecurityError("发布文件数量超出上限")
    missing = CORE_PUBLIC_FILES - seen
    if missing:
        raise SecurityError("发布白名单缺少核心公共文件：" + ", ".join(sorted(missing)))
    site = _scan_site_json(output / PAGES_SITE_JSON)
    if site["index"]["file"] not in seen:
        raise SecurityError("site.json 指向的索引密文对象不在发布白名单内")
    try:
        index_raw = _read_nofollow(output / "index.html", MAX_FILE_BYTES)
    except OSError as exc:
        raise SecurityError(f"无法读取 index.html：{exc}") from exc
    _validate_encrypted_index_html(index_raw, "index.html")
    return sorted(clean)


# ---------------------------------------------------------------------------
# Capacity reporting
# ---------------------------------------------------------------------------


def _capacity_level(total: int, limit: int) -> str:
    """Integer thresholds so 75%/90% cannot drift on float rounding."""
    if total * 100 >= limit * int(round(CAPACITY_CRITICAL_RATIO * 100)):
        return "critical"
    if total * 100 >= limit * int(round(CAPACITY_WARNING_RATIO * 100)):
        return "warning"
    return "ok"


def _capacity_message(level: str, total: int, percent: float, limit: int) -> str:
    mib = total / (1024 * 1024)
    limit_mib = limit / (1024 * 1024)
    if total > 0 and percent < 0.1:
        shown = "<0.1%"  # truncated percent cannot resolve a tiny export
    elif percent < 1:
        shown = f"{percent:.2f}%"
    else:
        shown = f"{percent:.1f}%"
    detail = (
        f"站点体积 {mib:.2f} MiB（{total} 字节），"
        f"占本地上限 {shown}（硬上限 {limit_mib:.2f} MiB）"
    )
    if level == "critical":
        return f"已达 {int(round(CAPACITY_CRITICAL_RATIO * 100))}% 预警线：" + detail
    if level == "warning":
        return f"已达 {int(round(CAPACITY_WARNING_RATIO * 100))}% 预警线：" + detail
    return detail


def _capacity_unavailable(reason: str) -> dict:
    """Honest failure state: never a zero that could read as an empty site."""
    return {
        "status": "unavailable",
        "reason": reason,
        "local_limit_bytes": MAX_TOTAL_BYTES,
    }


def _capacity_report(output_dir: Path, files: Iterable[str]) -> dict:
    """Quantify only the already-validated publish artifacts.

    Sizes come from ``lstat`` on the exact whitelist paths: no object is opened
    or decrypted, no unlisted file is counted, and sizes are never taken from
    the builder's return value.
    """
    output = Path(output_dir)
    shell_bytes = 0
    object_bytes = 0
    largest = 0
    count = 0
    total = 0
    for relative in files:
        _check_relative_path(relative)
        candidate = output / relative
        try:
            st = candidate.lstat()
        except OSError as exc:
            raise SecurityError(f"无法统计发布文件：{relative}（{exc}）") from exc
        if stat.S_ISLNK(st.st_mode):
            raise SecurityError(f"发布白名单不允许符号链接：{relative}")
        if not stat.S_ISREG(st.st_mode):
            raise SecurityError(f"发布白名单只允许普通文件：{relative}")
        size = st.st_size
        if size > MAX_FILE_BYTES:
            raise SecurityError(f"发布文件过大：{relative}")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise SecurityError("发布文件总大小超出上限")
        if relative in PUBLIC_STATIC_FILES:
            shell_bytes += size
        elif OBJECT_FILE_RE.fullmatch(relative):
            object_bytes += size
        else:  # pragma: no cover - _check_relative_path already rejects
            raise SecurityError(f"发布白名单含未授权路径：{relative}")
        largest = max(largest, size)
        count += 1

    limit = MAX_TOTAL_BYTES
    level = _capacity_level(total, limit)
    ratio = total / limit if limit > 0 else 0.0
    # Truncate rather than round: a displayed 90.0% must never belong to a site
    # the integer check still classifies as merely "warning".
    percent = (total * 1000 // limit) / 10 if limit > 0 else 0.0
    return {
        "status": level,
        "message": _capacity_message(level, total, percent, limit),
        "file_count": count,
        "total_bytes": total,
        "shell_bytes": shell_bytes,
        "object_bytes": object_bytes,
        "largest_file_bytes": largest,
        "local_limit_bytes": limit,
        "ratio": round(ratio, 6),
        "percent": percent,
        "warning_ratio": CAPACITY_WARNING_RATIO,
        "critical_ratio": CAPACITY_CRITICAL_RATIO,
    }


def _capacity_summary(report: Mapping[str, Any]) -> dict:
    """Allowlist projection for state records; unknown fields never escape."""
    return {key: report[key] for key in CAPACITY_KEYS if key in report}


def _export_files(output_dir: Path) -> list[str]:
    """List allowlisted names present in an export without following links.

    Only the build directory's closed surface is inspected: the four static
    entry files and ``objects/*.bin``.  The archive, raw snapshots and ``.git``
    are never scanned.
    """
    output = Path(output_dir)
    try:
        entries = list(os.scandir(output))
    except OSError:
        return []
    names: list[str] = []
    for entry in entries:
        if entry.name in PUBLIC_STATIC_FILES:
            names.append(entry.name)
        elif entry.name == "objects":
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                raise SecurityError("导出目录的 objects 必须是普通目录")
            try:
                children = list(os.scandir(entry.path))
            except OSError as exc:
                raise SecurityError(f"无法读取导出目录 objects：{exc}") from exc
            for child in children:
                relative = f"objects/{child.name}"
                if OBJECT_FILE_RE.fullmatch(relative):
                    names.append(relative)
    return sorted(names)


def _status_capacity(config: PublishConfig) -> dict:
    """Read-only capacity of the current export for ``status``.

    Never builds, publishes, runs git or touches the network; an absent,
    partial or invalid export is reported as unavailable instead of zero.
    """
    output = config.workspace / "build"
    if output.is_symlink():
        return _capacity_unavailable("导出目录是符号链接，已拒绝统计")
    if output.exists() and not output.is_dir():
        return _capacity_unavailable("导出目录不是目录")
    if not output.is_dir():
        return _capacity_unavailable("尚未构建：build 目录不存在")
    try:
        files = _export_files(output)
        if not files:
            return _capacity_unavailable("导出目录没有可发布的加密站点文件")
        return _capacity_report(output, validate_whitelist(output, files))
    except (OSError, PublishError) as exc:
        return _capacity_unavailable(str(exc))


def _validate_build_result(output_dir: Path, result: Any) -> dict:
    if not isinstance(result, dict):
        raise PublishError("build_site 返回值必须是 dict")
    if result.get("status") != "ok":
        raise PublishError(f"build_site 未成功：status={result.get('status')!r}")
    version = result.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise PublishError("build_site 返回了非法 version")
    changed = result.get("changed")
    if not isinstance(changed, bool):
        raise PublishError("build_site 返回值缺少布尔 changed")
    for key in ("entries", "objects_written", "objects_reused"):
        value = result.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PublishError(f"build_site 返回值 {key} 必须是非负整数")
    files = result.get("files")
    if not isinstance(files, list):
        raise PublishError("build_site 返回值 files 必须是列表")
    whitelist = validate_whitelist(Path(output_dir), files)
    site = _scan_site_json(Path(output_dir) / PAGES_SITE_JSON)
    if site.get("version") != version:
        raise PublishError("build_site 返回的 version 与 site.json 不一致")
    return {
        "status": "ok",
        "changed": changed,
        "version": version,
        "archive_id": site["archive_id"],
        "files": whitelist,
        "entries": result["entries"],
        "objects_written": result["objects_written"],
        "objects_reused": result["objects_reused"],
        "capacity": _capacity_report(Path(output_dir), whitelist),
    }


# ---------------------------------------------------------------------------
# Entry completeness gate
# ---------------------------------------------------------------------------


def verify_entries(root: Path) -> int:
    """Refuse to publish unfinished/empty entries instead of skipping them."""
    index_path = Path(root) / "index.csv"
    if not index_path.is_file():
        raise PublishError("index.csv 不存在，请先运行 bin/ca index")
    problems: list[str] = []
    count = 0
    try:
        with index_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = set(reader.fieldnames or [])
            missing = REQUIRED_INDEX_COLUMNS - fields
            if missing:
                raise PublishError("index.csv 缺少列：" + ", ".join(sorted(missing)))
            for row in reader:
                count += 1
                entry_id = (row.get("id") or "").strip() or "(无 id)"
                status_value = (row.get("status") or "").strip()
                if status_value not in ALLOWED_ENTRY_STATUSES:
                    problems.append(f"{entry_id}: status={status_value or '(空)'}")
                if not (row.get("title") or "").strip():
                    problems.append(f"{entry_id}: 空标题")
                if not (row.get("summary") or "").strip():
                    problems.append(f"{entry_id}: 空摘要")
    except OSError as exc:
        raise PublishError(f"无法读取 index.csv：{exc}") from exc
    if problems:
        shown = "; ".join(problems[:8])
        if len(problems) > 8:
            shown += f"（共 {len(problems)} 项）"
        raise PublishError("发布前校验未通过，请修正后重试（不会静默跳过）：" + shown)
    return count


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_only(root: Path, config: PublishConfig, site_module=None, *,
               lock: bool = False) -> dict:
    """Export the encrypted site locally.  No git, no network."""
    root = Path(root)
    _assert_config_layout(config)
    _require_secret(config.key_file, "站点密钥")
    _ensure_private_dir(config.workspace, "发布 workspace")
    output = config.workspace / "build"
    _sibling_free(output, "导出目录")
    if output.exists() and not output.is_dir():
        raise SecurityError(f"导出目录不是目录：{output}")
    site = site_module if site_module is not None else _load_encrypted_site()
    # The CLI build runs under the same publisher lock so callers keep the
    # frozen serialisation contract; publish_site() already holds it.
    context = _publish_lock(config) if lock else contextlib.nullcontext()
    with context:
        result = site.build_site(root, output, config.key_file)
        validated = _validate_build_result(output, result)
    validated["output_dir"] = str(output)
    return validated


# ---------------------------------------------------------------------------
# Git transport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitTarget:
    """Resolved remote.  Production builds it from config; tests may inject."""

    url: str
    env: Mapping[str, str]
    author_name: str = GIT_AUTHOR_NAME
    author_email: str = GIT_AUTHOR_EMAIL


def production_target(config: PublishConfig) -> GitTarget:
    ssh_command = " ".join(
        [
            "ssh",
            "-F",
            os.devnull,
            "-i",
            shlex.quote(str(config.ssh_key_file)),
            "-o",
            f"UserKnownHostsFile={shlex.quote(str(config.known_hosts_file))}",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
        ]
    )
    return GitTarget(
        url=GIT_REMOTE_TEMPLATE.format(repository=config.repository),
        env={"GIT_SSH_COMMAND": ssh_command},
        author_name=config.git_name,
        author_email=config.git_email,
    )


# Only non-Git, non-credential variables are inherited.  Everything that can
# redirect the repository, index, object store or inject config is dropped and
# rebuilt explicitly so a hostile parent environment cannot retarget the push.
INHERITED_ENV_KEYS = (
    "PATH",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "WINDIR",
)
FORBIDDEN_GIT_ENV = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_VALUE_0",
    "GIT_CONFIG_PARAMETERS",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
}


def _git_env(target: GitTarget) -> dict[str, str]:
    for key in target.env:
        if str(key).upper() in FORBIDDEN_GIT_ENV or str(key).upper().startswith("GIT_CONFIG_"):
            raise SecurityError(f"发布传输环境不允许重定向 Git 位置或注入配置：{key}")
    env = {key: os.environ[key] for key in INHERITED_ENV_KEYS if key in os.environ}
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ASKPASS": "",
            "GCM_INTERACTIVE": "never",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    env.update({str(k): str(v) for k, v in target.env.items()})
    env.update(
        {
            "GIT_AUTHOR_NAME": target.author_name,
            "GIT_AUTHOR_EMAIL": target.author_email,
            "GIT_COMMITTER_NAME": target.author_name,
            "GIT_COMMITTER_EMAIL": target.author_email,
        }
    )
    return env


def _run_git(repo: Path, args: list[str], target: GitTarget, *, check: bool = True) -> subprocess.CompletedProcess:
    command = [
        "git",
        "-c",
        "credential.helper=",
        "-c",
        "commit.gpgsign=false",
        "-c",
        f"core.hooksPath={repo / '.git' / 'hooks-disabled'}",
        "-C",
        str(repo),
        *args,
    ]
    try:
        completed = subprocess.run(
            command,
            env=_git_env(target),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise PublishError("找不到 git 可执行文件") from exc
    except subprocess.TimeoutExpired as exc:
        raise PublishError(f"git 命令超时：{' '.join(args[:3])}") from exc
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise PublishError(f"git {' '.join(args[:3])} 失败：{detail[-1] if detail else '未知错误'}")
    return completed


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


MIN_PASSPHRASE_LENGTH = 12


def _validate_passphrase(passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise PublishError(f"口令至少 {MIN_PASSPHRASE_LENGTH} 个字符")
    if len(set(passphrase)) < 6:
        raise PublishError("口令过于简单，请使用更长的随机口令")


def init_key(root: Path, config: PublishConfig, site_module=None) -> dict:
    """Create the external owner-only site key using a TTY passphrase."""
    if not _stdin_is_tty():
        raise PublishError("初始化必须在交互式终端（TTY）中运行，口令不会从参数或环境变量读取")
    _assert_config_layout(config)
    _sibling_free(config.key_file, "密钥文件")
    if config.key_file.exists():
        raise PublishError(f"密钥文件已存在，拒绝覆盖：{config.key_file}")
    first = getpass.getpass("设置发布解锁口令（输入不回显）: ")
    second = getpass.getpass("再次输入以确认: ")
    if first != second:
        raise PublishError("两次输入的口令不一致")
    _validate_passphrase(first)
    site = site_module if site_module is not None else _load_encrypted_site()
    site.initialize_key(config.key_file, first)
    ready, note = _inspect_secret(config.key_file, "密钥文件")
    if not ready:
        raise PublishError(f"密钥文件创建后不可用：{note}")
    return {
        "status": "ok",
        "key_file": str(config.key_file),
        "message": "站点密钥已创建（口令未显示、未记录）",
    }


def status(root: Path, config: PublishConfig) -> dict:
    key_ready, key_note = _inspect_secret(config.key_file, "站点密钥")
    ssh_ready, ssh_note = _inspect_secret(config.ssh_key_file, "SSH 部署密钥")
    hosts_ready, hosts_note = _inspect_known_hosts(config.known_hosts_file)
    build_site_json = config.workspace / "build" / PAGES_SITE_JSON
    state = read_state(config)
    return {
        "status": "ok",
        "enabled": config.enabled,
        "repository": config.repository,
        "branch": config.branch,
        "site_url": config.site_url,
        "config": str(config.config_path),
        "key": {"path": str(config.key_file), "ready": key_ready, "note": key_note},
        "ssh_key": {"path": str(config.ssh_key_file), "ready": ssh_ready, "note": ssh_note},
        "known_hosts": {
            "path": str(config.known_hosts_file),
            "ready": hosts_ready,
            "note": hosts_note,
        },
        "workspace": str(config.workspace),
        "site_build": {
            "ready": build_site_json.is_file(),
            "path": str(build_site_json),
        },
        "capacity": _status_capacity(config),
        "last_publish": state,
    }


def _remote_tip(repo: Path, target: GitTarget, branch: str) -> str | None:
    completed = _run_git(
        repo,
        ["ls-remote", "--heads", target.url, f"refs/heads/{branch}"],
        target,
        check=False,
    )
    if completed.returncode != 0:
        raise PublishError("无法读取远端仓库状态（网络、凭据或主机密钥校验失败）")
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == f"refs/heads/{branch}":
            sha = parts[0]
            if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
                raise PublishError("远端返回了非法 commit id")
            return sha
    return None


def _rev_parse(repo: Path, ref: str, target: GitTarget) -> str | None:
    completed = _run_git(repo, ["rev-parse", "--verify", ref], target, check=False)
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _ensure_repo(config: PublishConfig, target: GitTarget) -> Path:
    repo = config.workspace / "repo"
    _sibling_free(repo, "发布工作区")
    if repo.exists():
        if not repo.is_dir():
            raise SecurityError(f"发布工作区不是目录：{repo}")
        if (repo / ".git").is_symlink():
            raise SecurityError("发布工作区的 .git 不能是符号链接")
        if not (repo / ".git").is_dir():
            raise SecurityError("发布工作区不是发布器管理的 git 仓库，拒绝复用")
    else:
        repo.mkdir(mode=0o700)
        _run_git(repo, ["init", "--initial-branch", config.branch], target)
    remotes = _run_git(repo, ["remote"], target).stdout.split()
    if "origin" in remotes:
        current = _run_git(repo, ["remote", "get-url", "origin"], target).stdout.strip()
        if current != target.url:
            raise SecurityError("github origin 与受信目标不一致，拒绝发布")
    else:
        _run_git(repo, ["remote", "add", "origin", target.url], target)
    return repo


def _fetch_base(repo: Path, target: GitTarget, branch: str) -> tuple[str | None, str | None]:
    """Return (advertised tip, fetched base commit).  Latest wins on a race."""
    tip = _remote_tip(repo, target, branch)
    if tip is None:
        return None, None
    refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    for _ in range(2):
        _run_git(repo, ["fetch", "--no-tags", "--prune", target.url, refspec], target)
        base = _rev_parse(repo, f"refs/remotes/origin/{branch}", target)
        if base is not None:
            return tip, base
    raise PublishError("无法获取远端分支")


def _assert_managed_site(repo: Path, base: str, archive_id: str, target: GitTarget) -> None:
    out = _run_git(repo, ["ls-tree", "-r", "-z", base], target).stdout
    paths: list[str] = []
    for entry in (item for item in out.split("\x00") if item):
        meta, _, path = entry.partition("\t")
        fields = meta.split()
        if len(fields) != 3:
            raise SecurityError("远端 tree 格式异常，拒绝覆盖")
        mode, object_type, _ = fields
        if mode not in {"100644", "100755"} or object_type != "blob":
            raise SecurityError(f"远端包含非普通文件（mode {mode}）：{path}")
        paths.append(path)
    if "site.json" not in paths or "index.html" not in paths:
        raise SecurityError("目标仓库已有内容但不是受管理的加密站点，拒绝覆盖")
    for path in paths:
        _check_relative_path(path)
    blob = _run_git(repo, ["show", f"{base}:site.json"], target, check=False)
    if blob.returncode != 0:
        raise SecurityError("无法读取远端 site.json，拒绝覆盖")
    if len(blob.stdout.encode("utf-8")) > MAX_SITE_JSON_BYTES:
        raise SecurityError("远端 site.json 超出大小上限")
    try:
        remote_site = json.loads(blob.stdout)
    except ValueError as exc:
        raise SecurityError(f"远端 site.json 无法解析：{exc}") from exc
    remote_site = _validate_public_site_data(remote_site)
    if remote_site.get("archive_id") != archive_id:
        raise SecurityError("远端站点 archive_id 与本地密钥不一致，拒绝覆盖其他站点")


def _commit_entries(repo: Path, commit: str, target: GitTarget) -> list[tuple[str, str, int, str]]:
    """Return ``(mode, object_id, size, path)`` for every blob in a commit."""
    out = _run_git(repo, ["ls-tree", "-r", "-l", "-z", commit], target).stdout
    entries: list[tuple[str, str, int, str]] = []
    for item in (chunk for chunk in out.split("\x00") if chunk):
        meta, _, path = item.partition("\t")
        fields = meta.split()
        if len(fields) != 4:
            raise SecurityError(f"历史提交 tree 格式异常：{commit[:12]}")
        mode, object_type, object_id, size_field = fields
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise SecurityError(
                f"历史提交含非普通文件（mode {mode}）：{path}（{commit[:12]}）"
            )
        try:
            size = int(size_field)
        except ValueError as exc:
            raise SecurityError(f"历史提交 blob 大小异常：{path}（{commit[:12]}）") from exc
        if size < 0 or not COMMIT_RE.fullmatch(object_id):
            raise SecurityError(f"历史提交 blob 标识异常：{path}（{commit[:12]}）")
        entries.append((mode, object_id, size, path))
    return entries


def _audit_commit(repo: Path, commit: str, archive_id: str, target: GitTarget) -> None:
    """A pushed commit may only contain the closed public site shape."""
    entries = _commit_entries(repo, commit, target)
    paths = [path for _, _, _, path in entries]
    if len(set(paths)) != len(paths):
        raise SecurityError(f"历史提交含重复路径：{commit[:12]}")
    total = 0
    site_entry: tuple[str, int] | None = None
    index_entry: str | None = None
    for _mode, object_id, size, path in entries:
        _check_relative_path(path)
        if size > MAX_FILE_BYTES:
            raise SecurityError(f"历史提交文件过大：{path}（{commit[:12]}）")
        total += size
        if path == PAGES_SITE_JSON:
            site_entry = (object_id, size)
        elif path == "index.html":
            index_entry = object_id
    if total > MAX_TOTAL_BYTES:
        raise SecurityError(f"历史提交总大小超出上限：{commit[:12]}")
    missing = CORE_PUBLIC_FILES - set(paths)
    if missing:
        raise SecurityError(
            "历史提交不是受管理的加密站点（缺少 "
            + ", ".join(sorted(missing))
            + f"）：{commit[:12]}"
        )
    if site_entry is None:  # pragma: no cover - covered by the missing check
        raise SecurityError(f"历史提交缺少 site.json：{commit[:12]}")
    object_id, size = site_entry
    if size > MAX_SITE_JSON_BYTES:
        raise SecurityError(f"历史提交 site.json 超出大小上限：{commit[:12]}")
    raw = _run_git(repo, ["cat-file", "blob", object_id], target).stdout
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SecurityError(f"历史提交 site.json 无法解析：{commit[:12]}") from exc
    site = _validate_public_site_data(data)
    if site["archive_id"] != archive_id:
        raise SecurityError(
            f"历史提交属于其他站点（archive_id 不匹配）：{commit[:12]}"
        )
    if site["index"]["file"] not in paths:
        raise SecurityError(f"历史提交缺少索引密文对象：{commit[:12]}")
    if index_entry is None:  # pragma: no cover - covered by the missing check
        raise SecurityError(f"历史提交缺少 index.html：{commit[:12]}")
    index_raw = _run_git(repo, ["cat-file", "blob", index_entry], target).stdout
    _validate_encrypted_index_html(index_raw, f"历史提交 index.html（{commit[:12]}）")


def _audit_push_history(
    repo: Path,
    target: GitTarget,
    base: str | None,
    head: str,
    files: Iterable[str],
    archive_id: str,
) -> None:
    """Audit every commit that this push would publish.

    With an empty remote the whole reachable history is new; otherwise only the
    commits after the fetched base are new.  Later retries therefore re-check a
    previously rejected workspace instead of trusting a clean HEAD tree.
    """
    if not COMMIT_RE.fullmatch(head):
        raise SecurityError("待推送 commit id 非法")
    revision_range = head if base is None else f"{base}..{head}"
    commits = _run_git(repo, ["rev-list", "--reverse", revision_range], target).stdout.split()
    if not commits and base != head:
        raise SecurityError("无法枚举待推送提交，拒绝发布未审计历史")
    for commit in commits:
        if not COMMIT_RE.fullmatch(commit):
            raise SecurityError("待推送历史含非法 commit id")
        _audit_commit(repo, commit, archive_id, target)
    _assert_head_tree(repo, head, files, target)


def _assert_head_tree(repo: Path, head: str, files: Iterable[str], target: GitTarget) -> None:
    head_paths = sorted(path for _, _, _, path in _commit_entries(repo, head, target))
    expected = sorted(set(files))
    if head_paths != expected:
        raise SecurityError("HEAD 树路径集合与构建白名单不一致，拒绝推送")


def _checkout_base(repo: Path, base: str, branch: str, target: GitTarget) -> None:
    _run_git(repo, ["checkout", "--force", "-B", branch, base], target)
    _run_git(repo, ["clean", "-ffdx"], target)


def _read_nofollow(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise SecurityError(f"发布文件不是普通文件：{path.name}")
        chunks = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise SecurityError(f"发布文件过大：{path.name}")
    return data


def _sync_worktree(repo: Path, build_dir: Path, files: Iterable[str]) -> None:
    repo_real = Path(os.path.realpath(repo))
    build_real = Path(os.path.realpath(build_dir))
    if _is_within(repo_real, build_real) or _is_within(build_real, repo_real):
        raise SecurityError("导出目录与 git 工作区不能重叠")
    for child in os.scandir(repo):
        if child.name == ".git":
            continue
        path = Path(child.path)
        if child.is_symlink() or not child.is_dir(follow_symlinks=False):
            path.unlink()
        else:
            shutil.rmtree(path)
    for relative in files:
        source = build_dir / relative
        data = _read_nofollow(source, MAX_FILE_BYTES)
        _atomic_write_bytes(repo / relative, data, mode=0o644)


def _staged_changes(repo: Path, target: GitTarget) -> bool:
    _run_git(repo, ["add", "-A"], target)
    completed = _run_git(repo, ["diff", "--cached", "--quiet"], target, check=False)
    if completed.returncode not in (0, 1):
        raise PublishError("无法检查待提交变更")
    return completed.returncode == 1


def _commit(repo: Path, target: GitTarget, version: str) -> str:
    # Commit messages never contain article titles, summaries or URLs.
    _run_git(repo, ["commit", "--no-verify", "-m", f"Publish site {version}"], target)
    head = _rev_parse(repo, "HEAD", target)
    if head is None:
        raise PublishError("提交后无法解析 HEAD")
    return head


def _push(repo: Path, target: GitTarget, branch: str, commit: str | None) -> None:
    if commit is None:
        raise PublishError("没有可推送的提交")
    _run_git(repo, ["push", "--porcelain", target.url, f"{commit}:refs/heads/{branch}"], target)


def _fetch_pages_site_json(url: str, timeout: float) -> dict:
    if not url.startswith("https://"):
        raise PublishError("只允许通过 https 读取 Pages site.json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        url,
        headers={
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": "ContentArchivePublisher/1.0",
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            status_code = getattr(response, "status", 200)
            if status_code != 200:
                raise PublishError(f"Pages 返回 HTTP {status_code}")
            raw = response.read(MAX_SITE_JSON_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PublishError(f"读取 Pages site.json 失败：{exc}") from exc
    if len(raw) > MAX_SITE_JSON_BYTES:
        raise PublishError("Pages site.json 超出大小上限")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PublishError(f"Pages site.json 无法解析：{exc}") from exc
    if not isinstance(data, dict):
        raise PublishError("Pages site.json 顶层不是对象")
    return data


def _verify_pages(config: PublishConfig, site: Mapping[str, Any], commit: str,
                  pages_fetch: Callable[[str, float], dict]) -> None:
    url = f"{config.site_url}{PAGES_SITE_JSON}?v={commit[:12]}"
    remote = pages_fetch(url, config.pages_timeout_seconds)
    if not isinstance(remote, dict):
        raise PublishError("Pages site.json 顶层不是对象")
    if remote.get("version") != site["version"]:
        raise PublishError(
            f"线上 site.json version 不一致（本地 {site['version']}，线上 {remote.get('version')!r}）"
        )
    if remote.get("archive_id") != site["archive_id"]:
        raise PublishError("线上 site.json archive_id 不一致")


def _record(config: PublishConfig, site: Mapping[str, Any] | None, *, status: str,
            commit: str | None, pushed: bool, verified: bool, changed: bool,
            entries: int | None = None, reason: str = "") -> dict:
    record: dict[str, Any] = {
        "status": status,
        "ok": status in {"success", "unchanged"},
        "changed": changed,
        "pushed": pushed,
        "verified": verified,
        "commit": commit,
        "repository": config.repository,
        "branch": config.branch,
        "site_url": config.site_url,
        "reason": reason,
        "enabled": config.enabled,
    }
    if site is not None:
        record.update(
            {
                "version": site["version"],
                "archive_id": site["archive_id"],
                "entries": site["entries"] if entries is None else entries,
                "objects_written": site["objects_written"],
                "objects_reused": site["objects_reused"],
            }
        )
        capacity = site.get("capacity")
        if isinstance(capacity, Mapping):
            record["capacity"] = _capacity_summary(capacity)
    return record


def _mark_verified(record: dict, commit: str, site: Mapping[str, Any]) -> None:
    """Persist the exact identity that a live Pages check just confirmed."""
    record["verified_commit"] = commit
    record["verified_version"] = site["version"]
    record["verified_archive_id"] = site["archive_id"]
    record["verified_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _carry_verified_identity(record: dict, previous: Mapping[str, Any] | None) -> None:
    """Keep the last confirmed identity across unrelated failures (history only)."""
    if not previous:
        return
    for key in ("verified_commit", "verified_version", "verified_archive_id", "verified_at"):
        if previous.get(key) and key not in record:
            record[key] = previous[key]


def _has_verified_credential(
    previous: Mapping[str, Any] | None, commit: str, site: Mapping[str, Any]
) -> bool:
    """A no-op may skip Pages only with a positive, precisely matching credential."""
    if not previous or previous.get("verified") is not True:
        return False
    return (
        previous.get("commit") == commit
        and previous.get("version") == site["version"]
        and previous.get("archive_id") == site["archive_id"]
    )


def publish_site(
    root: Path,
    config: PublishConfig | None = None,
    *,
    target: GitTarget | None = None,
    pages_fetch: Callable[[str, float], dict] | None = None,
    site_module=None,
) -> dict:
    """Build, commit, push and verify.  Overrides exist for tests only."""
    root = Path(root)
    if config is None:
        config = load_config(root)
    _assert_config_layout(config)
    _require_secret(config.key_file, "站点密钥")
    _require_secret(config.ssh_key_file, "SSH 部署密钥")
    _require_known_hosts(config)
    fetcher = pages_fetch or _fetch_pages_site_json
    site: dict | None = None
    with _publish_lock(config):
        try:
            entries = verify_entries(root)
            site = build_only(root, config, site_module)
            if target is None:
                target = production_target(config)
            else:  # test-injected transport still uses the configured identity
                target = replace(
                    target,
                    author_name=config.git_name,
                    author_email=config.git_email,
                )
            repo = _ensure_repo(config, target)
            tip, base = _fetch_base(repo, target, config.branch)
            if base is not None:
                _assert_managed_site(repo, base, site["archive_id"], target)
                _checkout_base(repo, base, config.branch, target)
            _sync_worktree(repo, config.workspace / "build", site["files"])
            staged = _staged_changes(repo, target)
            head = _rev_parse(repo, "HEAD", target)
            if staged:
                head = _commit(repo, target, site["version"])
            elif head is None:
                raise PublishError("没有可提交的发布内容")

            previous = read_state(config)
            if head == tip and not staged:
                _assert_head_tree(repo, head, site["files"], target)
                if _has_verified_credential(previous, head, site):
                    record = _record(
                        config, site, status="unchanged", commit=head,
                        pushed=False, verified=True, changed=site["changed"],
                        entries=entries, reason="构建与远端均无变化，沿用已核验上线凭证",
                    )
                    _carry_verified_identity(record, previous)
                    _write_state(config, record)
                    return _with_files(record, site)
                # No positive credential (missing/failed/pending/mismatched):
                # re-check Pages instead of claiming an unverified no-op.
                record = _record(
                    config, site, status="pushed_pending", commit=head,
                    pushed=False, verified=False, changed=site["changed"],
                    entries=entries,
                )
                _carry_verified_identity(record, previous)
                try:
                    _verify_pages(config, site, head, fetcher)
                except Exception as exc:  # noqa: BLE001 - never claim online success
                    record["reason"] = f"{type(exc).__name__}: {exc}"
                    _write_state(config, record)
                    return _with_files(record, site)
                record.update(status="success", ok=True, verified=True, reason="")
                _mark_verified(record, head, site)
                _write_state(config, record)
                return _with_files(record, site)

            _audit_push_history(repo, target, base, head, site["files"], site["archive_id"])
            current_tip = _remote_tip(repo, target, config.branch)
            if current_tip != tip:
                raise PublishError("远程分支在发布过程中被更新，已放弃推送（不会 force push）")
            _push(repo, target, config.branch, head)
            if _remote_tip(repo, target, config.branch) != head:
                raise PublishError("推送后远端 commit 与本地不一致")

            record = _record(
                config, site, status="pushed_pending", commit=head,
                pushed=True, verified=False, changed=site["changed"],
                entries=entries,
            )
            try:
                _verify_pages(config, site, head, fetcher)
            except Exception as exc:  # noqa: BLE001 - push succeeded, Pages not confirmed
                record["reason"] = f"{type(exc).__name__}: {exc}"
                _carry_verified_identity(record, previous)
                _write_state(config, record)
                return _with_files(record, site)
            record.update(status="success", ok=True, verified=True, reason="")
            _mark_verified(record, head, site)
            _write_state(config, record)
            return _with_files(record, site)
        except PublishError as exc:
            failure = _record(
                config, site, status="failed", commit=_safe_head(config),
                pushed=False, verified=False,
                changed=False if site is None else bool(site.get("changed")),
                reason=f"{type(exc).__name__}: {exc}",
            )
            failure["error"] = str(exc)
            _carry_verified_identity(failure, read_state(config))
            with contextlib.suppress(Exception):
                _write_state(config, failure)
            raise
        except Exception as exc:  # noqa: BLE001 - record unexpected failures too
            failure = _record(
                config, site, status="failed", commit=_safe_head(config),
                pushed=False, verified=False,
                changed=False if site is None else bool(site.get("changed")),
                reason=f"{type(exc).__name__}: {exc}",
            )
            failure["error"] = str(exc)
            _carry_verified_identity(failure, read_state(config))
            with contextlib.suppress(Exception):
                _write_state(config, failure)
            raise


def _safe_head(config: PublishConfig) -> str | None:
    repo = config.workspace / "repo"
    if not (repo / ".git").is_dir():
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _with_files(record: dict, site: Mapping[str, Any]) -> dict:
    return {**record, "files": list(site["files"])}


# ---------------------------------------------------------------------------
# Automatic entry point
# ---------------------------------------------------------------------------


def _skipped(reason: str, **extra: Any) -> dict:
    return {"status": "skipped", "reason": reason, **extra}


def maybe_publish(root: Path) -> dict:
    """Publish only when enabled; never raise into the indexing caller."""
    root = Path(root)
    if os.environ.get("CA_SKIP_AUTO_PUBLISH") == "1":
        return _skipped("CA_SKIP_AUTO_PUBLISH=1")
    config_path = root / "publish.yaml"
    if not config_path.is_file():
        return _skipped("config 未配置：publish.yaml 不存在")
    try:
        config = load_config(root)
    except ConfigError as exc:
        return {"status": "failed", "error": str(exc)}
    if not config.enabled:
        return _skipped("publish.yaml enabled=false")
    try:
        return publish_site(root, config)
    except Exception as exc:  # noqa: BLE001 - auto hook must never break indexing
        record = {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "ok": False,
        }
        with contextlib.suppress(Exception):
            _write_state(config, record)
        return record


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


PASSPHRASE_FLAGS = ("--passphrase", "--password", "--pass", "--secret", "--key-pass")


def _reject_passphrase_args(argv: list[str]) -> None:
    for token in argv:
        lowered = token.lower()
        if any(lowered == flag or lowered.startswith(flag + "=") for flag in PASSPHRASE_FLAGS):
            raise PublishError("拒绝在命令行传入口令；请在 TTY 中交互输入")


def _add_common(parser: argparse.ArgumentParser) -> None:
    default_root = Path(__file__).resolve().parents[1]
    parser.add_argument("--root", type=Path, default=default_root, help="归档根目录")
    parser.add_argument("--config", type=Path, default=None, help="publish.yaml 路径")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="publish_site.py",
        description="加密站点发布器（默认 disabled，不会自动上线）",
    )
    subs = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("init", "在 TTY 中设置口令并创建外置站点密钥"),
        ("build", "本地加密导出，不连接 GitHub"),
        ("status", "显示配置/密钥就绪情况与最近发布状态"),
        ("run", "构建、白名单校验、提交推送并核对 Pages 版本"),
    ):
        sub = subs.add_parser(name, help=help_text)
        _add_common(sub)
    return parser


def _print_json(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        _reject_passphrase_args(argv)
    except PublishError as exc:
        print(f"[publish] {exc}", file=sys.stderr)
        return 2
    parser = _build_parser()
    args = parser.parse_args(argv)
    root = Path(args.root)
    try:
        try:
            config = load_config(root, args.config)
        except ConfigError as exc:
            if args.command == "status":
                _print_json({
                    "status": "ok",
                    "configured": False,
                    "enabled": False,
                    "error": str(exc),
                    "capacity": _capacity_unavailable(f"publish.yaml 不可用：{exc}"),
                })
                return 0
            raise
        if args.command == "init":
            _print_json(init_key(root, config))
        elif args.command == "build":
            result = build_only(root, config, lock=True)
            result.pop("output_dir", None)
            _print_json(result)
        elif args.command == "status":
            _print_json(status(root, config))
        elif args.command == "run":
            result = publish_site(root, config)
            _print_json(result)
            # Distinct code: pushed but the online version is not confirmed yet.
            return 0 if result.get("status") in {"success", "unchanged"} else 3
        else:  # pragma: no cover - argparse enforces choices
            parser.error("未知命令")
        return 0
    except ConfigError as exc:
        _print_json({"status": "failed", "kind": "config", "error": str(exc)})
        return 2
    except (SecurityError, PublishError) as exc:
        _print_json({"status": "failed", "kind": "publish", "error": str(exc)})
        return 1
    except OSError as exc:
        _print_json({"status": "failed", "kind": "filesystem", "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
