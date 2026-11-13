# Deployment and Instance Operations

Use when creating a new archive, scheduling collection, upgrading an installed toolkit, or moving an archive to another machine. Everything here describes an instance's own choices; no path, recipient, schedule, or credential belongs in this repository.

## Instance contract

The instance `README.md` is the fact source. Before operating, confirm from it:

- archive root, virtualenv path, and supported Python version;
- active taxonomy slugs and the summary character range;
- scheduled job time, timezone, and what each checkpoint command does;
- whether optional subscription selection is installed and explicitly enabled, with its policy, budgets and retention;
- delivery targets and whether each channel is enabled;
- publication state (absent, disabled, or enabled with a target);
- local hard bans (extra do-not-touch rules, vocabulary, or review gates).

If a fact is missing from the README, ask instead of inventing it. When you learn a durable new fact while working, propose a README edit to the owner rather than writing it into this skill.

## New instance layout

```text
<archive>/
├── README.md  AGENTS.md
├── taxonomy.yaml  sources.yaml  media.yaml      # copied from examples/ and adapted
├── selection.yaml                              # optional, disabled until approved
├── bin/ca  scripts/  templates/                 # installed toolkit
├── library/  trash/  .state/  index.csv
└── .venv/                                       # instance virtualenv
```

1. Create the project directory and virtualenv, install the toolkit's declared requirements, and verify imports with the instance interpreter (`python -c "import lxml.etree"` before relying on `trafilatura`).
2. Copy configuration from the repository `examples/`, then adapt: taxonomy slugs, summary range, source list, media budgets. Do not edit the shipped examples in place.
3. Write `README.md` (taxonomy, summary spec, commands, schedule) and a thin `AGENTS.md` (pointer plus hard bans).
4. Archive two synthetic-or-public articles through the full path before adding sources: intake → finalize → index → audit. A green audit on an empty archive proves nothing.
5. Add sources only after probing them (`references/vendor-sources.md`), and backfill to a clean watermark before enabling the schedule.
6. Create the scheduled job last, and confirm what it writes to.

`bin/ca` is a bash wrapper that resolves the archive root from its own location and picks an interpreter: `CA_PYTHON` when set, otherwise `<archive>/.venv/bin/python`, otherwise `python3` from `PATH`; it exits non-zero when none is executable. Because the fallback interpreter may not carry the toolkit's dependencies, verify imports (notably `lxml.etree`) in the interpreter that will actually run. Call the wrapper by resolved absolute path from any session whose working directory is not the archive; do not rely on the chat's cwd. `bin/ca root` prints the resolved archive root.

## Platform requirements

- POSIX shell and bash: the wrapper uses `set -euo pipefail` and `BASH_SOURCE`.
- Linux is the supported platform, with CI on Ubuntu. State locking uses `fcntl.flock` (advisory locks), imported unconditionally by the daily gate and source fetcher. Native Windows requires a Linux environment such as WSL. macOS is outside the maintained support and CI scope; POSIX compatibility alone is not an acceptance result.
- Python from the instance virtualenv. Verify the interpreter version against the installed wheels rather than using a system Python that happens to be newer; a too-new interpreter can install an `lxml` build that fails to import.
- Install the declared base requirements as shipped. They currently include `markdown-it-py` and `cryptography`, and `requirements-publish.txt` overlaps with them; separate files do not yet provide dependency isolation. Article media processing also imports `markdown-it-py`, so do not remove it merely because hosted publication is disabled. Publication behavior is optional; a minimal core-only dependency installation is not currently provided.

## Scheduled collection

- **Anchor the job to the archive.** Set the job's working directory to the archive root so its `AGENTS.md` is injected, and keep the schedule and timezone in the instance README.
- **Two checkpoints, one run.** `daily start` discovers candidates and records gate/source state. Without an enabled selection policy, the agent archives each candidate through the legacy path. With selection enabled on a matching engine, candidates need evidence-grounded accept/reject/defer decisions before only admitted work reaches `add --collection auto`; judgment/archive waits and technical failures remain distinct. `daily finish` reconciles outcomes and execution, rebuilds the index, audits, builds the digest, snapshots state, and prunes old run files. See `references/subscription-selection.md` (relative to the skill root). A single monolithic job that silently swallows a failed fetch is worse than two visible checkpoints.
- **Persistence over convenience.** Selected work stays durable until it is seen; quota-deferred work is recorded separately. A run that stops before finish must not lose candidates.
- **Failures are visible.** On `start` failure, overwrite the gate with `status: failed` instead of leaving yesterday's gate. On `finish`, require a fresh `started_at` (bounded age), a matching fetch generation timestamp, and no failed status.
- **First run and backfill.** Backfill only the latest configured N items per source, still filtering `seen`. Regular runs use the watermark to choose coverage width, not to reject unseen items at or below it.
- **Continuity.** If the scheduler supports attached sessions and run-to-run continuity, enable them so the user can reply with a follow-up instruction such as "move item 2". Keep dedupe in an explicit state file rather than in conversation memory.

## Delivery configuration

Delivery belongs to the instance, not the skill. Read the README for the enabled channels and their targets, then configure the scheduler accordingly.

- **Single or symmetric payload:** route the job's delivery to the platform's home channel, or to an explicit `platform:chat_id`, with `attach_to_session` enabled when the user should be able to reply.
- **Asymmetric payloads** (full digest to one channel, a character-capped summary to another, no cross-channel fallback): a comma-separated delivery list sends identical text everywhere, so deliver locally and run a delivery step at the finish checkpoint that builds one payload per channel. Persist per-channel outcomes in the gate file so a rerun skips channels that already succeeded, and exit non-zero when a required channel failed instead of falling back silently.
- **Never store credentials in the archive or this repository.** Read them from the environment the runtime already provides, and never print them in logs, digests, or failure output.
- **Test with a dry run** or loopback fixture before enabling live sends.

Channel setup, scopes, and retry rules: `references/messaging-channels.md`.

## Indexes and readers

- `bin/ca index` currently builds CSV, Markdown, and HTML together; there is no separate reader-generation switch. Using a reader service or hosted publication is optional, but generating the HTML is part of this command.
- Rebuild derived artifacts after a batch, never inside every file write; the manifest CSV and any HTML reader are both derived from `library/`.
- Keep Markdown authoritative; a reader that cannot be rebuilt from entries is a liability.
- Keep reader assets inline or alongside the archive so a local `file://` open and a mounted copy both work, and reference only localized `library/` images.
- Do not deliver an HTML file alone as a complete archive while images remain relative to `library/`; verify the actual opening method the user uses.

Synthetic publication/delivery tests do not establish acceptance of a real browser, message recipient, NAS deployment, or hosted site. The opt-in instance dry run covers daily-delivery preview, not archive search/update. Keep unverified integrations disabled until their own target-environment checks pass; none is required for basic local archiving.

## Upgrade, backup, and rollback

- **Pinned upgrades.** A private instance installs a reviewed release or a frozen, verified snapshot; it does not track an upstream development branch. Record the installed version and commit in the instance README or a provenance file.
- **Selection enablement is a separate gate.** Toolkit v0.1.0 does not include selection. Verify a matching engine/skill, adapt the disabled example privately, freeze and calibrate in isolation, and then obtain explicit owner approval for instance upgrade, policy enablement and any scheduler change. Calibration against synthetic or parent-labelled cases is not user-labelled accuracy. Do not delete historical articles or change manual-save behavior as part of enablement.
- **Freeze and hash before migrating.** Compare the accepted source hashes against what is about to be installed, and stop if they differ without an approved explanation.
- **Back up before applying.** Include `library/`, `.state/`, configuration, and the previous toolkit; a backup that omits `.state/` loses dedupe and watermark history.
- **Apply into a copy first.** Run the toolkit's tests and one end-to-end `add → finalize → index → audit` against the copy, then migrate. Compare entry counts, index rows, seen keys, and raw/image hashes before and after.
- **Never let an upgrade run side effects.** An upstream push must not modify a running archive, install a global skill, send a notification, or publish an article. Those actions require a separate, explicit decision.
- **Rollback is a restore, not a rewrite.** Keep the previous version and its backup available, and do not rewrite published history to hide a failed upgrade.

## Moving an instance

1. Copy `library/`, `trash/`, `.state/`, and configuration; rebuild the virtualenv on the target.
2. Re-resolve absolute paths that live outside the archive (credentials, publication workspace) — they are machine-local by design and must be re-entered, not migrated.
3. Re-run `index`, `audit`, and `reconcile-seen` on the target, then archive one new item end to end before trusting the copy.
4. Update the README with the new schedule and delivery targets, and keep the old instance read-only until the new one passes acceptance.
