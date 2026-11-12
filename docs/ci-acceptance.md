# CI and Release Readiness Acceptance (historical record)

## Status

Historical acceptance record for the CI/release-preparation unit based on
`db4b50150c4dcde0780660e171298b030f400153` and its approved follow-ups. Local
integration was accepted by the parent; the standing hosted gate is a
successful exact-commit Ubuntu job under the approved Linux-only scope. Every
count below is bound to the tree or commit named with it and does not describe
the current suite or current HEAD; a later candidate requires its own complete
local checks and exact-commit Ubuntu CI. Earlier macOS failures remain
historical evidence, not passes. This record does not approve a tag, GitHub
Release, release asset, private-instance upgrade, notification, or deployment.

## Version bindings

- Unit base `db4b50150c4dcde0780660e171298b030f400153`; launcher-repair export
  tree `ce5f3ffdbdc1a43176960580da05edecfdc6db95` (baseline source plus
  executable launcher); CI-preparation commit
  `1012f134607bfae55d6946cfa089f9a70810f261`; scratch-mapping commit
  `581e3250ca08dc42d41a9c383d1c3515d408318a`.
- In the accepted state of this unit, `bin/ca` had Git mode `100755`; the unit
  base `db4b5015…` recorded `100644` (see the failed baseline below). Its blob,
  `a6c9a28ddf40d46b588070857e885eaf74d462d8`, was unchanged. No
  runtime source contents or dependency versions were changed by this unit.
- Local verification environment: Python 3.13.5, Node 26.12.1, Bash 5.2.37,
  Git 2.47.3. The hosted workflow selects Ubuntu with Python 3.13 and Node 22.
- Roles: implementation `deepseek-v4.1-flash` / `opencode-go`; independent
  review `z-ai/glm-5.3-flash` / `commandcode` — original verdict
  **CONDITIONAL**, not PASS; parent `gpt-6-astra` / `openai-codex` closed the
  reproducible required findings using the repaired code, regression tests and
  direct probes below. This is parent acceptance, not a fabricated second
  reviewer verdict.

## Method

- Local runs exported a staged Git tree, created a fresh Python 3.13.5
  environment inside the export, installed `requirements.txt` and
  `requirements-publish.txt` with `PYTHONPATH` cleared, then ran `pip check`,
  imports of `lxml.etree`, `trafilatura`, `yaml` and `cryptography`,
  `tools/check_repository.py`, and full `unittest` discovery.
- Checksum-verified `actionlint` 1.7.12 on `.github/workflows/test.yml`: exit
  0. This checks Actions syntax, not an actual hosted run.
- The inventory contained **93 files**, excluding the manifest. Verification
  checks membership in both directions, byte counts, SHA-256,
  path/type/mode safety, syntax and disabled examples. The initial
  CI-preparation diff had 12 paths; added lines were screened for credential
  literals, unsafe execution and private machine/deployment details, while
  runtime scripts, dependency pins, the canonical skill, `AGENTS.md`,
  `IDEA.md` and the historical acceptance record remained unchanged. These
  screens do not prove authenticity or the absence of all private content.
- `unittest`'s `Ran N tests` includes skips; pass and skip counts are
  reported separately. The two default skips in that unit were
  `test_real_chromium_unlock_search_exit_and_tamper` (requires
  `CA_RUN_BROWSER_TESTS=1` and browser tooling) and
  `test_preview_is_read_only_and_internally_consistent` (requires
  `CA_RUN_INSTANCE_TESTS=1` and a configured private instance). Neither is
  reported as passed. Historical migration evidence in
  [acceptance.md](acceptance.md) is not used as this unit's result.

## Local results (bound to their trees)

| Stage | Actual result |
| --- | --- |
| Baseline `db4b5015…`, full suite | **567 tests, 1 error, 2 skipped — a failed baseline, not a green result** |
| Export tree `ce5f3ffd…` after the launcher mode fix | 567 total, 565 passed, 2 skipped, 0 failures/errors |
| CI-preparation commit `1012f134…`, checker module | 47 tests, all passed, 0 skipped (included in that commit's integrated total) |
| Checker/workflow module after the scratch-mapping repair | 48 passing tests |
| Fresh staged-tree export, before the hosted push | 614 total, 612 passed, 2 skipped, 0 failures/errors |
| Fresh export using the workflow's actual environment mapping | 615 total, 613 passed, 2 skipped, 0 failures/errors |
| Fresh export after the macOS fixture repair | 615 total, 613 passed, 2 skipped, 0 failures/errors |

The baseline failure is reproducible in
`test_publish_hooks.TestPublishCommand.test_wrapper_routes_publish_without_using_the_real_archive`:
Git recorded `bin/ca` as `100644` while the working file was executable and
the checkout had `core.filemode=false`, so the Git export lost the launcher's
execution bit. A scratch-only mode change made that one test pass with
identical launcher contents. The approved correction changed the recorded Git
mode only; no test skip or CI-only chmod is an acceptable substitute for
fixing the distributed Git mode. Totals were measured from runner output, not
by adding module summaries.

Offline negative probes and repairs, each retained as regression coverage:

- Three checker defects were reproduced (default example-loader imports wrote
  bytecode; a rejected symlinked loader was still executed; manifest updates
  accepted enabled examples). After a bounded test-first repair the parent
  reran **45 checker tests, all passed with no skips**, then independently
  exercised **10 probes** across Git-checkout and extracted-tree modes:
  default checks left the file inventory and bytes unchanged without either
  cache environment variable; verify/update rejected linked loaders without
  executing their markers; enabled source/publication examples were rejected
  without rewriting the manifest.
- Integration found the scanner's own PGP-marker literal triggered its
  private-key screen. The self-scan regression failed before splitting the
  source literal and passed after, without changing the compiled detection
  expression; a second regression confirms an actual synthetic PGP marker is
  still rejected and not printed.
- The first hosted failure (below) was reproduced locally in two existing
  tests with disjoint platform and runner directories; a new workflow-driven
  regression reads the actual YAML environment and exercises the real
  input/output path validators in a subprocess — it failed before the
  workflow repair and passed afterward.
- Three macOS path-refusal failures were reproduced in a frozen Linux export
  by substituting a nonexistent system pathname. The approved repair creates
  existing synthetic forbidden files in copied project roots outside
  `.state/`, invokes copied CLIs, preserves refusal assertions and checks
  unchanged fixtures; both repaired modules ran **36 tests passed**, with
  **3 control passes** and **3 expected failures** when only the path
  validator was deliberately bypassed inside the scratch subprocesses — the
  tests still detect loss of path enforcement, not merely file existence.

## Hosted results (historical)

| Commit | Job | Result |
| --- | --- | --- |
| `1012f134…` | [run 42782656855](https://github.com/taukemora/Content-Archive-Kit/actions/runs/42782656855) | **Not an accepted pass**: 614 total, 10 failures, 16 errors, 2 skips |
| `581e3250…` | [Ubuntu job 129725574872](https://github.com/taukemora/Content-Archive-Kit/actions/runs/42785505233/job/129725574872) | Successful |
| `581e3250…` | [macOS job 129725575503](https://github.com/taukemora/Content-Archive-Kit/actions/runs/42785505233/job/129725575503) | **Cancelled** — exceeded the 25-minute execution limit |

The first hosted run installed dependencies and passed the repository gate,
but its suite failed: fixtures lived under the runner temp directory while
extraction and content-input checks still allowed the platform temp root. The
initial local export had nested fixtures under platform temp, so the
environment mismatch was not exposed locally; no green local run is
substituted for the failed hosted result. The follow-up maps the existing
`CA_SCRATCH_ROOT` option to the same dedicated runner directory as
`CA_TEST_SCRATCH` and enables verbose test names; runtime source, dependency
versions and path-safety rules remain unchanged. A fresh staged-tree export
installed into a new environment and tested with the workflow's actual
mapping passed as recorded above; at that stage the candidate still required
both Ubuntu and macOS jobs.

macOS: no complete suite summary is available. The log continued through
later tests, so the cancellation is not evidence of a streaming deadlock.
Three of the four observed failures depend on `/etc/hostname` existing (the
content/raw-file refusal cases in `test_phase3b` and the summary-file refusal
case in `test_phase3c`): the CLI checks existence before path authorization,
so an absent file produces `not found` instead of reaching the expected
`path not allowed` branch. The fourth,
`test_phase5_delivery.TestNoCrossFallback.test_weixin_session_not_ready_fails_fast_without_second_email`,
had no printed failing assertion; a wall-clock assertion exists in that test,
but source inspection alone does not prove it caused the hosted failure. That
failure and the job timeout remained unresolved when the support scope was
narrowed, and the fixture repair is not a claim of green macOS CI. No
threshold was raised, test skipped, safety check weakened, or runtime
behavior changed to hide these results.

## Linux-only support decision

The maintainer approved limiting supported platforms and CI to Linux with
Ubuntu as the hosted runner. macOS is no longer a release gate and has no
compatibility guarantee — a support-scope decision, not a repair or passing
result for the historical macOS jobs. The workflow removes only
`macos-latest` from its runner matrix; Python 3.13, Node 22, the full
unittest command, disabled optional suites, read-only permissions, pinned
actions and the 25-minute job timeout remain unchanged. The synthetic
file-input repairs are retained as general test-isolation improvements, and
delivery and path-safety tests (including the one that failed on macOS)
remain in the Ubuntu suite. The READMEs, release procedure/draft, test
instructions and canonical in-repository skill state the same Linux-only
scope; only the skill's platform metadata and platform guidance changed and no installed skill was
updated. The older platform statement in [acceptance.md](acceptance.md) is
preserved as migration history, superseded by this decision.

## Remaining gates and limits

- Finalize documentation, evidence and manifest only after an integrated
  run; repeat the affected checks and validate the final staged-tree export
  before any authorized commit/push. CI must never regenerate the manifest or
  apply a temporary launcher chmod to hide drift.
- After push, require the successful Ubuntu job for the exact immutable
  commit; the final SHA and run URL are read back from GitHub and reported
  separately. Editing this record later creates a new candidate that requires
  its own CI.
- The checker validates reviewed public code; it is not an untrusted-code
  sandbox. It imports real example loaders only after earlier
  integrity/safety/syntax checks pass. Private-key findings omit matched
  bytes, but syntax and loader diagnostics may include values.
- The workflow is validation-only: read-only repository permissions, no
  repository secrets, and no notification, release, asset-upload or
  deployment step. Browser and real-instance tests remain opt-in. Enabled
  publication code paths remain covered with synthetic fixtures; the global
  auto-publication suppression flag is not used to bypass those tests.
- No private archive is read or upgraded in this unit; no installed skill,
  cron job, publication target, NAS setting, or performance behavior is
  changed. Release notes remain a draft. Artifact export, checksum, clean
  artifact installation, and release publication belong to the later,
  separately authorized checkpoint in [releasing.md](releasing.md).