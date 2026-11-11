"""Opt-in Hermes intake probe. Importing this module never loads Hermes or credentials.

The offline suite tests the evidence checks. A live probe additionally needs a
Hermes checkout/interpreter and an explicitly selected, configured API provider.
No model-controlled command is executed; see tests/README.md for limitations.
"""
from __future__ import annotations

import json
import shlex
from collections import Counter
from collections.abc import Mapping
from importlib import import_module
from pathlib import Path


def child_environment(home: Path, source: Mapping[str, str]) -> dict[str, str]:
    """Do not inherit the active profile, injected import paths, or credentials."""
    allowed = ("PATH", "LANG", "LC_ALL", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
               "http_proxy", "https_proxy", "no_proxy", "SSL_CERT_FILE")
    env = {key: source[key] for key in allowed if key in source}
    env.update(HOME=str(home), HERMES_HOME=str(home),
               HERMES_IGNORE_RULES="1", HERMES_IGNORE_USER_CONFIG="1",
               PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(home))
    return env


def audit_result(result: dict, events: list[dict], provider: str, model: str, *,
                 require_readme_for_extract: bool = False) -> list[str]:
    """Cross-check raw model calls against the bounded handlers, failing closed.

    ``require_readme_for_extract`` is a scorer-only switch: the worker sets it
    from the case's own ``expect_save`` expectation, it is never placed in the
    system message, user message or tool schema, and it only tightens the
    judgement.  When set, a positive case's ``web_extract`` must follow a
    complete README tool reply in a *prior* assistant turn, exactly like the
    intake call; the observer cannot see that because event completion order is
    not model causality.
    """
    reasons = []
    for field, expected in (("completed", True), ("failed", False),
                            ("partial", False), ("interrupted", False)):
        if result.get(field) is not expected:
            reasons.append(f"invalid runtime status: {field}")
    if type(result.get("api_calls")) is not int or result["api_calls"] < 1:
        reasons.append("no completed model API call")
    if result.get("provider") != provider or result.get("model") != model:
        reasons.append("runtime provider/model differs from requested route")
    calls = []
    try:
        messages = result["messages"]
        if not isinstance(messages, list):
            raise ValueError("messages must be a list")
        pending_readmes = set()
        readme_delivered = False
        for message in messages:
            # Dispatch completion order is not model causality. A same-turn
            # README + intake/extract batch has not given the model the
            # prerequisite text, whichever tool reply is recorded first.
            if message.get("role") == "tool" and message.get("tool_call_id") in pending_readmes:
                reply = json.loads(message["content"])
                total = reply.get("total_lines")
                if (reply.get("ok") is True and reply.get("name") == "read_file"
                        and Path(reply.get("path", "")).name == "README.md"
                        and type(reply.get("offset")) is int and reply["offset"] == 1
                        and type(total) is int and total > 0
                        and isinstance(reply.get("content"), str)
                        and len(reply["content"].splitlines()) == total):
                    readme_delivered = True
            for call in message.get("tool_calls", []):
                if message.get("role") != "assistant":
                    raise ValueError("tool calls must originate in an assistant turn")
                function = call["function"]
                args = json.loads(function["arguments"])
                if not isinstance(args, dict):
                    raise ValueError("arguments must be an object")
                if (function["name"] == "read_file"
                        and Path(args.get("path", "")).name == "README.md"
                        and isinstance(call.get("id"), str)):
                    pending_readmes.add(call["id"])
                if function["name"] == "terminal":
                    argv = shlex.split(args.get("command", ""))
                    if len(argv) > 1 and argv[1] == "add" and not readme_delivered:
                        reasons.append("intake requested before a complete README reply in a prior model turn")
                if (function["name"] == "web_extract" and require_readme_for_extract
                        and not readme_delivered):
                    reasons.append("extract requested before a complete README reply in a prior model turn")
                calls.append({"name": function["name"], "args": args})
        observed = [{"name": e["name"], "args": e["args"]} for e in events]
        def counted(items):
            return Counter(json.dumps(item, sort_keys=True, allow_nan=False) for item in items)
        if counted(calls) != counted(observed):
            reasons.append("raw tool calls do not exactly match observation counts/arguments")
    except (KeyError, TypeError, ValueError, AttributeError):
        reasons.append("malformed runtime tool-call evidence")
    return reasons


def _worker(payload: dict) -> dict:
    """Run only in a fresh subprocess with a scrubbed environment and scratch cwd."""
    import hashlib
    import logging
    import os
    import sys

    from skill_intake_harness import IntakeObserver, evaluate

    logging.disable(logging.CRITICAL)
    home = Path(os.environ["HERMES_HOME"])
    source = Path(payload["hermes_root"])
    if (source / ".env").exists():
        raise RuntimeError("Hermes source dotenv is not allowed in an isolated probe")
    sys.path.insert(0, str(source))
    import_module("hermes_constants").set_hermes_home_override(home)
    AIAgent = import_module("run_agent").AIAgent
    registry = import_module("tools.registry").registry
    inline_executors = import_module("agent.inline_tool_executors").INLINE_TOOL_EXECUTORS

    root, cwd = Path(payload["root"]), Path(payload["cwd"])
    observer = IntakeObserver(root, cwd)
    # Process-local replacement, never a live profile/installation modification.
    # Inline handlers bypass the registry; remove those as well.
    inline_executors.clear()
    for name in registry.get_all_tool_names():
        registry.deregister(name)
        registry.deregister(name, scope=registry.current_scope_key())
    toolsets = {"read_file": "file", "write_file": "file",
                "terminal": "terminal", "web_extract": "web"}
    handlers = {}
    for schema in observer.schemas():
        fn = schema["function"]
        name = fn["name"]
        handlers[name] = lambda args, _name=name, **kw: observer.dispatch(_name, args)
        registry.register(
            name=name, toolset=toolsets[name], schema=fn,
            handler=handlers[name],
            check_fn=lambda: True,
        )

    def verify_dispatch():
        if inline_executors or set(registry.get_all_tool_names()) != set(toolsets):
            raise RuntimeError("unexpected tool registration")
        for name, handler in handlers.items():
            entry = registry.get_entry(name)
            if entry is None or entry.handler is not handler:
                raise RuntimeError("bounded tool handler replaced")

    def inventory(tree):
        result = {}
        for path in tree.rglob("*"):
            if path.is_symlink():
                raise RuntimeError("unexpected fixture symlink")
            if path.is_file():
                result[path.relative_to(tree).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    base = home.parent
    if root.parent != base or (cwd != base and base not in cwd.parents):
        raise RuntimeError("fixture paths do not share an isolated base")
    # Warm idempotent framework housekeeping in this fresh, empty home before
    # the monitored phase. Keep its exact files in the baseline, not an ignore
    # list: later writes to these files must still fail the inventory check.
    import_module("hermes_logging").setup_logging(hermes_home=home)
    import_module("hermes_constants").get_scratch_dir(home=home)
    verify_dispatch()
    before, archive_before = inventory(base), inventory(root)
    agent = AIAgent(
        **payload["runtime"], model=payload["model"],
        enabled_toolsets=["file", "terminal", "web"],
        disabled_toolsets=["delegation", "memory", "skills", "todo", "clarify"],
        max_iterations=8, max_tokens=2048, run_budget_seconds=240,
        quiet_mode=True, save_trajectories=False, verbose_logging=False,
        skip_context_files=True, load_soul_identity=False, skip_memory=True,
        skip_background_review=True, session_db=None, fallback_model={},
        credential_pool=None, checkpoints_enabled=False, cwd=str(cwd),
    )
    try:
        verify_dispatch()
        names = {s["function"]["name"] for s in agent.tools}
        if names != set(toolsets):
            raise RuntimeError("model surface differs from bounded tool surface")
        system_prompt = (
            "You are an assistant. Apply the supplied repository skill to the current "
            "user request. Only the tools supplied in this run are available. "
            "This is a dry-run: use the supplied tools to record intended operations "
            "appropriate to the request; they do not execute changes. "
            "Do not claim that a recorded operation was actually performed. "
            "This observation ends at the first change intent. Stop after its tool reply; "
            "do not proceed to editorial or derived operations. If no change is "
            "appropriate, answer without one. "
            "Tool results accurately describe their execution boundaries.\n"
            f"Current working directory: {cwd}\n"
            f"Known existing archive root: {root}\n\n"
            "Repository skill (loaded for this conversation):\n" + payload["skill"]
        )
        case = payload["case"]
        result = agent.run_conversation(
            case["message"].replace("$ARCHIVE_ROOT", str(root)),
            system_message=system_prompt,
        )
        # Scorer-only: the case expectation decides whether the observed
        # extract needed a prior complete README reply. It stays out of every
        # model-facing string and out of the tool schema.
        reasons = audit_result(result, observer.events,
                               payload["runtime"]["provider"], payload["model"],
                               require_readme_for_extract=case.get("expect_save") is True)
        verdict = evaluate(case, observer, completed=result.get("completed") is True,
                           failed=result.get("failed") is not False)
        if reasons:
            verdict["reasons"] = [r for r in verdict["reasons"] if r != "all checks passed"]
        verdict["reasons"].extend(reasons)
        verdict["passed"] = verdict["passed"] and not reasons
        verdict.update(
            api_calls=result.get("api_calls"),
            runtime_provider=result.get("provider"), runtime_model=result.get("model"),
            completed=result.get("completed"),
            # Keep actual tool-call evidence, not chain-of-thought or credentials.
            tool_calls=[call for m in result.get("messages", [])
                        for call in m.get("tool_calls", [])],
            tool_messages=[({"role": "assistant", "tool_calls": m["tool_calls"]}
                            if m.get("tool_calls") else
                            {k: m[k] for k in ("role", "tool_call_id", "content") if k in m})
                           for m in result.get("messages", [])
                           if m.get("tool_calls") or m.get("role") == "tool"],
            final_response=result.get("final_response", ""),
        )
    finally:
        agent.close()
    after = inventory(base)
    verdict["archive_unchanged"] = inventory(root) == archive_before
    verdict["bootstrap_files"] = before
    verdict["fixture_files_unchanged"] = after == before
    verdict["changed_fixture_files"] = sorted(
        name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    if after != before:
        verdict["passed"] = False
        verdict["reasons"] = [r for r in verdict["reasons"] if r != "all checks passed"]
        verdict["reasons"].append("isolated fixture files changed (including home/cwd and agent close)")
    return verdict


def main(argv=None) -> int:
    import argparse
    import datetime
    import hashlib
    import logging
    import os
    import platform
    import subprocess
    import sys
    import tempfile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-root", type=Path)
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--output", type=Path, help="new evidence directory outside the checkout")
    parser.add_argument("--case", action="append", help="case id; default is all cases")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        payload = json.load(sys.stdin)
        try:
            verdict = _worker(payload)
        except Exception as error:
            # Exception text can contain provider/credential details: never emit it.
            import traceback
            verdict = {"id": payload["case"]["id"], "passed": False,
                       "reasons": [f"worker failed: {type(error).__name__}"], "events": [],
                       "error_frames": [{"file": Path(f.filename).name, "line": f.lineno,
                                         "function": f.name}
                                        for f in traceback.extract_tb(error.__traceback__)]}
        print("CAK_PROBE_RESULT=" + json.dumps(verdict, ensure_ascii=False))
        return 0
    if not all((args.hermes_root, args.provider, args.model, args.output)):
        parser.error("--hermes-root, --provider, --model and --output are required")
    from skill_intake_harness import cases
    selected = [c for c in cases() if args.case is None or c["id"] in args.case]
    if not selected or (args.case and set(args.case) != {c["id"] for c in selected}):
        parser.error("unknown or empty case selection")
    repo = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if output == repo or repo in output.parents:
        parser.error("evidence must be outside the checkout")
    if output.exists():
        parser.error("output already exists; use a new directory, never overwrite evidence")
    output.mkdir(parents=True)
    logging.disable(logging.CRITICAL)
    source = args.hermes_root.resolve()
    sys.path.insert(0, str(source))
    # Supported auth resolution is the only bootstrap that uses the caller's
    # profile. No credential file is copied, displayed, or exposed to the model.
    # This adapter intentionally supports explicit chat-completions API routes,
    # not OAuth, plugins, external-process providers or fallback chains.
    import_module("hermes_cli.env_loader").load_hermes_dotenv(load_external_secrets=False)
    resolved = import_module("hermes_cli.runtime_provider").resolve_runtime_provider(
        requested=args.provider, target_model=args.model)
    runtime = {k: resolved[k] for k in ("provider", "api_key", "base_url", "api_mode")}
    if (runtime["provider"] != args.provider or runtime["api_mode"] != "chat_completions"
            or not isinstance(runtime["api_key"], str) or not runtime["api_key"]):
        raise RuntimeError("probe requires an explicit API-key chat-completions route")
    skill_path = repo / "skills/content-archive/SKILL.md"
    skill = skill_path.read_text(encoding="utf-8")
    version = import_module("hermes_cli.version_info").get_version_info()
    report = {
        "schema_version": 1, "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "provider": args.provider, "model": args.model, "hermes_revision": version.commit,
        "hermes_version": version.display_version,
        "hermes_source_hashes": {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                                 for name in ("run_agent.py", "agent/agent_init.py", "tools/registry.py",
                                              "agent/inline_tool_executors.py", "agent/conversation_loop.py")},
        "python": platform.python_version(), "skill_sha256": hashlib.sha256(skill_path.read_bytes()).hexdigest(),
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "harness_sha256": hashlib.sha256((repo / "tests/skill_intake_harness.py").read_bytes()).hexdigest(),
        "scope": "loaded-skill intake decisions with synthetic, non-executing tools; not discovery or end-to-end archiving",
        "expected_case_ids": [c["id"] for c in selected], "cases": [],
    }
    for case in selected:
        with tempfile.TemporaryDirectory(prefix="case-", dir=output) as directory:
            base = Path(directory)
            home, root, other = base / "home", base / "archive", base / "other-project"
            for path in (home, root, other):
                path.mkdir()
            cwd = other if case["cross_project"] else root
            payload = {"hermes_root": str(source), "runtime": runtime, "model": args.model,
                       "skill": skill, "root": str(root), "cwd": str(cwd), "case": case}
            try:
                child = subprocess.run(
                    [sys.executable, "-B", str(Path(__file__).resolve()), "--child"],
                    input=json.dumps(payload), capture_output=True, text=True,
                    env=child_environment(home, os.environ), cwd=cwd, timeout=300,
                )
                lines = [line.removeprefix("CAK_PROBE_RESULT=") for line in child.stdout.splitlines()
                         if line.startswith("CAK_PROBE_RESULT=")]
                if child.returncode != 0 or len(lines) != 1:
                    raise RuntimeError("missing or ambiguous worker receipt")
                row = json.loads(lines[0])
                # Portable evidence: redact credentials defensively and replace
                # scratch locations before writing any transcript to disk.
                text = json.dumps(row, ensure_ascii=False).replace(runtime["api_key"], "[REDACTED]")
                row = json.loads(text.replace(str(base), "$FIXTURE"))
            except (subprocess.TimeoutExpired, RuntimeError, ValueError) as error:
                row = {"id": case["id"], "passed": False,
                       "reasons": [f"probe incomplete: {type(error).__name__}"], "events": []}
        report["cases"].append(row)
        report["passed"] = len(report["cases"]) == len(selected) and all(c["passed"] for c in report["cases"])
        (output / "receipt.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"id": case["id"], "passed": row["passed"], "reasons": row["reasons"]}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Probe could not run: {type(error).__name__}")
        raise SystemExit(2)
