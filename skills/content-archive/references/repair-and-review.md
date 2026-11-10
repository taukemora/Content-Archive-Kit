# Repair and Review

Use when an existing archive needs more than a one-line fix, especially when the user asks for phases or multiple agents.

## Gate order

Use the user's approved batch order; the table below is a starting template, not a reason to postpone a reproduced data-loss or security defect. Each phase gets its own spec and exit check:

| Phase | Goal | Exit check |
|---|---|---|
| 1. Trust / correctness | partial visibility, digest counts, gate failures, no silent data loss | audit, digest, stale/failed gate test |
| 2. Usability / docs | audit depth, search, index views, delivery docs, cross-document consistency | search/index/digest plus document checks |
| 3. Hardening | source visibility, watermark/quota, undated items, SSRF/path limits, atomic finalize, seen repair, storage caps | sandbox probes + full regression |

## Spec template

Write one file per phase, in the instance's documentation directory:

- **Scope and non-goals.** Name what this phase will not fix so agents do not expand it.
- **Owner files.** One owner per file; list read-only files separately.
- **Frozen interfaces.** Run-JSON fields, CLI flags, and function signatures both sides must honour. If an adapter emits `date_source`, `published_unknown`, `raw_file`, or `content_file`, the CLI and the scheduled prompt must accept and pass them; test one candidate end to end, not just the JSON.
- **Data invariants.** Entry count, index rows, seen keys, and existing summaries/images untouched.
- **Acceptance commands.** Exact commands and expected output, including negative tests.
- **Do-not-touch list.** Scheduler job, live library, existing entries, unrelated documentation.

## Subagent protocol

- The main agent writes the spec and acceptance tests; implementation agents edit only their owner files and run only their owned test files while siblings are active.
- Read-only reviews may run in parallel on the same snapshot. When two models cross-check, give both the **same complete input**; different samples invalidate the comparison.
- A subagent's "passed" is a claim. Re-run the commands yourself — including at least one unstubbed end-to-end run that adds a new item — before accepting. Reviewer findings are claims too: check each alleged defect against the actual run JSON and index before changing digest or selection semantics; a manually archived item can look like a run addition.
- Parallel edits require disjoint file ownership. Never let two agents edit one file, and never run a data migration while code that reads that data is being changed.
- After any subagent run, check that entries, index rows, and seen records agree and that every recorded path resolves. A probe that resolves the real archive root can rewrite dedupe state even when it looks like a sandbox test.

## Sandbox rules

- Copy the archive to a scratch directory (`<scratch>/<phase>`) and run destructive tests there, or point the toolkit's root variable at a temporary directory.
- Create `<scratch>/.state/seen.json` when testing dedupe updates. Ensure finalize resolves `.state` from the entry's owning archive root rather than falling back to the live root, and ensure the file-path allowlist checks the containing archive before any outer scratch allowlist — otherwise an instance config file inside the copy is wrongly accepted.
- Never run `add`, `finalize`, `daily start`, or `reconcile-seen` against the live archive as a negative test.
- Keep the real `library/`, `.state/seen.json`, and `.state/sources.json` read-only during review.
- If a probe must touch live state, snapshot exact before/after counts and hashes first, and restore only after verifying the diff.

## Verification commands

Use the instance virtualenv with `-B` (not a system interpreter when they differ), and rebuild the derived index before auditing alignment:

```bash
.venv/bin/python -B -m py_compile scripts/*.py tests/*.py
.venv/bin/python -B -m unittest discover -s tests -v
bin/ca index
bin/ca audit
bin/ca digest --run .state/runs/latest-fetch.json
bin/ca daily finish
find library -name entry.md | wc -l
```

For count and path parity without `jq`:

```bash
.venv/bin/python -B - <<'PY'
import csv, json
from pathlib import Path
entries = list(Path('library').rglob('entry.md'))
rows = list(csv.DictReader(open('index.csv', encoding='utf-8-sig')))
seen = json.load(open('.state/seen.json'))['entries']
print(len(entries), len(rows), len(seen))
assert len(entries) == len(rows) == len(seen)
assert all(Path(v['path']).exists() for v in seen.values())
PY
```

## Acquisition-output acceptance

1. Validate the output path and reject an existing output before network access or error-metadata writes; an early failure must not overwrite evidence from a successful earlier acquisition.
2. Stage raw response, extracted text, and metadata together, recording URL, provider, requested limit, and hashes. Publish with same-filesystem replacement and write the success metadata last as a commit marker; preserve replaced evidence in quarantine and restore it on a handled write failure.
3. Inject failure between writes and verify both a clean first run and a forced replacement of an existing result. Distinguish handled-exception recovery from process-kill or power-loss durability; per-file `os.replace` does not make a multi-file transaction crash-atomic.
4. Exercise a real retrieved article through isolated `add → finalize → index → audit`; provider success alone does not prove downstream compatibility. Keep `completeness: unverified` in acquisition metadata and decide archive status only after checking sections and the actual ending.

## Retry-state and evidence lifecycle

1. **Define selection separately from retention.** Use `seen` for archive dedupe and a bounded lookback for fresh discovery. Keep selected work durable until it is seen, not just quota leftovers. Test same-timestamp late arrivals, older in-window articles, and the exact date-only boundary.
2. **Preserve first enqueue time across every writer.** Inspect both the fetch commit and the failure-parking code; lock-held writes can still reset TTL when they replace whole records. Merge metadata and keep original content/raw references and unknown top-level fields. Test repeated failure registration without advancing the enqueue time.
3. **Persist evidence before publishing a retry record.** Save both selected and quota-deferred docs bodies and raw snapshots, then put usable `content_file`, `cache_name`, and `raw_file` into pending. On reconstruction, retain existing cache names rather than reading extraction-private fields. Validate any legacy filename before deriving a path; report missing files instead of inventing content.
4. **Preserve referenced bytes through changing pages.** Give different page or body versions distinct identities and never overwrite an older referenced snapshot merely because both were fetched on the same day. Reuse identical content. Check cache and raw bytes, not just path existence.
5. **Protect all pending references during cleanup.** Re-read and merge under the state lock, including failed, empty, unselected, and concurrently updated sources; do network work outside the lock. Soft-delete only eligible unreferenced evidence. Check save-to-commit and commit-to-cleanup interleavings separately; a sequential probe does not establish all concurrent guarantees.
6. **Apply deletions only to unchanged baseline records.** Compare the full record or a robust revision, not a second-resolution timestamp plus reason, because another writer can update its payload in the same second. Use deterministic overlap between two processes to prove neither pending additions nor other-source state is lost.
7. **Test the complete retry lifecycle with an isolated local HTTP fixture.** Expose a docs section, select or defer it without archiving, remove it from the next page, fail the source, then restore the page without that section. Require the pending retry to keep a readable body and its original raw throughout. Add a downstream archive invocation to prove the stored paths are accepted, not merely present in JSON.
8. **Distinguish exit events from net queue shrinkage.** An expired record may exit while the same URL, still unseen and inside fresh coverage, is discovered again. Test and report both events; do not claim the URL was permanently discarded or that the queue necessarily shrank. A truly out-of-window article must not be re-queued through fresh discovery.

## Editorial and status integration

- Review proposed summaries against that entry's full text, not only a writer's support quotes. Cross-article numbers and product details can look plausible but are not evidence for the current entry. A reviewer's claimed absence also needs a full-text check: a later paragraph may explicitly support a capability omitted from the first matching quote. Distinguish sample units and detected fraction from relative increase; validate counts again after a factual edit. Keep source publication strings and page-display dates separate from timezone conversion; never "repair" a legitimate cross-day conversion.
- Keep archive, per-channel delivery, and overall states distinct. A successful archive survives a delivery failure; a failure-notification receipt does not turn a failed archive green. Test actual CLI retries and require the already-succeeded channel to be skipped. A dry run must not claim a newly successful delivery or write state.
- Check diagnostics end to end: adapter rejections in JSON do not satisfy user-visible coverage if the digest drops them. Count all new entries before display slicing, retain omitted partials in the coverage section, and describe pending exits as lifecycle events rather than net shrinkage.
- Freeze and hash the implementation before the final full-suite run; if hashes change during execution, rerun after writers stop. A green narrow suite is not stable full-tree evidence.

## Regression-test maintenance

- Replace obsolete assertions only when an approved behaviour change explains them; preserve the original intent with stronger checks. When selected work becomes durable, assert both the awaiting-archive URL set and the quota-deferred set rather than merely changing a total.
- Use isolated deterministic fixtures for exact digest counts. A test hard-coded to today's production count fails when the archive grows; updating the expected number to today's value is not a repair. Keep production dry runs read-only and assert invariants instead.
- Match tests to the claimed boundary: readable retained files, accepted downstream paths, unchanged raw bytes, and state parity. A JSON field or a green aggregate count alone does not prove these properties.

## Negative tests worth writing

- **Stale or unaccounted gate:** an old `started_at`, a mismatched `fetch_generated_at`, or a selected candidate neither in `seen.json` nor registered as failed must make `daily finish` fail with the toolkit's failure marker, parking unaccounted candidates in pending state.
- **Duplicate race:** force two workers past the initial dedupe check, then commit one before the other rechecks; require the winner's entry/raw/images and seen path to survive, and require the loser to clean up only its exclusively reserved directory.
- **Media text injection:** embed entity-encoded HTML, Markdown image/link syntax, and autolinks in chart rows, aria labels, and component context; inspect the generated entry body on both the direct and supplied-content paths, not just the sanitized SVG bytes.
- **Corrupt seen:** truncate `seen.json`; loading must recover from `.bak` or fail loudly, never return an empty map.
- **Code fence and body H1:** put `# comment` inside a fenced block and `# Inner Section` outside; intake must demote the inner section to `## Inner Section` while keeping the comment, and audit/cleanup must leave fenced comments intact.
- **Markdown tail:** a complete article ending in `[text](url)` or `![alt](path)` must not be classified as truncated.
- **Source empty:** a 200 response with zero parsed items must be `empty`, not `ok`, and must not advance the watermark.
- **Undated or future-dated item:** undated items must appear with `date_source: fetched` and `published_unknown: true`, and neither fallback dates nor future card dates may advance the watermark loader.
- **Quota and lookback:** selecting with a cap must report deferrals and retain selected work; a source with a valid watermark must select unseen in-window items older than the guard window even when their timestamps equal or precede the watermark. Verify out-of-window exclusions separately.
- **SSRF and path limits:** `127.0.0.1`, `169.254.169.254`, `fec0::1`, private ranges, `--content-file /etc/hostname`, and the instance `sources.yaml` (even inside a scratch copy) must be rejected.
- **Finalize inputs:** `--path /etc/passwd`, `--summary-file /etc/hostname`, and `--subcategory ../../evil` must all be rejected without changing the entry; `--subcategory` is metadata, not a path segment.
- **Streaming image cap:** an oversized response over a real loopback socket (without mocking the fetch helper) must be aborted before buffering the whole payload, confirming the response is streamed and its encoding is never probed.

## Exit criteria

- Every phase acceptance command was rerun by the main agent, not only by the implementer.
- No P0/P1 findings remain; P2 findings are recorded as follow-ups.
- README, AGENTS, the skill references, and the scheduled prompt agree on counts, gates, and terminology.
- The live archive still has the same entry count and intact summaries, images, and raw files.
