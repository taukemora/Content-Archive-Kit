# Optional subscription selection

Status: published in **v0.2.0**, an early **Pre-release**, as an opt-in,
disabled-by-default feature. The public [Release](https://github.com/taukemora/Content-Archive-Kit/releases/tag/v0.2.0)
records the source commit, CI and verified download assets; the in-tree
[candidate notes](releases/v0.2.0.md) remain a historical preparation snapshot.
Publication does not install or enable selection in an existing archive;
activation requires explicit instance configuration and verification.
Bounded repairs cover current-run
membership, authoritative counter/identity validation, atomic reassessment
capacity, outstanding-work age, evicted-work overflow, seen-proof recovery,
mixed-result digest reporting and live pending-queue counting in the digest.
Not part of published v0.1.0. A frozen candidate completed
a narrow isolated semantic-judgment and CLI-replay experiment; independent review
passed only that experiment's result claims, not product or release acceptance.
See the verification scope below. Manual saves and existing articles are unaffected
while `selection.yaml` is absent or `enabled: false`.

The design boundary is unchanged: the existing Agent makes editorial judgments, and
the toolkit validates and persists them. Keyword matching is not treated as proof of
research value or materiality, and no second model provider is introduced. Rules
belong in the instance's `selection.yaml`; the shipped
[`examples/selection.yaml`](../examples/selection.yaml) is disabled and synthetic.
Article text and feed metadata are untrusted data, never workflow instructions.

## Policy contract (`selection.yaml`, schema 1)

Top-level keys are exactly `schema`, `enabled`, `version`, `mode`, `rules`, `budgets`,
`retention`. Unknown keys, a missing required key, a non-boolean `enabled`, a
boolean/integer type swap, an unknown mode, an empty/duplicate rule list or a
malformed limit raises `SelectionConfigError`. A present-but-invalid file — including
a path that exists but is not a regular file — fails the command visibly; it is never
silently treated as missing/disabled. Rules are criteria the Agent applies; list
order has no keyword or first-match execution semantics.

- `schema` must be the integer `1`.
- `enabled` is a boolean. Missing file or `false` keeps every legacy path unchanged;
  nothing reads or writes the ledger.
- `version` is a positive integer. It is recorded in every decision, and the policy
  content digest (SHA-256 over the normalized policy, including rules, budgets and
  retention) is stored beside it.
- `mode` must be `enforce`. `shadow` is rejected with an explicit error rather than
  silently changing semantics.
- Each rule has a stable slug `id`, `outcome` (`accept` or `reject`), a
  `description`, and `evidence` (`metadata`, `body`, or `either`). A rule cannot
  declare `defer`: deferring is a decision that names an explicit information gap and
  follow-up, not an exclusion keyword.
- Budget defaults: 40 discovered candidates per run, 20 per source, 20 new judgments
  per run, 10 archive admissions per run. Each is a per-run upper bound, never a
  claim of complete coverage.
- Retention defaults: evidence details 30 days, compact terminal identities 180 days
  (required to exceed the 30-day discovery lookback), unresolved review horizon 14
  days, at most 2 explicit reassessment attempts, and a 20,000-record capacity.

Policy content changes are detected by digest. They invalidate reuse of an existing
judgment and are reported as requiring explicit reassessment; they never requeue all
exclusions automatically.

## Commands

All selection state lives in one authoritative ledger,
`.state/selection/ledger.json`, written atomically under the existing
`.state/.lock`. Preview caches live under `.state/selection/previews/`. Commands emit
JSON.

```text
bin/ca select status
bin/ca select preview (--url URL | --key KEY) [--run FILE] [--content-file FILE] [--dry-run]
bin/ca select decide --key KEY --outcome accept|reject|defer --reason TEXT
                       --evidence QUOTE [--evidence QUOTE ...]
                       [--rule RULE_ID] [--run FILE] [--gap TEXT --follow-up TEXT]
                       [--dry-run]
bin/ca select ready [--run FILE] [--limit N] [--dry-run]
bin/ca select reassess --key KEY [--run FILE] [--apply] [--reason TEXT]
bin/ca select cleanup [--apply]
```

`--run` defaults to `.state/runs/latest-fetch.json` (the file `daily start` writes).
Dry runs perform the same arithmetic and report the same result without writing.

Exit codes: `0` success (including an idempotent repeat or an accepted decision that
lands in `budget_wait`); `2` missing/disabled policy, invalid policy (including
`select status`, which still prints a machine-readable `policy.status: invalid`
report), or wrong mode; `3` validation failure, stale/foreign/changed run binding,
structurally invalid ledger record, contradictory decision, exhausted judgment
budget, attempt limit, or corrupt ledger; `4` ledger capacity refusal.

### `status`

Read-only. Reports the policy status (`missing`, `disabled`, `enabled`, `invalid`),
version, digest, rules, budgets and retention, plus ledger `records`, outcome
`counts`, execution `states`, `capacity` (`used`/`limit`), `open`, `unresolved`,
`expired_unresolved`, `needs_reassessment`, `open_detail` (per-state open counts,
oldest open age in days, stalled count, review limit, overflow-marked keys) and a
`next_action` hint. An invalid policy still returns this report with
`policy.status: invalid` and exits `2`. It creates no directory and writes no file.

### `preview`

Produces bounded preview text without archiving, localizing media or publishing:

- candidate `content_file` recorded by discovery (a real docs section keeps its
  `#fragment` identity; scope is `section` when the URL has a fragment, otherwise
  `document`);
- an explicit `--content-file` under the existing `.state/`/scratch allowlist and
  5 MB limit;
- otherwise a direct page fetch through the existing safe-fetch redirect/SSRF
  boundary (`fetch_sources.fetch_text`), scope `page`.

Preview text is capped at 20,000 characters; `truncated` says whether text was cut
and `completeness` stays `unknown` — a capped body is not proof of completeness. A
cache file is written only outside `--dry-run`; its name and sidecar bind `key`,
`source_id`, `body_hash`, `policy_digest` and `run_generated_at`. `cache_hit` is true
only when the on-disk bytes still match that binding, so a tampered cache is never
reused as fresh evidence.

The authoritative ledger also records which same-run preview is **current** for a
key (`previews[key]`), updated atomically under `.state/.lock`. `decide` resolves
body evidence only through that record, re-verifies the hash under the mutation
lock, and refuses a body that changed after it was read. A second preview of the
same key therefore supersedes the first; a stale same-run body can no longer
authorize a decision. A recorded preview path outside the previews subtree is
ignored (no cache sidecar may escape the permitted content subtree).

### `decide`

Validates all of the following before writing anything:

1. the exact candidate key (fragment preserved) appears in the current run;
2. the ledger already knows that key (fetch registers discovery; a decision cannot
   create an unknown candidate);
3. the record was registered for this run's `generated_at` (otherwise: stale run);
4. the candidate `source_id` and observed metadata fingerprint still match the
   registered record — a mutated run-file quote/source is refused even when the
   timestamp and key are unchanged, and a record flagged `reassessment_required`
   needs explicit reassessment first;
5. the policy digest still matches (undecided records are rebound by a fetch; decided
   records are not);
6. the judgment budget for this run — counted monotonically, see below — is not
   exhausted;
7. at least one `--evidence` quote occurs in the recorded metadata and/or the
   ledger's current preview body, according to the rule's `evidence`;
8. for accept/reject, `--rule` exists and declares the same outcome; for defer, a
   non-empty `--gap` and `--follow-up` are present and no rule is used.

The ledger record stores outcome, execution state, rule, reason, gap/follow-up,
normalized quotes, an evidence fingerprint, policy digest/version, the discovery run,
the decision run, the admission run, and a bounded history (five entries).
Identical repeats return `status: idempotent` and write nothing; a different
decision on an already-decided key fails and points at `select reassess`.

Accept checks the archive budget for this run: within budget the record becomes
`ready` with an admission timestamp; beyond budget it becomes `budget_wait` and is
reported as such.

Per-run counters live in the one ledger (`runs[generated_at]`, at most the last 30
runs). Judgment and archive usage are monotonic within a run: `decide` increments
before the outcome is written, so reassess/re-decide cannot refund a model-work slot
or an archive admission. An idempotent repeat is free. The counters are
authoritative state: a present boolean, negative, fractional or non-integer counter,
or a run entry that is not a mapping, is corruption and raises instead of being
coerced to zero. Only genuinely absent legacy counter history falls back to the
counts derived from recorded decisions. The ledger `schema` must be the integer `1`
(not `true`), and every record's dictionary key, embedded `key` and canonical
identity of its `url` (fragments preserved) must agree; a foreign or collapsed
identity is rejected on read by `status`, `decide`, `ready`, `reassess` and the
automatic path alike.

`select ready` reports only this run's admissions. A `budget_wait` is promoted only
when the key is in the current run with unchanged evidence/policy and budget
remaining; a prior-run admission is re-admitted under the same checks, otherwise it
is listed under `not_readmitted` with a reason. Current-run membership is required on
every path: a record whose `admitted_run` merely equals the current `generated_at`
but whose identity is no longer in `candidates` is reported as `not_in_current_run`,
never admitted. `count`, `displayed`, `truncated`, `archives_used` and
`archives_limit` are reported honestly. Automatic `add` applies the same renewal
rule on both the renewal and the still-fresh branch — a missing or invalid run cannot
authorize an add, and the refusal happens before any library or media write — so
interrupted accepted work stays retryable only while it is genuinely current.

### `reassess`

Preview-first: without `--apply` it prints the current decision, whether the policy
digest or observed metadata changed, remaining attempts, whether the identity is in
the current run and whether a fresh discovery will be required, and writes nothing.

With `--apply` it increments the attempt counter (maximum 2, re-checked inside the
mutation lock), appends a history entry that preserves the previous
outcome/rule/reason, clears the old outcome and evidence detail, and returns the
record to `awaiting_judgment`. A terminal identity that has disappeared from
`latest-fetch` can still be reopened: the retained verified metadata requeues only
that identity, the run binding is cleared, and a fresh discovery/preview/decision is
required before any admission. Execution-expired undecided work is reassessable for
the same reason; it is not an editorial reject.

The requeue is bounded by the same single pending queue (`fetch_sources.PENDING_LIMIT`).
If the queue is full and the reopened key is not already queued, `--apply` refuses
with exit `4` and an actionable capacity error before any durable write: the ledger,
the queue and the attempt count stay unchanged, and no attempt is consumed. A key
already in the queue may be refreshed without enlarging it. Once room exists the same
explicit operation succeeds normally.

### `cleanup`

Read-only plan by default; `--apply` is required to write. Terminal, non-active
records older than 30 days lose their quote details (the fingerprint and
`detail_expired` remain; expired details cannot be reused as fresh evidence), and
terminal records older than 180 days are removed. Active (open) identities and any
key still present in `pending.json` are protected. Unreferenced preview files older
than 30 days are removed on apply; a preview whose key is active is kept.

On apply every planned decision is recomputed while the state lock is held:
record state, timestamps, pending references and preview files are re-read
immediately before anything is dropped, so a concurrent official reassessment
cannot lose its reopened work and stale plan data cannot delete evidence. Expired
preview bindings are dropped from the ledger at the same time.

## Outcomes, execution states and discovery

Content outcomes are `accept`, `reject` and `defer`. Execution states are
`awaiting_judgment`, `budget_wait`, `judgment_wait`, `ready`, `archive_failure`,
`archived`, `rejected`, `unresolved` (a defer) and `execution_expired`.

- `budget_wait` is an accepted item waiting for archive budget.
- `judgment_wait` is undispatched work waiting because the run's judgment budget was
  genuinely consumed; it is durable, distinct from `budget_wait`, retried in a later
  run, and never a fabricated reject.
- `execution_expired` is a bounded non-editorial pause for undecided queue work whose
  retry life elapsed. It suppresses automatic rediscovery (so an expired identity
  never gets a fresh TTL every run) but can be reopened explicitly with `reassess`.

Reject and defer never write `seen.json` and never create a library entry; they
suppress automatic rediscovery through the ledger. A deferred review whose 14-day
horizon passes remains unresolved (and is reported as expired), never an editorial
reject.

Ledger records are structurally validated on read: a non-mapping record, an unknown
state/outcome, a missing key/url/run binding or a boolean/non-integer counter raises
`SelectionStateError` instead of looking like an empty or rejected state.

`fetch_sources.py` applies the policy only when it is present and enabled:

- discovered candidates are filtered by terminal ledger identities before quota
  selection, and stale pending records for those identities are exited, so a queue
  delta cannot resurrect rejected work;
- the per-source cap is the policy `per_source` budget; the global cap is an explicit
  `--max` when given, otherwise the policy `discovered` budget;
- sources are scheduled round-robin from a rotating start index kept in
  `.state/sources.json` (`rotation`), so a scarce budget does not always favour the
  first configured source (disabled mode keeps the legacy order and no rotation);
- this run's selected candidates are registered as `awaiting_judgment`, together
  with the retained payload (summary/date/extractor/content/raw paths, cache name and
  section identity) that recovered work needs;
- terminal records are re-observed: a changed title/summary marks
  `reassessment_required` and is reported, but the exclusion stays in force; a
  decided-but-open record keeps its original evidence binding and is marked rather
  than refreshed;
- the commit phase re-reads the ledger and `seen.json` under `.state/.lock`, drops
  terminal pending keys, distinguishes deliberate `seen`/`expired`/limit removals
  from crashes, and re-derives queue work (`reason: ledger_recovery`) for **all owed
  open states** — accepted budget waits, admissions and archive failures included —
  without resetting the first enqueue age;
- fresh `seen` state closes every eligible open state — undecided waits, accepted
  `budget_wait`/`ready`/`archive_failure` — as archived queue evidence: the pending
  work is removed, the editorial outcome and bounded history are retained, and stale
  overflow/failure markers are cleared. That is the crash-recovery window between a
  durable seen write and `mark_archived`; a repeated observation is idempotent and
  never appends history or resurrects phantom open work;
- a deliberate pending-TTL expiry marks the record `execution_expired` instead of
  silently dropping or resurrecting it; accepted admissions and `judgment_wait` work
  are never aged out by that TTL;
- the pending bound is re-applied after all recovery and merges. Work that cannot fit
  is not silently deleted: an existing open ledger record — including accepted work
  evicted by the pre-merge limit — keeps its payload and enqueue age and is marked
  `queue_overflow`, reported by `select status` and the digest, and recovered
  automatically by a later run with room (or explicitly by `reassess`). A capacity
  exit for an identity with no ledger record is a visible coverage loss through
  `pending_exits`, not an editorial reject and not a durable accepted obligation;
- `--dry-run` computes the same report and writes no ledger, queue or source state.

## Text preview and automatic archive enforcement

Automatic add (`bin/ca add URL --collection auto`) requires a current accepted
admission when the policy is enabled, checked before any directory, media download
or content write: the record must exist, have outcome `accept`, be `ready` or
`archive_failure` (retrying a technical failure is allowed), match the current policy
digest, carry unchanged observed evidence and not require reassessment. An admission
timestamp older than 7 days — or one bound to a prior run — is renewed only when the
identity is in the current run with unchanged evidence/policy and archive budget
remaining. If the supplied `--content-file` is a recorded preview, its bytes must
still match the current recorded `body_hash`. A refusal prints
`{"status": "blocked", ...}` and exits 1 without touching the library. A present but
invalid policy fails automatic add closed; when the policy cannot be read at all it
does not block a manual save.

After a successful automatic add the record becomes `archived` with seen evidence; a
failure after admission records `archive_failure` with a visible reason and stays
retryable. Manual add (`--collection manual`, the default) keeps legacy behaviour: it
always overrides an automatic exclusion and only appends a bounded `manual_override`
history entry, leaving the editorial outcome intact. An invalid or corrupt policy or
ledger never blocks a manual save; the ledger is left untouched, an
`selection automation warning` is reported if history cannot be updated, and the
corrupt ledger is never silently repaired or reset. A manual save of an undecided
record also closes the automatic queue work (`manual_save`, archived with seen
evidence) without inventing an editorial outcome. Reusing preview content does not
change later full-archive extraction, completeness marking, media localization or
summary validation.

## Daily reconciliation and digest

`daily start` uses the policy `discovered` budget for its fetch cap (it is never
silently capped down to the judgment budget) and stores the fetch selection report in
the gate. `daily finish` classifies every unique candidate of the run into exactly one
accounted outcome:

| kind | meaning | accounted |
| --- | --- | --- |
| `archived` | present in `seen.json` (durable seen/library proof) | yes |
| `rejected` | valid reject decision with evidence | yes |
| `deferred` | valid defer with gap and follow-up; expiry stays unresolved | yes |
| `budget_wait` | accepted, explicitly waiting for archive budget | yes |
| `judgment_wait` | undispatched because the run's judgment budget was consumed | yes |
| `technical_failure` | recorded failure, ledger `archive_failure`, or `fail-candidate` | yes |
| `execution_expired` | bounded non-editorial queue expiry, recoverable via reassess | yes |
| `unjudged` | discovered but no decision, below the judgment limit | no — fails finish |
| `pending_archive` | accepted but never archived and never failed | no — fails finish |
| `archive_unproven` | ledger `archived` without durable seen/library proof | no — incomplete, repair needed |

Unaccounted candidates are parked in the pending queue and fail the run; rejected and
deferred keys are removed from the pending queue instead of being re-offered. Only
when the actual judgment limit has been consumed are remaining undispatched candidates
marked `judgment_wait`, which closes accounting without pretending they were judged;
untouched work below the limit still fails finish. Document-section identities are
traced explicitly: another section's generic URL cannot close a `#fragment`
candidate. The gate records counts, exclusions by rule, unresolved deferrals and the
unaccounted lists. A completed checkpoint means accounting is closed, not that every
page on a site was scanned.

The digest adds a `选题` section when the policy is enabled: per-run counts, exclusions
by rule and reason, unresolved deferrals (gap, follow-up, attempts against the policy
review limit, expiry), technical failures, archive-without-proof items, and a
reassessment notice. It also surfaces **historical open work outside the latest run**
— budget waits, judgment waits, unjudged/pending-archive, technical failures and
execution expiries — with the oldest open age, a stalled count and a next-action hint.
That age is measured from the preserved enqueue/open origin (`queued_at`, else
`first_seen`), not from `updated_at`: an observation refresh or crash recovery only
moves `updated_at`, so old work never looks new again. A deliberate `reassess` keeps
the same origin, so reopening a decision does not reset its outstanding age. Overflow-
marked work is listed separately with its preserved enqueue age. That is why a zero
new-archive count can stay numerically zero without reading as a healthy empty-work
result. When nothing was archived, the digest distinguishes an all-rejected/all-
deferred run, unclosed selection accounting, and a genuine zero-new run; technical
failures never become a zero-news success. Existing delivery/publication status
handling is unchanged: delivery and publication remain optional and absent from this
flow.

Separately, the digest's `待补队列` count reports the live retry queue, not a
fetch-time snapshot: for the latest run — or an overview rendered without
`--run` — it counts the current `pending.json` entries; a valid zero omits the
line instead of replaying a stale positive snapshot; a missing queue file keeps
the snapshot; an unreadable or malformed queue is reported as unknown, never as
zero; and a historical or unidentifiable explicit run keeps its own snapshot.
A run is identified as current by a matching, non-empty `generated_at` in
`.state/runs/latest-fetch.json`, not by the supplied filename.
Rendering the digest never mutates pending, run, seen or ledger state.

## Verification

`tests/test_subscription_selection.py` covers the policy contract, disabled
compatibility, preview/cache binding and SSRF refusal, decisions and evidence,
stale/foreign run rejection, budgets, rotating fairness, suppression, queue crash
recovery, changed-metadata reporting, automatic admission, manual override, daily
reconciliation (all-rejected, omitted judgment, accepted-but-unarchived, technical
failure), reassessment, retention/capacity, cleanup protection and status reporting.
One synthetic end-to-end case drives `daily start` -> decide accept -> automatic add ->
finalize -> `daily finish` (real index rebuild, audit and digest) with publication and
delivery absent.

The repair added focused regressions, all synthetic and loopback-only:

- `tests/test_subscription_binding.py` — changed run source/metadata refused, changed
  accepted observation needs reassessment while unchanged retry still works,
  structural ledger validation, current-preview binding (stale body refused, preview
  subtree containment), machine-readable invalid-policy status and non-regular policy
  path.
- `tests/test_subscription_recovery.py` — reopen after leaving `latest-fetch`,
  attempt bound, accepted `budget_wait` recovery with payload and original enqueue
  age, manual-save queue closure without churn, non-editorial execution expiry
  without TTL resurrection, bounded/explicit pending overflow, and cleanup
  revalidation inside the lock.
- `tests/test_subscription_budget.py` — current-run-only `ready`, budget-wait
  promotion priority, monotonic judgment usage across reassess, idempotent repeats,
  bounded run counters, and discovery-above-judgment accounting as `judgment_wait`
  (with the below-limit failure case and the 40/20 defaults).
- `tests/test_subscription_digest.py` — archived accounting requires seen proof,
  section identity cannot be closed by a generic URL, historical open work never
  reads as a healthy zero-new digest, and the policy review limit is used. Mixed
  editorial/non-editorial results must not be reported as all rejected/deferred;
  all-rejected and all-deferred runs retain their legitimate summary, historical
  outstanding work stays visible, and an empty run cannot satisfy an all-editorial
  claim merely because both counts are zero. The digest's pending-queue count is
  covered end-to-end: current-run digests and overviews rendered without `--run`
  read the live queue, a valid zero omits the line, a missing queue file keeps
  its snapshot, an unreadable queue reports unknown, a historical run keeps its
  own snapshot, and rendering never changes pending, run, seen or ledger bytes.
- `tests/test_subscription_manual.py` — manual duplicate unaffected by an invalid
  policy, automatic add still blocked, corrupt ledger warning without repair, and
  manual closure of undecided automatic work.
- `tests/test_subscription_repair_v2.py` — current-run membership on `ready` and the
  fresh automatic-admission branch (helper and real `add --collection auto` CLI, no
  library write), corrupt per-run counters/schema/record identity failing visibly and
  unwritten while manual saves still warn, atomic full-queue reassessment refusal
  without consuming an attempt, outstanding age from the enqueue origin in status and
  digest, pre-merge eviction overflow marking with later-room recovery, and seen-proof
  closure of accepted ready/budget_wait/archive_failure work with history preserved.

These are synthetic tests; they do not establish editorial accuracy or real-site
coverage.

### Isolated semantic and CLI experiment

A frozen candidate, before the follow-up digest-condition repair, was exercised
with the native Hermes no-tool agent (`openai-codex / gpt-6-astra`): six development
cases and 24 held-out cases. The cases were synthetic and the reference labels
were parent-agent-authored, not user annotations. Reference labels were kept out
of the model input and were not changed to improve agreement.

On the held-out set, literal outcome agreement was **22/24**. Explicitly treating
`fail` and `technical_failure` as equivalent acquisition-failure names gives
**23/24**; these are different comparison rules, not interchangeable scores.
The nine accepted case IDs matched the reference set. The remaining substantive
disagreement, **h07**, was model `reject` versus reference `defer`: both avoid
archiving, but rejection and retained review work have different consequences.
The reference was not declared wrong and the disagreement remains open.

The six development cases' actual model decisions were separately replayed
through the real CLI in throwaway synthetic instances. Acquisition failure stayed
distinct from editorial rejection, and publication/delivery were not requested.
An independent different-model review passed only the reported experiment-result
claims. These results apply to that frozen candidate, not automatically to later
changes. They do not prove autonomous model tool use, automatic skill discovery,
real-source coverage, production accuracy, release readiness or private-instance
compatibility; follow-up product changes need their own engineering regressions.

## Known limitations

- The toolkit does not judge content and does not re-fetch every rejected URL to
  detect remote edits; only changed title/summary observed in a later discovery run is
  reported as needing reassessment.
- Deferred items are not automatically re-checked at the review horizon. Expiry is
  computed when state is read and stays `unresolved`; a human/Agent reassessment is
  still explicit.
- Evidence quotes are substring checks against recorded text. Structural validity is
  not proof that the semantic conclusion is correct.
- The judgment budget is per run: once 20 decisions are recorded, further `decide`
  calls fail until a new run exists. Judgment beyond a run's budget is not silently
  dropped; at `daily finish` the undispatched remainder is recorded as
  `judgment_wait` only after the limit was actually consumed.
- A preview of a fragment URL fetched directly from the page has whole-page scope; only
  the recorded section content keeps section identity.
- The policy is validated but not interpreted for content: an `accept` and a `reject`
  rule with the same semantics remain the author's responsibility.
- `select status` reports the ledger; it does not verify that `library/` and
  `seen.json` still agree — `audit` and `daily finish` remain the parity checks.
- `archived` accounting uses `seen.json` as durable proof; a ledger-only archive flag
  is reported as `archive_unproven` and needs audit/repair. Section candidates need
  their exact `#fragment` identity in `seen.json`.
- Pending overflow is bounded but not resolved by force: a full queue makes explicit
  reassessment refuse atomically, while a later discovery run with available room
  recovers retained open work automatically. Overflow-marked work is visible in
  status/digest with its original enqueue age; capacity exits for identities that were
  never registered stay coverage losses in `pending_exits`, not silent obligations.
- A single deliberate `seen` exit is reported when a manual save closes undecided
  automatic work; only repeated exit/recovery churn is prevented. Durable seen proof
  also closes accepted ready/budget_wait/archive_failure work left open by an
  interrupted `mark_archived`, preserving the editorial outcome and history.
- Local engineering checks and the narrow experiment-result review above are not
  product/release acceptance, real-source calibration or a deployment activation.
- Further model or real-feed evaluation, deployment activation, cron changes and
  publication/delivery targets require separate approval; an upstream implementation
  does not automatically install, activate, or modify any deployment.
