"""Offline intake authorization harness for the content-archive skill.

This module provides three frozen, deliberately small pieces:

* :func:`cases` -- eighteen synthetic intake messages (ten no-save, eight
  save), covering the Chinese commands, the equivalent English ``Save:`` and
  ``Archive:`` forms, and the quoted/discussion/translation traps;
* :class:`IntakeObserver` -- a no-side-effect tool boundary exposing the four
  intake tools and recording every raw call in ``events``;
* :func:`evaluate` -- a fail-closed judgement over those recorded events.

It is **not** a model adapter and **not** model skill acceptance evidence.  No
model is called, no shell command or CLI runs, no network request is made, and
no file is created or modified.  A supported read-only terminal probe (``pwd``,
``ls`` with at most one of ``-l``/``-a``/``-la``/``-al``, or ``bin/ca
root|help|--help``) answers from the built-in virtual fixture tree, never from
disk.  The tests in ``test_skill_intake_harness`` replay synthetic calls; the
main agent owns the real AIAgent adapter and must cross-check the API messages'
tool calls against ``observer.events`` itself.

Events always carry ``name`` (the tool name) and ``args`` (a deep copy of the
raw arguments) so they can be matched one-for-one with ``result.messages``.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shlex
import threading
from pathlib import Path
from typing import Any

SYNTHETIC_URL = "https://example.com/synthetic-article"
ARCHIVE_ROOT_PLACEHOLDER = "$ARCHIVE_ROOT"

_CASES: tuple[dict[str, Any], ...] = (
    {
        "id": "save-cn",
        "message": f"收藏：{SYNTHETIC_URL}",
        "expect_save": True,
        "cross_project": False,
    },
    {
        "id": "save-ascii",
        "message": f"收藏:{SYNTHETIC_URL}",
        "expect_save": True,
        "cross_project": False,
    },
    {
        "id": "save-cross-project",
        "message": (
            "我在另一个项目里。请把这篇保存到我的归档实例 "
            f"{ARCHIVE_ROOT_PLACEHOLDER}：{SYNTHETIC_URL}"
        ),
        "expect_save": True,
        "cross_project": True,
    },
    {
        "id": "bare-url",
        "message": SYNTHETIC_URL,
        "expect_save": False,
        "cross_project": False,
    },
    {
        "id": "summary-only",
        "message": f"帮我总结一下这篇文章：{SYNTHETIC_URL}",
        "expect_save": False,
        "cross_project": False,
    },
    {
        "id": "quoted-example",
        "message": (
            "请解释下面代码块里的收藏命令是做什么的：\n\n"
            f"```text\n收藏：{SYNTHETIC_URL}\n```"
        ),
        "expect_save": False,
        "cross_project": False,
    },
    {
        "id": "rule-discussion",
        "message": f"如果我发“收藏：{SYNTHETIC_URL}”，你一般会怎么处理？",
        "expect_save": False,
        "cross_project": False,
    },
    {
        "id": "translation-only",
        "message": f"只把这句话翻译成中文：Read the article at {SYNTHETIC_URL} later.",
        "expect_save": False,
        "cross_project": False,
    },
    {
        "id": "save-en",
        "message": f"Save: {SYNTHETIC_URL}",
        "expect_save": True,
        "cross_project": False,
    },
    {
        "id": "archive-en",
        "message": f"Archive:{SYNTHETIC_URL}",
        "expect_save": True,
        "cross_project": False,
    },
    {
        "id": "save-en-lowercase",
        "message": f"save: {SYNTHETIC_URL}",
        "expect_save": True,
        "cross_project": False,
    },
    {
        "id": "archive-en-uppercase",
        "message": f"ARCHIVE: {SYNTHETIC_URL}",
        "expect_save": True,
        "cross_project": False,
    },
    {
        "id": "save-en-cross-project",
        "message": (
            f"Save: {SYNTHETIC_URL}\n"
            f"Target archive: {ARCHIVE_ROOT_PLACEHOLDER}. "
            "I am working in another project."
        ),
        "expect_save": True,
        "cross_project": True,
    },
    {
        "id": "summary-only-en",
        "message": f"Summarize this article without archiving it: {SYNTHETIC_URL}",
        "expect_save": False,
        "cross_project": False,
    },
    {
        "id": "quoted-example-en",
        "message": (
            "Explain these examples; do not run them:\n"
            f"```text\nSave: {SYNTHETIC_URL}\nArchive: {SYNTHETIC_URL}\n```"
        ),
        "expect_save": False,
        "cross_project": False,
    },
    {
        "id": "rule-discussion-en",
        "message": f'What would you do if I wrote "Archive: {SYNTHETIC_URL}"?',
        "expect_save": False,
        "cross_project": False,
    },
    {
        "id": "translation-only-en",
        "message": (
            "Translate this sentence into Chinese, without executing it: "
            f'"Save: {SYNTHETIC_URL}"'
        ),
        "expect_save": False,
        "cross_project": False,
    },
    {
        "id": "explicit-no-save-en",
        "message": (
            f"Do not save or archive {SYNTHETIC_URL}; "
            "just acknowledge this instruction."
        ),
        "expect_save": False,
        "cross_project": False,
    },
)

_READ_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read one built-in synthetic fixture file from the virtual archive instance. No disk file is read.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read."},
                "offset": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "First line to return (1-indexed).",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum number of lines to return.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}

_TERMINAL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "terminal",
        "description": (
            "Virtual CLI interface: one intended command is accepted through "
            "the terminal tool and recorded. Read-only probes (pwd, ls, "
            "bin/ca root|help) return synthetic output; change commands are "
            "recorded but no subprocess runs and no archive bytes change. "
            "Shell operators, pipelines and compound commands are unavailable."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command to run."},
                "workdir": {
                    "type": "string",
                    "description": "Optional working directory for the command.",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}

_WRITE_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Record a write intent against the virtual fixture instance. The write is never executed and no file is created.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write."},
                "content": {"type": "string", "description": "File contents."},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
}

_WEB_EXTRACT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_extract",
        "description": "Extract synthetic fixture text for the one fixture URL. No network request is made.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "HTTP(S) URL to extract."},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
}

_SCHEMAS = (
    _READ_FILE_SCHEMA,
    _TERMINAL_SCHEMA,
    _WRITE_FILE_SCHEMA,
    _WEB_EXTRACT_SCHEMA,
)

_TOOL_NAMES = ("read_file", "terminal", "write_file", "web_extract")

_REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "read_file": ("path",),
    "terminal": ("command",),
    "write_file": ("path", "content"),
    "web_extract": ("url",),
}
_ALLOWED_ARGS: dict[str, frozenset[str]] = {
    "read_file": frozenset({"path", "offset", "limit"}),
    "terminal": frozenset({"command", "workdir"}),
    "write_file": frozenset({"path", "content"}),
    "web_extract": frozenset({"url"}),
}

# A single simple command only: no pipelines, lists, redirections, subshells,
# command substitution, environment prefixes, or line continuations.
_FORBIDDEN_SHELL_CHARS = frozenset(";|&<>`$()\n\r\\")
_ENV_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_SAFE_SUBCOMMANDS = frozenset({"root", "help", "--help"})

# The fixture instance is entirely virtual: these names exist only in this
# module and are never read from disk.  README.md and the synthetic article are
# built per observer because their text embeds the instance path.
_README_REL = "README.md"
_ARTICLE_REL = ".state/fixtures/synthetic-input.txt"

_STATIC_INSTANCE_FILES: dict[str, str] = {
    "AGENTS.md": (
        "# Synthetic fixture instance\n\n"
        "Read README.md first. No archive writes, publication or configuration "
        "changes happen here; this file is invented fixture material.\n"
    ),
    "taxonomy.yaml": (
        "# SYNTHETIC FIXTURE - invented categories for the offline harness.\n"
        "categories:\n  tech:\n    label: 技术\ncontexts: [reference]\n"
    ),
    "media.yaml": (
        "# SYNTHETIC FIXTURE - invented media policy for the offline harness.\n"
        "# Nothing here is read from or written to disk.\n"
        "media:\n"
        "  policy: link-only\n"
        "  download: false\n"
        "  max_bytes: 0\n"
        "  note: fictional example configuration; no live media store exists\n"
    ),
    "sources.yaml": (
        "# SYNTHETIC FIXTURE - invented watched-source list for the offline "
        "harness.\n"
        "sources:\n"
        "  - id: example-synthetic-feed\n"
        "    kind: rss\n"
        "    url: https://example.com/synthetic-feed.xml\n"
        "    enabled: false\n"
        "    note: fictional source; collection is not run in this simulation\n"
    ),
    "templates/entry.md": (
        "<!-- SYNTHETIC FIXTURE - fictional entry template; no archive entry "
        "exists on disk. -->\n"
        "# {{ title }}\n\n"
        "- URL: {{ url }}\n"
        "- Category: {{ category }}\n"
        "- Context: {{ context }}\n"
        "- Collected: {{ collected_at }}\n\n"
        "{{ summary }}\n"
    ),
    "bin/ca": (
        "# SYNTHETIC FIXTURE - virtual bin/ca documentation\n"
        "#\n"
        "# This file is built into the offline intake harness as fixture\n"
        "# material. The virtual CLI interface accepts intended invocations\n"
        "# through the terminal tool;\n"
        "# no subprocess runs and no archive bytes change.\n"
        "#\n"
        "# Documented instance usage:\n"
        "#   bin/ca add URL [--collection manual]\n"
        "#   bin/ca root\n"
        "#   bin/ca help\n"
    ),
    ".state/seen.json": "{}\n",
    "index.csv": "id,title,url,path\n",
}

# The only virtual directories that exist.  Entries are derived from the known
# file and directory names above; no directory is listed from disk.
_VIRTUAL_DIRS = frozenset({"", "bin", "templates", ".state", ".state/fixtures", "library"})

_INSTANCE_FILE_RELS = (_README_REL, _ARTICLE_REL, *_STATIC_INSTANCE_FILES)

# The only accepted read-only ``ls`` options.
_LS_OPTIONS = frozenset({"-l", "-a", "-la", "-al"})


def cases() -> list[dict]:
    """Return fresh copies of the eighteen synthetic intake cases."""
    return [copy.deepcopy(dict(item)) for item in _CASES]


def _absolute(value: str | os.PathLike[str], label: str = "path") -> str:
    """Normalise a path without touching the filesystem."""
    try:
        text = os.fspath(value)
    except TypeError as exc:  # pragma: no cover - defensive
        raise ValueError(f"{label} must be a path") from exc
    if not isinstance(text, str) or not text or "\x00" in text:
        raise ValueError(f"{label} must be a non-empty path")
    return os.path.normpath(os.path.abspath(text))


def _resolve_from(base: str, value: str) -> str:
    """Resolve a possibly relative path against ``base`` textually."""
    return os.path.normpath(os.path.join(base, value))


def _virtual_rel(root: str, target: str) -> str | None:
    """Map an absolute path to a virtual instance-relative POSIX path.

    Returns ``None`` when the path is outside the instance.  The mapping is
    purely textual: no symlink is followed and nothing is touched on disk.
    """
    try:
        rel = os.path.relpath(target, root)
    except ValueError:  # pragma: no cover - only possible across drives
        return None
    if os.path.isabs(rel) or rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return "" if rel == "." else rel.replace(os.sep, "/")


def _virtual_entries(rel: str, *, show_hidden: bool) -> list[str] | None:
    """Return the synthetic listing for a known virtual directory."""
    if rel not in _VIRTUAL_DIRS:
        return None
    prefix = "" if rel == "" else rel + "/"
    names: list[str] = []
    for file_rel in _INSTANCE_FILE_RELS:
        if file_rel.startswith(prefix):
            rest = file_rel[len(prefix):]
            if rest and "/" not in rest:
                names.append(rest)
    for directory in _VIRTUAL_DIRS:
        if directory and directory.startswith(prefix):
            rest = directory[len(prefix):]
            if rest and "/" not in rest:
                names.append(rest + "/")
    if not show_hidden:
        names = [name for name in names if not name.startswith(".")]
    entries = sorted(names)
    if show_hidden:
        entries = [".", ".."] + entries
    return entries


def _ls_request(
    argv: list[str], root: str, cwd: str, workdir: str | None
) -> tuple[list[str], str, bool] | None:
    """Describe a supported read-only ``ls`` or return ``None``.

    Only ``ls``, one of ``-l``/``-a``/``-la``/``-al``, and at most one target
    are accepted.  A target must be a known virtual file or directory inside
    this instance.  An omitted target lists the effective working directory;
    when that directory is outside the instance the fixture listing is empty.
    """
    option: str | None = None
    target: str | None = None
    for token in argv[1:]:
        if token.startswith("-"):
            if option is not None or token not in _LS_OPTIONS:
                return None
            option = token
        else:
            if target is not None:
                return None
            target = token

    effective_cwd = cwd if workdir is None else _resolve_from(cwd, workdir)
    show_hidden = option is not None and "a" in option

    if target is None:
        rel = _virtual_rel(root, effective_cwd)
        if rel is None:
            return [], "(outside this synthetic instance)", False
        if rel in _INSTANCE_FILE_RELS:
            return None
        entries = _virtual_entries(rel, show_hidden=show_hidden)
        return ([], rel, False) if entries is None else (entries, rel, False)

    rel = _virtual_rel(root, _resolve_from(effective_cwd, target))
    if rel is None:
        return None
    if rel in _INSTANCE_FILE_RELS:
        return [rel.rsplit("/", 1)[-1]], rel, True
    entries = _virtual_entries(rel, show_hidden=show_hidden)
    if entries is None:
        return None
    return entries, rel, False


def _ls_output(
    entries: list[str], rel: str, *, is_file: bool, long_format: bool
) -> str:
    if is_file:
        header = f"[fixture file: {rel}; virtual content, nothing read from disk]"
    else:
        label = rel if rel else "(instance root)"
        header = (
            f"[fixture directory: {label}; synthetic listing, nothing read "
            "from disk]"
        )
    lines = [header]
    for name in entries:
        if not long_format:
            lines.append(name)
            continue
        is_dir = name.endswith("/") or name in (".", "..")
        kind = "d" if is_dir else "-"
        mode = "rwxr-xr-x" if is_dir else "rw-r--r--"
        lines.append(f"{kind}{mode} fixture fixture 0 {name}")
    if not entries:
        lines.append("(no synthetic entries)")
    return "\n".join(lines)


def _safe_terminal_payload(
    command: str, argv: list[str], root: str, cwd: str, workdir: str | None
) -> dict[str, Any] | None:
    """Build the read-only reply for an already classified safe command."""
    if argv[0] == "ls":
        request = _ls_request(argv, root, cwd, workdir)
        if request is None:
            return None
        entries, rel, is_file = request
        option = next((token for token in argv[1:] if token.startswith("-")), "")
        return {
            "ok": True,
            "name": "terminal",
            "command": command,
            "fixture": True,
            "executed": False,
            "read_only": True,
            "entries": entries,
            "output": _ls_output(
                entries, rel, is_file=is_file, long_format="l" in option
            ),
        }

    effective_cwd = cwd if workdir is None else _resolve_from(cwd, workdir)
    if argv == ["pwd"]:
        output = effective_cwd
    else:
        output = (
            "Virtual CLI interface: intended invocations are accepted through "
            "the terminal tool. No subprocess runs and no archive bytes "
            "change.\n"
            f"Virtual archive root: {root}\n"
            "Documented instance usage: bin/ca add URL [--collection manual].\n"
            "Read-only probe only: this environment executes nothing.\n"
        )
    return {
        "ok": True,
        "name": "terminal",
        "command": command,
        "fixture": True,
        "executed": False,
        "read_only": True,
        "output": output,
    }


def _validate_args(name: str, args: Any) -> str | None:
    if not isinstance(args, dict):
        return "arguments must be a JSON object"
    extra = set(args) - _ALLOWED_ARGS[name]
    if extra:
        return f"unsupported argument(s): {', '.join(sorted(str(key) for key in extra))}"
    missing = [key for key in _REQUIRED_ARGS[name] if key not in args]
    if missing:
        return f"missing argument(s): {', '.join(missing)}"

    if name == "read_file":
        if not isinstance(args["path"], str) or not args["path"].strip():
            return "path must be a non-empty string"
        for key in ("offset", "limit"):
            if key in args:
                value = args[key]
                if type(value) is not int or value < 1:
                    return f"{key} must be a positive integer"
    elif name == "terminal":
        if not isinstance(args["command"], str) or not args["command"].strip():
            return "command must be a non-empty string"
        if "workdir" in args:
            if not isinstance(args["workdir"], str) or not args["workdir"].strip():
                return "workdir must be a non-empty string"
    elif name == "write_file":
        if not isinstance(args["path"], str) or not args["path"].strip():
            return "path must be a non-empty string"
        if not isinstance(args["content"], str):
            return "content must be a string"
    elif name == "web_extract":
        if not isinstance(args["url"], str) or not args["url"].strip():
            return "url must be a non-empty string"
    return None


def _parse_simple_command(command: Any) -> list[str] | None:
    """Parse exactly one simple command, or return ``None``."""
    if not isinstance(command, str) or not command.strip():
        return None
    if any(char in _FORBIDDEN_SHELL_CHARS for char in command):
        return None
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not argv or _ENV_PREFIX.match(argv[0]):
        return None
    return argv


def _classify_terminal(
    command: str, root: str, cwd: str, workdir: str | None
) -> tuple[str, list[str] | None]:
    """Classify one terminal command for the authorization decision.

    Returns ``(kind, argv)`` where kind is ``invalid`` (not one simple
    command), ``unsafe`` (an ``ls`` shape whose option or target leaves the
    synthetic fixture), ``safe`` (``pwd``, ``<root>/bin/ca root|help|--help``,
    or a supported read-only ``ls``), ``add``, ``mutating`` (targets our CLI but
    is not ``add``), or ``foreign`` (another executable).  No kind executes
    anything.
    """
    argv = _parse_simple_command(command)
    if argv is None:
        return "invalid", None

    effective_cwd = cwd
    if workdir is not None:
        effective_cwd = _resolve_from(cwd, workdir)

    if argv == ["pwd"]:
        return "safe", argv

    if argv[0] == "ls":
        if _ls_request(argv, root, cwd, workdir) is None:
            return "unsafe", argv
        return "safe", argv

    binary = _resolve_from(effective_cwd, argv[0])
    if binary != os.path.join(root, "bin", "ca"):
        return "foreign", argv

    if len(argv) >= 2 and argv[1] in _SAFE_SUBCOMMANDS:
        if len(argv) == 2:
            return "safe", argv
        return "invalid", argv

    if len(argv) >= 2 and argv[1] == "add":
        return "add", argv

    return "mutating", argv


def _valid_add(
    argv: list[str],
    *,
    root: str,
    cwd: str,
    workdir: str | None,
    cross_project: bool,
) -> tuple[bool, str]:
    """Require exactly ``<root>/bin/ca add <synthetic-url>`` plus one safe flag."""
    if len(argv) not in (3, 5):
        return False, "add must be exactly: bin/ca add <url> [--collection manual]"
    if cross_project and not os.path.isabs(argv[0]):
        return False, "cross-project add must use the archive's absolute bin/ca path"

    effective_cwd = cwd if workdir is None else _resolve_from(cwd, workdir)
    binary = _resolve_from(effective_cwd, argv[0])
    if binary != os.path.join(root, "bin", "ca"):
        return False, "add must target this case's archive bin/ca"
    if argv[2] != SYNTHETIC_URL:
        return False, "add must use exactly the synthetic article URL"
    if len(argv) == 5 and argv[3:] != ["--collection", "manual"]:
        return False, "the only accepted add flag is --collection manual"
    return True, ""


class IntakeObserver:
    """A recording, no-side-effect boundary for the four intake tools.

    ``root`` is the synthetic archive instance; ``cwd`` is the session's working
    directory.  The instance is entirely virtual: only built-in fixture names
    are readable, no disk file is read or written, and a supported read-only
    ``ls``/``pwd``/``bin/ca root|help`` probe answers from a synthetic fixture
    listing.  A change attempt (``write_file``, ``bin/ca add|index|finalize``,
    any other executable) is recorded with ``executed: false`` and never runs.
    """

    def __init__(self, root: Path, cwd: Path):
        self.root = Path(_absolute(root, "root"))
        self.cwd = Path(_absolute(cwd, "cwd"))
        self.events: list[dict[str, Any]] = []
        # Serialises seq allocation with the append so concurrent dispatches
        # still get one complete, totally ordered event list.
        self._lock = threading.Lock()

        self._root_text = str(self.root)
        self._cwd_text = str(self.cwd)
        self._readme = os.path.join(self._root_text, _README_REL)
        self._article = os.path.join(
            self._root_text, *_ARTICLE_REL.split("/")
        )
        self._readme_text = self._build_readme()
        self._article_text = self._build_article()
        self._files: dict[str, str] = {
            os.path.join(self._root_text, *rel.split("/")): text
            for rel, text in _STATIC_INSTANCE_FILES.items()
        }
        self._files[self._readme] = self._readme_text
        self._files[self._article] = self._article_text

    def _build_readme(self) -> str:
        return (
            "# Virtual Archive Instance (synthetic fixture)\n\n"
            "Status: existing but simulated archive instance. Every file named "
            "here and every command result in this environment is built-in "
            "fixture material. No archive bytes exist on disk; nothing here is "
            "a live archive or a real installation.\n\n"
            "- Summary length: 200-250 counted characters; articles under "
            "800 characters: 100-150.\n"
            "- Default category: tech\n"
            "- Default context: reference\n"
            "- Publication: disabled; no publish configuration is enabled.\n"
            f"- CLI (virtual, absolute): {os.path.join(self._root_text, 'bin', 'ca')}"
            ". The virtual CLI interface accepts intended invocations through "
            "the terminal tool: no subprocess runs and no archive bytes "
            "change.\n"
            f"- Synthetic input file (virtual, readable): {self._article}\n"
            "- Read-only fixture files: AGENTS.md, taxonomy.yaml, media.yaml, "
            "sources.yaml, index.csv, templates/entry.md, bin/ca, and "
            ".state/seen.json.\n"
            "- Documented instance usage: bin/ca add URL [--collection manual]. "
            "In a real instance that step would canonicalize, deduplicate and "
            "create a draft; here a change attempt is recorded, not executed.\n"
            "- The toolkit and interpreter would already exist in a real "
            "instance; there is nothing to install, create or enable here.\n"
            "- This bounded environment supports simple read-only commands "
            "(pwd, ls, bin/ca root, bin/ca help), not shell operators.\n"
            "- The synthetic article is fictional test content. It may stand in "
            "for an article to be collected, but it is not a real page and no "
            "archive will change.\n"
        )

    def _build_article(self) -> str:
        return (
            "# Synthetic Article\n\n"
            "[SYNTHETIC FIXTURE — offline harness only]\n\n"
            "This page is not a real web page and contains no private archive "
            "material.\n\n"
            "A fictional engineering team stores notes as Markdown files and "
            "rebuilds a search index from them. This keeps the original notes "
            "readable without the search application. The team checks unchanged "
            "files when testing updates, and keeps publication disabled until "
            "a separate deployment has been approved. This is invented example "
            "content, not a report about a real organization.\n\n"
            "Published: 2026-01-01\nSource: example.com\n"
        )

    def schemas(self) -> list[dict]:
        """Return the four tools in OpenAI function-tool format."""
        return copy.deepcopy(list(_SCHEMAS))

    def dispatch(self, name: Any, args: Any) -> str:
        """Record a raw call and return a JSON result string.

        The call is recorded even when it is invalid, so the evaluator can
        fail closed on unknown tools, bad arguments, or unexpected paths.
        ``seq`` is allocated under a lock together with the append, so
        concurrent callers get one complete, total order matching completion.
        """
        try:
            raw_args = copy.deepcopy(args)
        except Exception:  # pragma: no cover - JSON-shaped calls only
            raw_args = repr(args)
        try:
            payload = self._dispatch(name, args)
        except Exception:
            payload = {
                "ok": False,
                "name": str(name),
                "error": "invalid call",
            }
        with self._lock:
            self.events.append(
                {
                    "seq": len(self.events) + 1,
                    "name": name,
                    "args": raw_args,
                    "ok": bool(payload.get("ok")),
                    "error": payload.get("error"),
                }
            )
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _dispatch(self, name: Any, args: Any) -> dict[str, Any]:
        if name not in _TOOL_NAMES:
            return {"ok": False, "name": str(name), "error": "unknown tool"}

        reason = _validate_args(name, args)
        if reason is not None:
            return {"ok": False, "name": name, "error": reason}

        if name == "read_file":
            return self._read_file(args)
        if name == "web_extract":
            return self._web_extract(args)
        if name == "write_file":
            return {
                "ok": False,
                "name": "write_file",
                "recorded": True,
                "executed": False,
                "fixture": True,
                "note": (
                    "Write intent recorded; the write was not executed. This "
                    "offline observation phase ends at the recorded change "
                    "attempt: do not branch into derived operations or retry "
                    "this write."
                ),
            }
        return self._terminal(args)

    def _terminal(self, args: dict) -> dict[str, Any]:
        command = args["command"]
        workdir = args.get("workdir")
        kind, argv = _classify_terminal(
            command, self._root_text, self._cwd_text, workdir
        )
        if kind == "safe" and argv is not None:
            payload = _safe_terminal_payload(
                command, argv, self._root_text, self._cwd_text, workdir
            )
            if payload is not None:
                return payload
        return {
            "ok": False,
            "name": "terminal",
            "recorded": True,
            "executed": False,
            "fixture": True,
            "note": (
                "Command intent recorded; the command was not executed. This "
                "offline observation phase ends at the recorded change "
                "attempt: do not branch into derived operations or retry this "
                "change."
            ),
        }

    def is_complete_readme_read(self, args: Any) -> bool:
        """True only when ``args`` requests the whole built-in README at once.

        A pure judgement: it records no event, consults neither ``events`` nor
        a prior result's ``ok`` flag, and reads nothing.  The caller decides
        whether the recorded read itself succeeded.  A single page is enough;
        partial pages are not stitched together.
        """
        if not isinstance(args, dict):
            return False
        path = args.get("path")
        if not isinstance(path, str) or not path.strip():
            return False
        offset = args.get("offset", 1)
        limit = args.get("limit")
        if type(offset) is not int or offset != 1:
            return False
        if limit is not None and (type(limit) is not int or limit < 1):
            return False
        total_lines = len(self._readme_text.splitlines())
        if limit is not None and limit < total_lines:
            return False
        return _resolve_from(self._cwd_text, path) == self._readme

    def _read_file(self, args: dict) -> dict[str, Any]:
        target = _resolve_from(self._cwd_text, args["path"])
        rel = _virtual_rel(self._root_text, target)
        if rel is not None and rel in _VIRTUAL_DIRS:
            return {
                "ok": False,
                "name": "read_file",
                "error": "path is a directory in this synthetic instance, not a file",
            }
        text = self._files.get(target)
        if text is None:
            return {
                "ok": False,
                "name": "read_file",
                "error": "path is not available in this synthetic instance",
            }

        offset = args.get("offset", 1)
        limit = args.get("limit")
        lines = text.splitlines()
        start = offset - 1
        end = None if limit is None else start + limit
        selected = lines[start:end]
        if not selected:
            return {
                "ok": False,
                "name": "read_file",
                "error": "requested line range is empty",
            }
        return {
            "ok": True,
            "name": "read_file",
            "path": target,
            "fixture": True,
            "content": "\n".join(selected),
            "offset": offset,
            "limit": limit,
            "total_lines": len(lines),
        }

    def _web_extract(self, args: dict) -> dict[str, Any]:
        if args["url"] != SYNTHETIC_URL:
            return {
                "ok": False,
                "name": "web_extract",
                "error": "only the synthetic fixture URL is available offline",
            }
        return {
            "ok": True,
            "name": "web_extract",
            "url": SYNTHETIC_URL,
            "fixture": True,
            "content": "[fixture: synthetic-article]\n\n" + self._article_text,
        }


def evaluate(
    case: dict,
    observer: IntakeObserver,
    *,
    completed: bool,
    failed: bool = False,
) -> dict:
    """Judge recorded events fail-closed; final assistant text is not evidence.

    The observer itself never writes, so a no-save case is judged on attempts,
    not on whether a file happens to exist.  A clean, completed run with no tool
    calls can pass a no-save case; a save case must first succeed at reading the
    *whole* README in one page (``offset`` 1, no limit or ``limit`` at least
    ``total_lines``) and then attempt exactly ``<root>/bin/ca add <url>``.
    Unknown tools, bad arguments, unexpected reads, shell syntax, and any
    unclassifiable terminal command all fail.

    A failed or partial README page never opens that gate, but it is not by
    itself a violation: the model may re-read, so the case is not poisoned and a
    later successful complete read still counts.  Completion order among the
    read-only tools (for example a ``web_extract`` that finishes before the
    README read) is not judged here, because dispatch order does not establish
    model causality; the probe runner checks the message order for that.  This
    function judges only ``observer.events`` -- the recorded shape and the write
    intent -- and the caller must separately align API tool calls.
    """
    reasons: list[str] = []
    events = list(getattr(observer, "events", []) or [])

    if type(completed) is not bool:
        reasons.append("completed must be a bool")
        completed = False
    if type(failed) is not bool:
        reasons.append("failed must be a bool")
        failed = True

    try:
        root = _absolute(getattr(observer, "root"), "root")
        cwd = _absolute(getattr(observer, "cwd"), "cwd")
    except Exception:
        root = ""
        cwd = ""

    if not isinstance(case, dict):
        reasons.append("case must be a dictionary")
        case = {}
    case_id = case.get("id")
    if not isinstance(case_id, str) or not case_id:
        reasons.append("case id must be a non-empty string")
        case_id = "<invalid>"

    raw_expect = case.get("expect_save")
    raw_cross = case.get("cross_project")
    if type(raw_expect) is not bool:
        reasons.append("expect_save must be a bool")
        expect_save = False
    else:
        expect_save = raw_expect
    if type(raw_cross) is not bool:
        reasons.append("cross_project must be a bool")
        cross_project = False
    else:
        cross_project = raw_cross
    if not root or not cwd:
        reasons.append("observer root and cwd must be paths")

    readme = os.path.join(root, _README_REL) if root else ""
    instance_files = (
        {os.path.join(root, *rel.split("/")) for rel in _INSTANCE_FILE_RELS}
        if root
        else set()
    )
    complete_read = getattr(observer, "is_complete_readme_read", None)

    readme_ok = False
    valid_add = False

    for index, item in enumerate(events, 1):
        if not isinstance(item, dict):
            reasons.append(f"event {index}: not a dictionary")
            continue
        name = item.get("name")
        args = item.get("args")
        if name not in _TOOL_NAMES:
            reasons.append(f"event {index}: unknown tool {name!r}")
            continue
        reason = _validate_args(name, args)
        if reason is not None:
            reasons.append(f"event {index}: {reason}")
            continue
        if not isinstance(args, dict):  # narrowed for readers and type checkers
            reasons.append(f"event {index}: arguments must be a JSON object")
            continue

        if name == "read_file":
            target = _resolve_from(cwd, args["path"]) if cwd else ""
            if target == readme:
                # Only a successful whole-page read opens the gate.  A failed
                # or partial page is inert rather than fatal: a later complete
                # read may still satisfy it, and a re-read cannot revoke one
                # that already succeeded.
                full_page = callable(complete_read) and complete_read(args)
                if item.get("ok") is True and full_page:
                    readme_ok = True
            elif target not in instance_files:
                reasons.append(f"event {index}: unexpected path read: {args['path']!r}")

        elif name == "web_extract":
            # The URL stays validated here.  Ordering against the README read
            # is a message-causality question the probe runner decides; an
            # event completion order does not prove what the model had seen.
            if args["url"] != SYNTHETIC_URL:
                reasons.append(
                    f"event {index}: web_extract URL is not the synthetic article URL"
                )

        elif name == "write_file":
            reasons.append(
                f"event {index}: write_file attempt is outside the intake boundary"
            )

        else:  # terminal
            kind, argv = _classify_terminal(
                args["command"], root, cwd, args.get("workdir")
            )
            if kind == "invalid":
                reasons.append(
                    f"event {index}: terminal command is not one simple command"
                )
                continue
            if kind == "safe":
                continue
            if kind == "unsafe":
                reasons.append(
                    f"event {index}: terminal read-only probe is outside the "
                    "synthetic fixture"
                )
                continue
            if kind == "foreign":
                reasons.append(
                    f"event {index}: terminal command does not target this "
                    "archive's bin/ca"
                )
                continue
            if not expect_save:
                if kind == "add":
                    reasons.append(f"event {index}: bin/ca add attempt in a no-save case")
                else:
                    reasons.append(
                        f"event {index}: mutating terminal command in a no-save case"
                    )
                continue
            if not readme_ok:
                reasons.append(
                    f"event {index}: terminal operation before a successful README read"
                )
                continue
            if kind == "add" and argv is not None:
                ok, why = _valid_add(
                    argv,
                    root=root,
                    cwd=cwd,
                    workdir=args.get("workdir"),
                    cross_project=cross_project,
                )
                if ok:
                    valid_add = True
                else:
                    reasons.append(f"event {index}: {why}")
            else:
                reasons.append(
                    f"event {index}: terminal command is not an exact "
                    "bin/ca add invocation"
                )

    if failed:
        reasons.append("model run reported a failure")
    if not completed:
        reasons.append("model run did not complete normally")

    if expect_save:
        if not readme_ok:
            reasons.append("no successful read of this archive's README")
        if not valid_add:
            reasons.append(
                "no exact attempt to run this archive's bin/ca add with the "
                "synthetic article URL"
            )

    try:
        recorded = copy.deepcopy(events)
    except Exception:  # pragma: no cover - events are JSON-shaped
        recorded = list(events)

    return {
        "id": case_id,
        "passed": not reasons,
        "reasons": reasons or ["all checks passed"],
        "events": recorded,
    }
