# Tests

Unit, integration, and acceptance tests for Content Archive Kit. Everything here
uses synthetic archive content: no article, image, state file, credential,
recipient, or machine-local path from a real archive is used. The default suite
does not require model credentials. The separately invoked model probe below
uses a configured model provider, not an archive's credentials.

## Running

```bash
cd tests
../.venv/bin/python -m unittest discover -s . -t .
```

`discover` must run from this directory (the checkout path may contain spaces,
which breaks package discovery from above). Any single module works the same
way:

```bash
cd tests
../.venv/bin/python -m unittest test_phase2 -v
```

The suite needs the toolkit's own runtime dependencies (`beautifulsoup4`,
`PyYAML`, and `cryptography` for the publication tests). Run it with the
interpreter that has them installed; see the root `requirements*.txt` files.

### Reading the result

`unittest` prints `Ran N tests` where **N includes skipped tests**. A run that
ends `OK (skipped=2)` means `N - 2` tests actually executed and passed; a
skipped test is not a passing test. Only quote a pass count when the summary is
`OK`; quote failures, errors, and skips from the same summary line. CI fails on
any non-zero exit, so a green workflow already means no failures or errors, but
the skip count still has to be read separately.

## Optional subscription selection (unreleased)

```bash
cd tests
../.venv/bin/python -B -m unittest \
  test_subscription_selection test_subscription_binding \
  test_subscription_recovery test_subscription_budget test_subscription_digest \
  test_subscription_manual test_subscription_repair_v2 \
  test_selection_repository_boundary -v
```

These synthetic suites exercise policy and evidence validation, preview binding,
admission, retry/reassessment, budgets, accounting/digest reporting and manual-save
independence. The repository checks reject a live root `selection.yaml` and an
enabled or invalid shipped selection example. They do not enable an instance,
call an editorial model, or establish editorial accuracy. Passing these tests is
not sufficient acceptance: independent cross-run and corruption probes can still
expose missing boundaries. See the current [feature status](../docs/subscription-selection.md).

`test_subscription_digest.py` uses the real digest renderer in synthetic projects
to distinguish mixed rejected/technical-failure, rejected/unjudged and
rejected/pending-archive runs from all-rejected or all-deferred runs. Compatibility
controls keep historical outstanding-work warnings visible and prevent an empty
candidate set from producing a vacuous all-rejected/deferred summary. The
pending-queue rendering has its own regression coverage in the same file: a
digest of the latest fetch run — or an overview rendered without `--run` — reads
the live `pending.json` queue rather than the run's fetch-time snapshot; a valid
zero omits the queue line; a missing queue file keeps the snapshot; an unreadable
queue is reported as unknown, never as zero; a historical run keeps its own
snapshot; a copy of the latest run is recognized by its non-empty `generated_at`,
not its path; and rendering never modifies pending, run, seen, or ledger bytes.

## Existing-archive and skill behavior

```bash
cd tests
env -u PYTHONPATH ../.venv/bin/python -B -m unittest \
  test_existing_archive_acceptance test_skill_intake_harness test_skill_probe_runner -v
```

- `test_existing_archive_acceptance.py` copies a synthetic existing archive and
  the real CLI/scripts. It searches, updates through `finalize` or `add --force`,
  rebuilds indexes, searches again, and audits. Unrelated entry directories
  (Markdown, raw snapshot, images) remain byte-identical; identity/path/first-seen
  state remains stable; force replacement retains the old directory in trash.
  Negative controls verify that skipped updates and unrelated-file/state damage
  are detected. This is not a migration of a real private archive.
- `skill_intake_harness.py` retains the original eight inputs: Chinese full-width
  and ASCII-colon save triggers, a Chinese cross-project save, a bare URL,
  summary-only, quoted-example, rule-discussion, and translation-only requests.
  Ten English inputs add `Save:`, `Archive:` without a space, lower/upper-case
  variants, an English cross-project save, and summary, quoted-example,
  rule-discussion, translation, and explicit-no-save negatives: eighteen in all.
  `save-ascii` is a Chinese command with an ASCII colon, not an English test.
  The observer records attempts
  without executing shell commands, writing files, or fetching articles.
- `test_skill_intake_harness.py` and `test_skill_probe_runner.py` test the observer
  and evidence checks with synthetic call sequences. **Passing these offline
  tests is not evidence that a model follows the skill.** They run in normal CI
  without Hermes or an API key.

### Opt-in real-model probe

Run `run_skill_intake_probe.py` explicitly with a Hermes-capable Python, a trusted
Hermes source directory, and an already configured API provider. This makes real
model API calls and consumes its allowance. It is not part of unittest discovery
or public CI; no new dependency or credential is required by the toolkit.

```bash
env -u PYTHONPATH "$HERMES_PYTHON" -B tests/run_skill_intake_probe.py \
  --hermes-root "$HERMES_SOURCE" \
  --provider "$PROBE_PROVIDER" --model "$PROBE_MODEL" \
  --output "$SCRATCH/new-skill-probe"
```

Set these variables for the local environment; the output directory must be new
and outside this checkout. `--case CASE_ID` selects a subset for diagnosis, not
full eighteen-case acceptance. No credentials belong in command arguments. The
adapter supports an explicit API-key `chat_completions` route; OAuth,
external-process/plugin routes and fallback chains are outside its scope.

The bootstrap resolves the selected provider through Hermes, then passes only
that runtime's credential in memory to fresh child processes. Each child uses
a scrubbed environment, scratch home/cwd, no session database, no imported user
memory or context files, and no background review. The process-local tool
registry and inline handlers are replaced with bounded synthetic tools before
inference; tool names and handler identities are checked again after agent
initialization. A Hermes source `.env` is refused. No installed skill, cron job,
private archive, publication target, or Hermes configuration is edited.

The repository's exact `skills/content-archive/SKILL.md` is preloaded. Positive
and negative cases receive the same dry-run context: tools record intended
operations without executing changes, and the assistant must not claim a real
save. The shared context declares the first-change observation phase in advance:
after the first change-intent tool reply, stop rather than continuing into
editorial or derived operations. This phase instruction names no correct command
or trigger; the canonical skill supplies the workflow. No expected verdict or
case identifier is supplied to the model. Positive
cases need a successful complete target README read, with the reply delivered
in a prior model turn, before extraction or a correctly rooted intake attempt.
Reading and requesting either operation in the same tool-call batch does not
satisfy that prerequisite. Read-only completion order is not model-message
causality; a later failed read does not revoke a prior complete successful reply.
Negative cases fail on write attempts, including blocked attempts;
unchanged files alone cannot make them pass. Raw model tool calls must match
recorded dispatch counts and arguments exactly, allowing concurrent read calls
to finish in a different order. Incomplete, failed, timed-out, mismatched-route
or malformed-evidence runs fail rather than becoming skipped/passed cases.

The positive route is deliberately narrow: `bin/ca add URL` with optional
`--collection manual`, using the resolved absolute CLI path across projects.
Direct `write_file` attempts, derived operations after that intake attempt, and
unsupported commands fail this protocol even on an authorized save request.
The full toolkit supports more operations than this observer. Such a failure
does not by itself prove that the user withheld authorization or that a real
archive was modified. Results and remaining gaps are recorded in
[behavior acceptance](../docs/behavior-acceptance.md).

`receipt.json` records individual results, observed calls and tool replies, model/provider,
Hermes version/source hashes and skill/harness hashes. Scratch paths are
normalized, credentials are not published, and each completed case is saved
before the next one starts. Review the receipt before publishing it. The
adapter depends on Hermes runtime interfaces, so a different engine version
requires revalidation; this is process/tool-surface isolation, not an OS sandbox.
The file-inventory check includes the isolated home and alternate working
directory through agent shutdown, not only the archive directory. Idempotent
framework logging/scratch setup runs before the baseline snapshot; its file
hashes are retained as `bootstrap_files`, not ignored by later comparisons.
It does not
monitor arbitrary host paths or prove the absence of transient create/delete
activity; safety still relies on the trusted engine and bounded handlers.

This probe measures only the observed **loaded-skill authorization and
instance-routing decisions in the explicit dry-run context** for the named
model/environment. A failed or incomplete run establishes no passing acceptance. It does not test
automatic skill discovery, complete intake/editorial correctness, live fetching,
all possible paraphrases, or messaging gateway behavior. The separate CLI tests
exercise actual archive writes. A single successful model run is not a universal
guarantee, and its count must not be added to the unittest pass count.

Final-response text is retained for human review but is not semantically graded.
In the bilingual run, all eighteen action/routing checks passed, yet the explicit
no-save response still invited the user to save later, contrary to the skill.
Do not interpret a passing action grade as complete response-policy compliance.

## Reader media scheduling

`test_reader_media_scheduler.py` runs the production scheduler through Node and
checks its integration/security contract. `reader_media_scheduler.mjs` contains
15 behavior cases with controlled geometry and load promises; those nested cases
are not 15 additional unittest tests. Run directly from the checkout root:

```bash
node tests/reader_media_scheduler.mjs
```

The opt-in real-browser fixture includes mixed image aspect ratios, a repeated
image/attachment object, a slow first image, distant media, and a genuinely hidden
mobile pane. It counts requests when they start, including unfinished transfers,
and requires decoded pixels while the next image is still below the fold. A bad
geometry precondition fails rather than skipping the assertion. Native `src`
assignment is observed to catch transient placeholder collapse before decoding.
The media fixture uses image-only galleries to avoid font-metric timing
assumptions. A pass-through fetch observer records every occurrence's geometry
at request time; server response barriers hold the first wave so the mobile
hide/re-show check has one demonstrably eligible pending object. It requires
no new requests while actually hidden and resumption without a remount or
scroll. Earlier initial-placeholder geometry can become stale after image
decoding and must not classify a later request. Independent scratch-only
mutations (fetch-all, missing resume, hidden fetch) must still fail the
corresponding assertions when validating this harness.
The existing normal/tamper browser test now tests authenticated invalid PNG bytes
as a separate decode-error case, then uses a valid generated PNG for normal
acceptance. An error placeholder cannot satisfy the happy path.

Install Playwright and Chromium in an optional test environment outside this
checkout. Set `CA_PLAYWRIGHT_PYTHON` to that environment's interpreter; optionally
set `CA_PLAYWRIGHT_BROWSER` to an existing Chromium executable instead of using
Playwright's installed browser. From `tests/`:

```bash
env -u PYTHONPATH CA_RUN_BROWSER_TESTS=1 \
  CA_PLAYWRIGHT_PYTHON="$CA_PLAYWRIGHT_PYTHON" \
  ../.venv/bin/python -B -m unittest \
  test_reader_media_scheduler test_encrypted_site.TestBrowserAcceptance -v
```

The default suite/hosted CI does not run Chromium. Historical source-bound local
results and additional lifecycle/upgrade probes are documented in
[reader media acceptance](../docs/reader-media-acceptance.md); later harness limits
and reader changes are described in [reader experience](../docs/reader-experience.md).
An older receipt does not certify a changed fixture or a later candidate.

## Reader formatting, ordering and capacity

From `tests/`, run the bounded synthetic regression:

```bash
env -u PYTHONPATH ../.venv/bin/python -B -m unittest \
  test_article_formatting test_format_backfill test_format_backfill_cli \
  test_media_markup test_media_rendering test_reader_sort test_publish_capacity -v
```

Formatting checks exercise HTML extraction and rendered Markdown, not only
source strings. Backfill fixtures and CLI candidates live in scratch; they must
not read a private archive. Capacity fixtures verify actual allowlisted sizes,
threshold boundaries and unavailable/unsafe exports without contacting hosting.

Enable the real-Chromium sorting test separately. It covers the offline page,
an older catalog without the new comparison fields, and a real encrypted export,
including filtering, selection/scroll retention and preference storage. The
native heading-row dropdown is checked for keyboard selection/focus, 44px hit
height and non-overlap at 320, 390 and 1280px. Visual review still requires
actual screenshots; use CJK fonts in an isolated browser environment for Chinese
line-breaking checks rather than making preview fonts a production dependency:

```bash
env -u PYTHONPATH CA_RUN_BROWSER_TESTS=1 \
  CA_PLAYWRIGHT_PYTHON="$CA_PLAYWRIGHT_PYTHON" \
  ../.venv/bin/python -B -m unittest test_reader_sort.BrowserSortAcceptance -v
```

The Node harness's individual cases are nested assertions, not extra unittest
tests. A passing selected module does not establish whole-candidate acceptance;
see [reader-experience boundaries](../docs/reader-experience.md).

Badge-caption regressions cover same-run white/grey style evidence, nested
inline spans, mixed-color/size and structural negatives, opaque-color parsing,
non-finite input, shorthand/reset rejection and absolute-size conversion.
End-to-end cases exercise extraction, safe caption rendering, candidate-only
backfill, unchanged source/raw/media bytes and repeat planning. These tests do
not establish perfect semantic caption detection or authorize live application.

## Repository checks

`tools/check_repository.py` is the public-tree gate used locally and in CI:

```bash
cd /path/to/Content-Archive-Kit
env -u PYTHONPATH .venv/bin/python tools/check_repository.py
```

Run it from the repository root with the project's virtualenv; `env -u
PYTHONPATH` keeps a caller's import path out of the check. The default command
is read-only: it does not rewrite the manifest and, because it sets
`sys.dont_write_bytecode` before importing the example loaders, it leaves no
`__pycache__` or `.pyc` files in the checked tree. It checks:

- exact file membership in **both** directions against
  `docs/public-tree-sha256.json` (missing entries and unlisted files both fail);
- byte counts and SHA-256 digests for every listed file;
- canonical relative POSIX paths: no absolute paths, drive letters, backslashes,
  `.`/`..` components, traversal, or non-canonical spellings;
- no symbolic links (the file itself or any ancestor) and no non-regular files;
- private-path screens (`.state`, `library`, `trash`, `.handoff`, live root
  configuration such as `publish.yaml` or `selection.yaml`, and key/environment filenames) plus a
  private-key content screen. Private-key findings name the path without
  printing matched bytes; syntax and loader diagnostics can include values;
- syntax: every shipped `.py` file is compiled in memory, `bin/ca` is checked
  with `bash -n`, and shipped `.js`/`.mjs` files with `node --check`;
- the launcher: `bin/ca` must be tracked as mode `100755`, and an extracted
  artifact must have the executable bit set;
- `examples/` configuration loaded through the real loaders: every watched
  source must be disabled, and `publish.yaml` and `selection.yaml` must stay disabled.

The checks run in a fixed order: path safety, membership, byte counts, SHA-256
and private-material screens first; Python/Bash/JavaScript syntax second; the
`examples/` loaders last. The loader step imports and executes
`scripts/common.py`, `scripts/media_policy.py`, `scripts/publish_site.py`, and
`scripts/subscription_selection.py`,
so it runs only when the earlier steps are clean and every loader and example
path is a regular file reached without a symlink. An unsafe or syntactically
invalid tree never executes them.

A checkout with `.git` uses `git ls-files` and rejects untracked non-ignored
files, so a new file must be staged before the manifest can include it. A
read-only extracted artifact without `.git` compares the manifest against the
filesystem and ignores only local environment/cache entries: `.venv/`, `venv/`,
`__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `*.pyc`,
`*.pyo`, and `.DS_Store`.

Use `--root PATH` to check a synthetic tree instead of this checkout:

```bash
cd /path/to/Content-Archive-Kit
env -u PYTHONPATH .venv/bin/python tools/check_repository.py --root /path/to/synthetic-root
```

`--update-manifest` is an explicit maintenance command, never a CI step. It
enumerates the reviewed file set, checks path safety and private-material
screens, then validates syntax and example configuration **before** writing
anything. Unlike read-only verification, it computes new byte counts and hashes
instead of requiring them to match the old manifest; that is how an approved
source change becomes the next inventory. It refuses an unsafe, untracked, syntactically invalid, or
enabled-example tree, so invalid code or configuration leaves the previous
manifest bytes intact (or leaves an absent manifest absent). On success it
preserves the existing JSON metadata and file mode, records a new
`captured_at`, excludes `docs/public-tree-sha256.json` itself, and replaces the
file atomically. Stage approved files first:

```bash
git add <approved-files>
env -u PYTHONPATH .venv/bin/python tools/check_repository.py --update-manifest
```

The checks are for reviewed public code, not an untrusted-code sandbox: the
example loaders execute Python, and arbitrary replacement code could write
files despite the normal checker's read-only behavior.
The checks are risk screens, not proof that a tree is free of private material.
They do not inspect git history, article or media semantics, deployment state,
or remote services, and they cannot prove that a path is not private by
content. The workflow file's YAML is also not proven semantically valid by
running the test suite. `actionlint` 1.7.12 is a separate maintainer validation
step run outside the GitHub workflow; the workflow itself does not invoke it.

## Environment switches

| Variable | Purpose |
| --- | --- |
| `CA_TEST_SCRATCH` | Parent directory for throwaway project trees. Defaults to `content-archive-kit-tests` under the platform temp dir (`TMPDIR`). |
| `CA_TEST_PYTHON` | Interpreter used for subprocess-level tests. Defaults to `<checkout>/.venv/bin/python` when present, else the running interpreter. |
| `CA_RUN_INSTANCE_TESTS=1` | Enables the read-only dry run that needs a real, configured archive (skipped in a public checkout). |
| `CA_RUN_BROWSER_TESTS=1` | Enables real-Chromium unlock/tamper, media-scheduling and sorting acceptance tests. |
| `CA_PLAYWRIGHT_PYTHON` | Interpreter that has Playwright installed (only used with `CA_RUN_BROWSER_TESTS=1`). |
| `CA_PLAYWRIGHT_BROWSER` | Optional existing Chromium executable; otherwise use Playwright's installed Chromium. |

Four tests are skipped by default: the instance dry run (no archive in a public
checkout) and the normal/tamper, media-scheduling and sorting browser acceptance tests
(need Chromium + Playwright). The
Node interop test runs whenever `node` is on `PATH`. The repository checker
needs `node` whenever the tree contains JavaScript, and the JavaScript syntax
tests skip themselves when `node` is absent.

## Continuous integration

`.github/workflows/test.yml` runs on pushes to `main`, pull requests, and manual
dispatch. It uses Ubuntu with Python 3.13, installs Node.js 22 for the
Node interop and checker syntax steps, installs both `requirements.txt` and
`requirements-publish.txt`, runs `pip check`, then `tools/check_repository.py`,
then the complete suite from `tests/`.

The workflow reads repository contents only (`permissions: contents: read`),
does not persist checkout credentials, has no repository secrets, and has no
publish, deploy, tag, release, or artifact-upload step. `CA_RUN_INSTANCE_TESTS`
and `CA_RUN_BROWSER_TESTS` are explicitly `0`; `CA_SKIP_AUTO_PUBLISH` is
deliberately not set, so the tests that exercise the enabled publication path
still run. Throwaway trees go under the runner's temporary directory via
`CA_TEST_SCRATCH`; `CA_SCRATCH_ROOT` points to that same dedicated directory so
runtime input/output checks recognize the fixture location even when the
runner directory is outside the platform's default temp directory. This uses
the existing explicit scratch option, not a weaker path check. The workflow
environment regression exercises both acceptance of fixture input/output and
rejection of project configuration and unrelated paths. Python commands clear
`PYTHONPATH` with `env -u`.

Linux is the supported platform. macOS was removed from the matrix by an
explicit scope decision, not accepted as passing; the earlier failures remain
in `docs/ci-acceptance.md`. No test was removed or newly skipped for that
decision: the complete suite, including path refusal and delivery failure
checks, still runs on Ubuntu. The existing job timeout is unchanged.

## Layout the tests expect

- `scripts/` holds the implementation. Scripts derive their project root from
  their own location, so a test that runs a script as a subprocess copies
  `scripts/` into its throwaway project and runs **that** copy. Running the
  checkout's script against sandbox paths would make the checkout the archive
  root — that is why several fixtures build a complete project tree instead of
  pointing a script at a stray directory.
- Sample configuration (`taxonomy.yaml`, `sources.yaml`, `media.yaml`) is read
  from `examples/` first and from the archive root second
  (`_support.template()`), so the suite runs both in this repository and in an
  instance that keeps its own copies.
- `requirements-publish.txt` must exist at the repository root
  (`test_publish_dependencies.py` compares it against what the publish modules
  actually import).
- `bin/ca` is exercised only as a copied launcher inside a sandbox.

## Shared helpers

`_support.py` provides the only path/interpreter plumbing:

- `ROOT` — repository root, derived from the file location;
- `SCRATCH` / `scratch_dir(prefix)` — throwaway trees outside the checkout;
- `archive_project(prefix, scripts=..., configs=..., state=...)` — a throwaway
  archive root with its own `scripts/`, sample config, and optional `.state/`;
- `template(name)` — sample configuration lookup (`examples/` then root);
- `PYTHON` — interpreter for subprocess tests;
- `state_digest(path)` — sha256 of a state file, `None` when absent, so the
  "tests did not touch instance state" guards also hold in a checkout with no
  state at all.

## Fixtures

- `fixtures/latest-fetch-synthetic.json` — a synthetic `fetch_sources.py` run
  (three invented vendors, six candidates) used by the digest numbering test.
  Its candidates are paired with index rows generated in the test; edit both
  together.

## Privacy rules for new tests

- Never read `library/`, `.state/`, `index.csv`, `index.md`, or an instance
  configuration from the checkout; build them inside a scratch project.
- Never assert on a personal recipient, repository, or host: assert on values
  the test configured itself, or on module constants (e.g. the publish allowlist)
  via `getattr` fallbacks.
- Never copy an article, image, or raw snapshot into this repository. Fixtures
  are invented text such as `example.com`, `Vendor Alpha`, and `archive@example.com`.
- File-input refusal tests must create an existing synthetic forbidden file;
  do not depend on an operating-system file such as `/etc/hostname` existing.
  Otherwise a missing-file error can mask the path-validation branch. A file
  in a copied project's root, outside `.state/`, is forbidden even when that
  project lives under scratch. Invoke the copied CLI and verify both the
  refusal diagnostic and unchanged fixture bytes; do not widen the allowlist
  or skip an operating system to make these tests pass.
- New repository-check tests build a synthetic tree under `CA_TEST_SCRATCH` and
  invoke `tools/check_repository.py --root <tree>`; they must not rewrite the
  real manifest or touch the checkout's files.
