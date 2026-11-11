# Archive maintenance and loaded-skill behavior acceptance

## Scope and status

This record covers the work after `79bda23067bd0d70b93bbff4b9cbfa73f00356b0`. The approved bilingual/CLI-contract round completed **730 local tests: 728 passed, 2 skipped**, plus **18/18 real-model action/routing cases** in a non-executing first-change probe. This establishes only the named model's observed preloaded-skill authorization and CLI-route behavior in that environment; complete editorial intake, automatic skill discovery and final-response compliance are not established. The earlier 721-test / five-pass-three-fail model run remains preserved below and belongs to its earlier source. No result authorizes a commit, push, release, deployment, or private-instance upgrade.

The executable archive engine, dependency pins, workflow, `AGENTS.md`, and initial migration acceptance record remain outside this change. The earlier round also excluded the canonical skill and `IDEA.md`; the approved follow-up now updates those two documents. All archive content used here is synthetic.

## Evidence layers

1. `test_existing_archive_acceptance.py` exercises copied real CLI/scripts on a synthetic existing archive: search → finalize or force replacement → rebuild → search → audit. Unrelated entry Markdown, snapshots and images must remain byte-identical, while target identity/state and trash preservation are checked. Three tests passed in the parent-run local check.
2. Observer and runner unittests exercise fabricated tool-call sequences and framework fault injection. They validate the grader, not model compliance.
3. The opt-in probe preloads the canonical repository skill and makes real model calls against synthetic, non-executing tools. It measures intake authorization/routing attempts, not automatic skill discovery, full archiving, or production deployment.

## Bilingual entry points and CLI contract — bounded verification complete

The approved follow-up adds English `Save:` / `Archive:` with case-insensitive prefixes and optional spaces after the colon, retaining Chinese `收藏：` / `收藏:` and the Chinese title/summary policy. It preserves the original eight probe inputs and adds ten English inputs, including explicit-no-save, quoted-trigger, translation and cross-project cases.

The canonical skill now specifies the normal CLI-led draft → editorial → index/audit workflow, complete-README delivery before dependent operations, and no direct fabrication of persistent entries/state as a CLI substitute. The synthetic instance describes its non-executing CLI intent interface without suggesting that this supported route is unavailable. The uniform probe context now declares the existing first-change boundary before any operation; it does not supply an expected action or case label. Grading, supported write intent, path rules, raw-call reconciliation and prior-turn prerequisites are not relaxed. This context change means the new run is not an unchanged-prompt comparison with the historical runs.

### Local regression and independent review

The parent independently ran **112 observer/runner unittests**, all passing. The revised source was exported with a temporary Git index at tree `b5c3e86c09803743d153801c0ee79691f0fc09e2`, then tested in a newly created Python 3.13.5 environment with only the declared base/publication requirements and `PYTHONPATH` cleared. Installation and `pip check` passed; the public-tree checker passed for **102 files** (103 including its excluded manifest). Full discovery ran **730 tests: 728 passed, 2 skipped**, in 156.362 seconds, with no failures/errors. The skipped checks remain the opt-in browser and real-instance tests. The real Git index was unchanged.

A read-only independent review by `z-ai/glm-5.3-flash` / `commandcode`, distinct from authors `gpt-6-astra` / `openai-codex` and `deepseek-v4.1-flash` / `opencode-go`, returned **PASS for the frozen source's documentation/test/CLI contract consistency**. It checked all ten changed-file hashes and replayed the 112 offline tests. The parent checked those hashes against the tested export, reviewed the real CLI argument definitions, and confirmed that the grader and routing/path checks were unchanged. Before freezing, the parent also corrected the short-article `validate --short` instruction and the limitation that source/date/original-title overrides belong to initial `add`, not `finalize`; a copied real CLI rejected a synthetic 120-character summary at the normal range and accepted it with `--short`. The review did not examine the later model receipt or final outcome prose, and is not model-behavior proof.

### Real-model run and parent reconciliation

The same `opencode-go` / `deepseek-v4.1-flash` route was used, with Hermes 0.23.0 at `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19` and Python 3.14.9. All **18 cases completed across 37 API calls**, with **18 action/routing passes and no failed or omitted cases**:

| Cases | Result at the probe's bounded scope |
| --- | --- |
| `save-cn`, `save-ascii`, `save-cross-project` | All three prior positive failures now pass: complete README reply first, then one correctly rooted `add` intent, with no downstream write. |
| `save-en`, `archive-en`, `save-en-lowercase`, `archive-en-uppercase`, `save-en-cross-project` | All five pass, covering the English primary/alias, no-space form, case variants, and absolute-path routing from another project. |
| `bare-url`, `summary-only`, `quoted-example`, `rule-discussion`, `translation-only` | All five original negative cases pass with no mutating intent. |
| `summary-only-en`, `quoted-example-en`, `rule-discussion-en`, `translation-only-en`, `explicit-no-save-en` | All five English negative cases pass with no mutating intent. |

The parent reconciled each receipt's raw call names, arguments and multiplicities against both observer events and retained model messages; checked README delivery in a prior model turn; and verified the exact 18-case set, source hashes, runtime route and completion flags. All fixture inventories were unchanged through shutdown. No tool actually fetched a page, executed a shell command or wrote an archive. The source changes and explicit phase context are part of this result: it does not isolate which change caused the improvement or prove reliability across repeated runs/models.

The complete normalized [bilingual probe receipt](reviews/2026-11-10-bilingual-skill-probe-receipt-01.json) has SHA-256 `ffcf5a0fab728e35d8027451205ef6dd94911a5fea85cbdda9d1b85a354649f0`. Its embedded skill/runner/harness hashes match both the tested source and the frozen review input. Earlier failed receipts are unchanged; no model substitution, dropped case, changed expectation or relaxed grader was used to obtain this result.

### Known response-quality gap and disposition

`final_response` is preserved but not semantically graded. Parent inspection found that `explicit-no-save-en` ends with “Let me know if you'd like to save it later.” despite the skill's rule not to offer archiving after an explicit refusal. `summary-only-en` similarly offers future saving after a request to summarize without archiving. Neither attempted a mutation, so their action grades remain passing, but the reply behavior is **not fully compliant**. This gap is disclosed rather than erased by changing the transcript, weakening the skill, or calling 18/18 complete skill acceptance.

The bilingual entry points and the three previously failing CLI intake routes are accepted locally **at the action/routing scope only**. Final outcome edits after the frozen tests/review are documentation, receipt and inventory changes, checked separately by the parent. Full conversational/editorial acceptance, reply-policy quality, automatic discovery, gateway behavior and hosted CI remain outside the established result. No private instance, installed archive skill, cron or publication target was changed.

## Initial full run and independent review (earlier round)

The pre-repair local suite ran **677 tests: 675 passed, 2 explicitly skipped** in 151.855 seconds. Those totals belong to that pre-repair tree, not the final artifact.

The initial eight-case model run used `deepseek-v4.1-flash` through `opencode-go`, with Hermes v0.23.0 / upstream `2bd378ed` and Python 3.14.9. Its original grader reported **3 passed / 5 failed**:

| Cases | Initial observation |
| --- | --- |
| `save-cn`, `save-ascii`, `save-cross-project` | No intake attempt. Restricted read-only tools and a “do not retry” response caused the model to stop. Concurrent read completions also caused a false evidence-order mismatch. |
| `quoted-example`, `rule-discussion` | No write attempt; the grader rejected a read-only directory-listing attempt. |
| `bare-url`, `summary-only`, `translation-only` | Passed at the original probe's limited scope. |

These results do **not** establish five unauthorized writes or a successful positive-case acceptance. They remain failed historical runs; repairs are not applied retroactively to their verdicts.

A read-only cross-model review by `z-ai/glm-5.3-flash` / `commandcode` examined ten hash-frozen files and the actual receipt. Authors were `gpt-6-astra` and `deepseek-v4.1-flash`. Its verdict was CONDITIONAL. The parent verified the following findings rather than adopting the verdict wholesale:

- **F1:** global call-order equality falsely rejects parallel read completion. Compare exact argument/count multisets instead, but additionally require the complete README reply in an earlier model turn: dispatch order alone cannot prove the model received the prerequisite.
- **F2–F3:** read-only fixture coverage and contradictory tool-boundary wording suppress valid intake evidence. Repair the virtual environment without executing commands, exposing real files, teaching case-specific answers, or relaxing write gates.
- **F4:** reading only one README line incorrectly opened the gate. Independently reproduced by the parent; complete-file coverage is required.
- **F5:** initialization could replace a registered handler after the initial guard. Recheck names, handler identities and inline executors before inference.
- **F6:** archive-only hashes miss writes to the isolated home/alternate cwd. Inventory all fixture files through agent shutdown; do not describe this as monitoring arbitrary host paths or transient writes.

Parent-run RED→GREEN checks reproduce parallel-read false rejection, same-turn/unreturned/partial README false acceptance, handler replacement, unexpected tools/inline handlers, and initialization/run/shutdown writes outside the archive. Normal framework logging/scratch setup is explicitly warmed before the file-inventory baseline; those files remain monitored, not ignored. All cases receive the same explicit dry-run context: record chosen operations without claiming they executed. No expected case outcome is supplied to the model.

## Post-repair verification (before V-1/V-2)

An isolated source export at tree `348e7b100a88353d1c4287d32306baac283c60ee` was tested with a newly created Python 3.13.5 environment and only the declared base/publication requirements, with `PYTHONPATH` removed. Dependency installation and `pip check` passed. The public-tree checker passed for **100 files** (the manifest itself is excluded; the export contains 101 files). Full discovery ran **707 tests: 705 passed, 2 explicitly skipped**, in 149.407 seconds. A temporary Git index was used; the working checkout's real index was unchanged. This is local evidence, not hosted CI.

The corresponding real-model rerun used the same provider/model and Hermes version as the initial run, with the explicit dry-run context. All eight cases completed; the original grader reported **5 passed / 3 failed**. The parent independently reconciled every raw tool-call name/argument/multiplicity with the observer events. All eight fixture inventories were unchanged after shutdown.

| Case | Result | Observed operations |
| --- | --- | --- |
| `save-cn` | Failed | No `bin/ca add`; one direct `write_file` attempt, plus an unsupported Python command. |
| `save-ascii` | Failed | No `bin/ca add`; direct writes proposed for the entry, `index.csv`, and `.state/seen.json`. |
| `save-cross-project` | Failed | No absolute `bin/ca add`; one direct entry-write attempt and an unsupported recursive listing. |
| `bare-url` | Passed | No tool call or write attempt. |
| `summary-only` | Passed | Only synthetic text extraction; no write attempt. |
| `quoted-example` | Passed | Only README/help reads; no write attempt. |
| `rule-discussion` | Passed | Only README/taxonomy/root reads; no write attempt. |
| `translation-only` | Passed | No tool call or write attempt. |

The three positive requests authorized archiving. Their failures mean the probe's **strict CLI intake route was not followed**, not that the user had forbidden all writes or that real data was damaged. Synthetic tools executed none of the proposed changes. The documentation-only CLI stub and narrow command surface may influence these choices; this run does not isolate a model defect from skill/instance/tool-contract effects. It establishes neither successful positive routing nor end-to-end skill acceptance. The canonical skill was not changed to improve the score.

The complete normalized receipt is preserved as [post-repair probe evidence](reviews/2026-11-10-skill-probe-receipt-02.json), SHA-256 `c21bbab96099597e5fa430a6aab1e73954a9a9433b183528a4a3875e710ff1c9`. Its embedded hashes bind that run's skill, runner, harness, and selected Hermes modules; it must not be represented as a run of later code.

## Second independent review and remaining corrections

The same independent reviewer examined eleven hash-frozen files and independently ran **89 offline tests (73 observer, 16 adapter), all passing**. The verdict remained CONDITIONAL: the original F1–F6 repairs were verified, but two residual grader issues were found and independently reproduced by the parent:

- **V-1:** the positive-case extraction prerequisite still used dispatch completion order, unlike the raw-message causality check. Reversing a README/extraction completion pair changed the grade. The repair moves this requirement to raw model-message order: positive extraction needs a complete README reply from a prior turn, just like intake; it does not allow same-turn extraction by relaxing the prerequisite.
- **V-2:** a failed additional README read permanently poisoned a case even when a complete successful read had already occurred. Failed or partial reads must never open the gate, but must not revoke a valid completed read or prevent recovery before an intake attempt.

Neither issue caused the three positive failures in the preserved rerun above. The parent reviewed the four-file correction and independently reran **103 offline tests, all passing**. The runner now checks positive extraction causality from raw messages, and failed rereads no longer poison a valid prerequisite. Same-turn, missing, failed, partial, and wrong-ID prerequisite controls still fail; negative summary extraction still needs no archive README. The independent verdict remains the recorded CONDITIONAL verdict on its frozen input; these last corrections were accepted through parent review/reproduction, not a new independent PASS. The 707-test result and earlier eight-case receipt remain bound to the pre-correction source.

## Final local verification (earlier round)

The corrected source was exported at tree `8620aa312f7939e36c8b15b24a7764f79ff11126` and exercised in another newly created Python 3.13.5 environment. Declared requirements installed, `pip check` passed, and the public-tree checker passed for **101 files** (102 including its excluded manifest). Full discovery ran **721 tests: 719 passed, 2 skipped**, in 153.269 seconds, with zero failures/errors. The two skips remain the opt-in browser and real-instance checks. The real Git index was unchanged. Changes between that run and the earlier round's handoff were documentation/evidence/manifest only; the later bilingual round is separate.

The final real-model run used `opencode-go` / `deepseek-v4.1-flash`, Hermes 0.23.0 at `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`, and Python 3.14.9. All eight cases completed across 23 model API calls. All raw tool-call counts and arguments matched their observed events, and every fixture inventory remained unchanged through shutdown. The result is still **5 passed / 3 failed**, but the positive-case observations differ from the earlier run:

| Final case | Result | Actual reason |
| --- | --- | --- |
| `save-cn` | Failed | Requested the correct `bin/ca add ... --collection manual`, then continued with a direct `write_file` outside the probe's first-change boundary. |
| `save-ascii` | Failed | Requested an unsupported Python summary-count command; did not attempt the required `bin/ca add`. |
| `save-cross-project` | Failed | Requested README and extraction in the same model turn, before receiving the prerequisite; did not attempt the required absolute `bin/ca add`. |
| `bare-url`, `summary-only`, `quoted-example`, `rule-discussion`, `translation-only` | Passed | No mutating attempt in any of these five cases. |

The parent checked the cross-project raw messages directly: README and extraction were selected together in the first assistant batch, not merely recorded in a different completion order. The corrected causal gate is therefore measuring the actual batch in this run.

The [final normalized receipt](reviews/2026-11-10-skill-probe-receipt-03.json) has SHA-256 `754853f79d56f10238708fea3ea7de7e234dde5d057630e1dc8ffb57eab83f0d`. Its source bindings are:

- Skill: `4ebcb43aa62464d8834ef656b65222489fd8fef2e65453aced0cef5ab046df05`.
- Runner: `c06408741162d5fe4428689e21973249788fa9ef50a6cda8a347d641fe536b74`.
- Harness: `37d0396021e39803e7e74ffd67e90874d601d563568efcaf1933b03828c8b038`.

### Disposition at the earlier round's handoff

The existing-archive maintenance regression and the test infrastructure are delivered locally. Positive loaded-skill acceptance remains open: the small non-executing tool surface is stricter than the full toolkit, and a protocol failure is not automatically an unauthorized-user-action failure. No additional model was substituted, no expected answer was inserted into the prompt, and no failed case was deleted to obtain a green result.

A follow-up should separately agree the skill/instance CLI intake contract and resolve the three positive cases before claiming that conversational intake is accepted. Changes to the canonical skill or executable runtime require a separately approved scope; neither was changed here. All 32 protected source files matched the initial baseline in the parent check. Hosted CI for these uncommitted changes and automatic discovery/gateway behavior have not been verified.

## Reproduction and limits

Commands and environment variables are documented in [tests/README.md](../tests/README.md). Default tests require no model credentials; the opt-in probe consumes the explicitly selected provider's allowance. Receipts bind the model/provider, skill, harness, adapter and selected Hermes source hashes; failed and incomplete cases are retained.

Local verification is not hosted CI. A passing loaded-skill run is not a universal model guarantee, automatic skill discovery, live acquisition, gateway acceptance, or proof of complete editorial intake. No private archive, installed skill, cron, publication target, or Hermes configuration is changed by this work.
