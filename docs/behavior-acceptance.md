# Archive maintenance and loaded-skill behavior acceptance (historical record)

## Status

Historical acceptance record for bounded skill-intake and archive-maintenance
work after `79bda23067bd0d70b93bbff4b9cbfa73f00356b0`. Every count below is
bound to the tree, skill hash and run named with it; none certifies the
current HEAD, a future run, or the current test harness. Round 3 demonstrates
normal skill discovery and real CLI intake in its isolated synthetic setting;
these results do not establish live acquisition, gateway behavior,
installed-profile behavior, or general final-response compliance. Evidence is
not authorization for publication, deployment, or private-instance upgrades;
any actual skill update still requires separate approval. All archive content
used here is synthetic.

The executable archive engine, dependency pins, workflow, `AGENTS.md`, and the
initial migration acceptance record ([acceptance.md](acceptance.md)) remain
outside these rounds.

## Method and evidence layers

1. `test_existing_archive_acceptance.py` exercises copied real CLI/scripts on
   a synthetic existing archive: search → finalize or force replacement →
   rebuild → search → audit. Unrelated entry Markdown, snapshots and images
   must remain byte-identical; target identity/state and trash preservation
   are checked. Three tests passed in the parent-run local check.
2. Observer and runner unittests exercise fabricated tool-call sequences and
   framework fault injection. They validate the grader, not model compliance.
3. The opt-in probe preloads the canonical repository skill and makes real
   model calls against synthetic, non-executing tools. It measures intake
   authorization/routing attempts, not automatic skill discovery, full
   archiving, or deployment.
4. The later conversation runner starts fresh isolated Hermes homes with the
   candidate skill available through normal discovery, not preloaded into the
   task prompt. Its bounded tools execute the actual copied CLI against
   synthetic pages and inspect real archive artifacts. It is scratch-only
   evaluation infrastructure, not a shipped platform or part of the unittest
   suite.

Final loaded-skill probe protocol (Round 2): every case receives the same dry-run
context declaring the first-change observation boundary in advance — tools record intended
operations without executing them, and no expected outcome or case label is
supplied to the model. Positive cases require a complete target-README reply
in a prior model turn before extraction or a correctly rooted
`bin/ca add URL --collection manual` attempt using the resolved absolute CLI
path across projects; direct `write_file` attempts, derived operations after
the intake attempt, and unsupported commands fail even on an authorized save.
Negative cases fail on any write attempt, including blocked attempts. Raw
tool calls must match recorded dispatch counts and arguments exactly, while
concurrent read calls may finish out of order. Fixture inventories (archive,
isolated home, alternate cwd) are checked through agent shutdown. Earlier
grader and fixture corrections are recorded below; Round 2 also changed the
shared phase context. These are versioned experiments, not an unchanged-protocol
comparison. Round 3 uses the separate executing conversation runner.

## Round 1 — loaded-skill probe, eight cases

Bindings: skill `4ebcb43aa62464d8834ef656b65222489fd8fef2e65453aced0cef5ab046df05`;
pre-correction export tree `348e7b100a88353d1c4287d32306baac283c60ee`,
corrected export tree `8620aa312f7939e36c8b15b24a7764f79ff11126`; Hermes
0.23.0 at `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`, Python 3.14.9; model
`opencode-go` / `deepseek-v4.1-flash`.

Local: the pre-repair suite ran **677 tests: 675 passed, 2 explicitly
skipped** (151.855 s), bound to that pre-repair tree. The post-repair export
at tree `348e7b10…` installed the declared requirements with `pip check`
clean; the public-tree checker passed for 100 files (101 including its
excluded manifest); full discovery ran **707 tests: 705 passed, 2 explicitly
skipped** (149.407 s). The corrected export `8620aa31…` passed the public-tree
checker for 101 files (102 including its excluded manifest) and full discovery
ran **721 tests: 719 passed, 2 skipped**, 0 failures/errors. Temporary Git
indexes were used throughout; the working checkout's real index was unchanged.
All 32 protected source files matched the initial baseline in the parent
check.

Real-model results:

- **Initial run:** 3 passed / 5 failed. The three positive cases made no
  intake attempt — restricted read-only tools and a “do not retry” response
  caused the model to stop, and concurrent read completions caused a false
  evidence-order mismatch. `quoted-example` and `rule-discussion` made no
  write attempt but failed because the grader rejected a read-only
  directory-listing attempt. `bare-url`, `summary-only` and `translation-only`
  passed at the original probe's limited scope. Repairs are not applied
  retroactively to this verdict.
- **Post-repair run** ([receipt-02](reviews/2026-11-10-skill-probe-receipt-02.json),
  SHA-256 `c21bbab96099597e5fa430a6aab1e73954a9a9433b183528a4a3875e710ff1c9`,
  26 API calls) and **final run after V-1/V-2**
  ([receipt-03](reviews/2026-11-10-skill-probe-receipt-03.json), SHA-256
  `754853f79d56f10238708fea3ea7de7e234dde5d057630e1dc8ffb57eab83f0d`, 23 API
  calls): both **5 passed / 3 failed**, with different failure modes:

| Case | Post-repair run | Final run |
| --- | --- | --- |
| `save-cn` | No `bin/ca add`; one direct `write_file` attempt plus an unsupported Python command | Correct `bin/ca add … --collection manual` requested, then a direct `write_file` outside the first-change boundary |
| `save-ascii` | No `bin/ca add`; direct writes proposed for the entry, `index.csv` and `.state/seen.json` | Unsupported Python summary-count command; no `bin/ca add` attempt |
| `save-cross-project` | No absolute `bin/ca add`; one direct entry-write attempt plus an unsupported recursive listing | README and extraction requested in the same turn, before the prerequisite; no absolute `bin/ca add` attempt |
| `bare-url`, `summary-only`, `quoted-example`, `rule-discussion`, `translation-only` | Passed — no mutating attempt | Passed — no mutating attempt |

The parent checked the cross-project raw messages directly: README and
extraction were selected together in the first assistant batch, not merely
recorded in a different completion order, so the corrected causal gate was
measuring the actual batch in the final run. The 707-test result and the
receipt-02 run remain bound to the pre-correction source; receipt-03 binds
runner `c06408741162d5fe4428689e21973249788fa9ef50a6cda8a347d641fe536b74` and
harness `37d0396021e39803e7e74ffd67e90874d601d563568efcaf1933b03828c8b038`.

These are failed historical runs: the strict CLI intake route was not
followed. The three positive requests did authorize archiving, so these
failures do not establish five unauthorized writes; conversely they establish
neither successful positive routing nor end-to-end skill acceptance. Synthetic
tools executed none of the proposed changes and no real data was damaged. A
protocol failure is not automatically an unauthorized-user-action failure
(the small non-executing tool surface is stricter than the full toolkit), and
these runs do not isolate a model defect from skill/instance/tool-contract
effects. The canonical skill was not changed to improve the score.

Grader corrections, each verified by the parent through RED→GREEN
reproduction:

- **F1:** global call-order equality falsely rejected parallel read
  completion → compare argument/count multisets, but require the complete
  README reply in an earlier model turn; dispatch order alone cannot prove
  the prerequisite was received.
- **F2–F3:** read-only fixture coverage and contradictory tool-boundary
  wording suppressed valid intake evidence → repair the environment without
  executing commands, exposing real files, teaching case-specific answers, or
  relaxing write gates.
- **F4:** reading only one README line opened the gate (independently
  reproduced) → complete-file coverage required.
- **F5:** initialization could replace a registered handler → recheck names,
  handler identities and inline executors before inference.
- **F6:** archive-only hashes missed writes to the isolated home/alternate
  cwd → inventory all fixture files through agent shutdown (bootstrap files
  warmed before the baseline snapshot, then monitored, not ignored).
- **V-1:** the positive extraction prerequisite used dispatch completion
  order → moved to raw model-message order (prior-turn complete README
  reply); same-turn extraction still fails.
- **V-2:** a failed additional README read permanently poisoned a case →
  failed or partial reads must never open the gate, but must not revoke a
  valid completed read or prevent recovery before an intake attempt.

After V-1/V-2, same-turn, missing, failed, partial and wrong-ID prerequisite
controls still fail, and negative summary extraction still needs no archive
README. Before those corrections, the reviewer ran **89 offline tests**
(73 observer, 16 adapter); afterward the parent ran **103 offline tests**,
all passing. The independent reviewer
(`z-ai/glm-5.3-flash` / `commandcode`) examined the hash-frozen files and its
verdict remained **CONDITIONAL** on its frozen input; these corrections were
accepted through parent review and reproduction, not a new independent PASS.
Neither V-1 nor V-2 caused the three positive failures in the preserved rerun.

Embedded receipt hashes bind each run's skill, runner, harness and selected
Hermes modules; they must not be represented as runs of later code. At this
round's handoff, positive loaded-skill acceptance remained open; a follow-up
had to agree the skill/instance CLI intake contract and resolve the three
positive cases before claiming that conversational intake is accepted.
Hosted CI for these uncommitted changes and automatic discovery/gateway
behavior were not verified at that point.

## Round 2 — bilingual entry points, eighteen cases

Scope: English `Save:` / `Archive:` with case-insensitive prefixes and
optional spaces after the colon, retaining Chinese `收藏：` / `收藏:` and the
Chinese title/summary policy; the eight original probe inputs plus ten English
inputs, including explicit-no-save, quoted-trigger, translation and
cross-project cases. The canonical skill specifies the CLI-led draft →
editorial → index/audit workflow, complete-README delivery before dependent
operations, and no direct fabrication of persistent entries/state as a CLI
substitute. The probe context declares the first-change boundary before any
operation, so this run is not an unchanged-prompt comparison with Round 1.

Bindings: local export tree `b5c3e86c09803743d153801c0ee79691f0fc09e2`;
candidate `a0831ad85751e082d90a55ce67712d4681361227`, tree
`de6dffeb93f576d9ce3883275e73dcd0a2fe4df7`
([PR #1](https://github.com/taukemora/Content-Archive-Kit/pull/1)); skill
`559ef1d4b28a57648bfce1356103f0b1b86935078b37204765ac185d1dc185f1`; Hermes
0.23.0 at `2bd378ed…`, Python 3.14.9 (probe) / 3.13.5 (suite); model
`opencode-go` / `deepseek-v4.1-flash`.

Local and hosted: **112 observer/runner unittests**, all passing; fresh
3.13.5 environment — install and `pip check` passed, the public-tree checker
passed for 102 files (103 including its excluded manifest), and full discovery
ran **730 tests: 728 passed, 2 skipped**, 0 failures/errors (156.362 s). The
hosted run on the synthetic merge commit
`d27ddfd3ad924e734b1e30210de450314784a8ef` (tree matches the candidate):
**730 total, 728 passed, 2 skipped**
([run 42944156572](https://github.com/taukemora/Content-Archive-Kit/actions/runs/42944156572)). The subsequent documentation/evidence
commit is a new candidate and requires its own hosted CI; no future result is
asserted here.

Real-model probe
([receipt](reviews/2026-11-10-bilingual-skill-probe-receipt-01.json), SHA-256
`ffcf5a0fab728e35d8027451205ef6dd94911a5fea85cbdda9d1b85a354649f0`):
**18/18 action/routing passes across 37 API calls**, no failed or omitted
cases. The three previously failing positive routes (`save-cn`, `save-ascii`,
`save-cross-project`) and all five English positive variants (`save-en`,
`archive-en`, `save-en-lowercase`, `archive-en-uppercase`,
`save-en-cross-project`) deliver the complete README reply first, then one
correctly rooted `add` intent with no downstream write; all ten negative cases
(`bare-url`, `summary-only`, `quoted-example`, `rule-discussion`,
`translation-only`, `summary-only-en`, `quoted-example-en`, `rule-discussion-en`,
`translation-only-en`, `explicit-no-save-en`) show no mutating intent.

Reply-quality defect, disclosed rather than erased: `final_response` is
preserved but not semantically graded. `explicit-no-save-en` ended with an
offer to save later, and `summary-only-en` similarly offered future saving,
contrary to the skill's rule against offering archiving after an explicit
refusal. Neither attempted a mutation, so the action grades pass, but reply
behavior is **not fully compliant**. The transcript, skill and grading were
not changed to hide this; it remains a known model-compliance limitation.
At the end of this round, the bilingual entry points and the three previously
failing CLI intake routes were accepted locally **at the action/routing scope
only**; Round 3 later adds normal-discovery and real synthetic-fixture intake
evidence, while reply-policy quality remains a known limitation throughout.

Review: `z-ai/glm-5.3-flash` / `commandcode` returned **PASS** for the frozen
source's documentation/test/CLI-contract consistency (checked all ten
changed-file hashes and replayed the 112 offline tests); it did not examine
the model receipt or final-outcome prose and is not model-behavior proof.
Before freezing, the parent corrected the short-article `validate --short`
instruction and documented that source/date/original-title overrides belong
to initial `add`, not `finalize`; a copied real CLI rejected a synthetic
120-character summary at the normal range and accepted it with `--short`.

## Round 3 — normal discovery and complete synthetic intake

Same candidate `a0831ad…` / tree `de6dffe…` and skill `559ef1d4…`; the
[parent verification
receipt](reviews/2026-11-11-conversation-verification.json) binds candidate,
skill, harness, eight selected engine modules, protected source and final
artifact hashes (selected-module hashes are not a fingerprint of the entire
Hermes installation); archive Python 3.13.5, probe engine Python 3.14.9.

Containment: each conversation starts from an empty isolated home plus the
exact repository skill, exposed by the normal skills index; the task prompt
supplies only working directory, archive root, scratch directory and tool
availability — no skill name or expected verdict. Native
`skill_view`/`skills_list` remain available. Four bounded tools allow UTF-8
fixture reads, temporary text writes, synthetic extraction, and one
allowlisted archive CLI command per terminal call; generic shell execution,
publication, arbitrary network access and `--force` are unavailable. CLI
fetching uses two explicitly synthetic pages; real CLI subprocesses perform
extraction, draft creation, validation, finalization and indexing. This is
trusted-engine/process-level containment, not an OS sandbox or live-web
capture.

Parent review reproduced and corrected scratch-runner defects before
acceptance (late/unmatched README replies; missing positive-action and
duplicate evidence; unknown terminal attempts in no-save cases; boolean CLI
flags; contradictory fixture paragraph counts; incomplete case inventories),
then reran **49 offline runner tests** and the **15-step self-test**,
including real CLI artifacts. These counts are separate from both model cases
and repository tests.

Failed first run:
[receipt-01](reviews/2026-11-11-conversation-receipt-01.json) — **2/6 action
checks passed, 33 model API calls**, archive unchanged. The adapter registered
a double-wrapped tool schema and returned plain dictionaries instead of
Hermes-compatible JSON strings; the engine rejected replies with
`tool_result_contract`. Some scratch diagnostic files were written by handlers
before their replies were rejected, so the model's claims that tools did
nothing were not accepted as evidence; conservative grading also rejected
unsupported read-only diagnostic commands in one negative case, which is not
proof of an unauthorized archive write. The runner/grader stayed outside the
public implementation. A registry-dispatch and schema check
(six final tool schemas, real dispatch of all four bounded tools) failed
before the two adapter-contract repairs and passed afterward. Neither the
canonical skill, model, user messages, budget nor acceptance policy changed to
obtain the rerun; no failed case was removed.

Accepted rerun:
[receipt-02](reviews/2026-11-11-conversation-receipt-02.json) — **6/6 passes
across 40 API calls** on `opencode-go` / `deepseek-v4.1-flash`; all six cases
independently invoked native `skill_view` and received the exact candidate
skill.

| Case | Verified outcome |
| --- | --- |
| `zh-save-new` | Complete README reply before mutation; real `add → validate → finalize → index → audit`; Chinese title/H1, original title and raw snapshot preserved; **210**-character Chinese summary; an unsupported read-only `ls` preflight refused, then the supported route succeeded |
| `en-save-new-cross-project` | Same full pipeline via the archive's absolute CLI from another cwd; **213**-character Chinese summary |
| `archive-duplicate` | Actual `add` returned duplicate evidence for the existing URL; no extra entry/state record; existing entry bytes unchanged |
| `quoted-example-no-save`, `summary-only-no-save`, `bare-url-no-save` | No write attempt or archive change (`summary-only` did synthetic extraction and a Chinese summary only) |

For both new-save cases, the accepted conversation receipt retains the summary,
`summary_chars`, and `validate_output` under `cases[].archive_checks.details`;
the reported counts can be recomputed with `count_summary_chars` in
`scripts/common.py`.

Parent reconciliation: raw calls and observed events reconciled by arguments
and multiplicity; README delivery checked in a prior model turn; both entries
and summaries reviewed; alternate-cwd and negative-case scratch files verified
unchanged; the real summary validator and audit rerun. Failed short summaries
were rewritten and revalidated before finalization (three validation attempts
for Alpha, two for Beta), not forced or truncated. Final archive: **two
entries, two index rows and two seen records**, two raw snapshots, **zero
audit issues**. Isolated-home framework files, including usage records and
logs, are retained in inventories and not misrepresented as archive mutations.

This bounded workflow is accepted for merging the upstream changes. It does
not test live sites, images, blocked-fetch fallback, messaging, cron,
publication, installation into the daily-use profile, all paraphrases,
repeated-run reliability, or other models. The Round 2 reply-quality defect
is not proven fixed by these accepted negative replies, which did not invite
saving after an explicit refusal. No private instance, installed archive
skill, cron, or publication target was changed.

## Reproduction and limits

Commands and environment variables are documented in
[tests/README.md](../tests/README.md). Default tests require no model
credentials; the opt-in probe consumes the explicitly selected provider's
allowance. Receipts bind the model/provider, skill, harness, adapter and
selected Hermes source hashes; failed and incomplete cases are retained.
Local verification is not hosted CI. A passing loaded-skill run is not a
universal model guarantee, automatic skill discovery, live acquisition,
gateway acceptance, or proof of complete editorial intake. No private
archive, installed skill, cron, publication target, or Hermes configuration
is changed by this work.