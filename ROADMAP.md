# Roadmap

The single release-scope and status roadmap for Content Archive Kit. It records
existing evidence, remaining gaps, and completion criteria without dates. Usage
lives in [README.md](README.md), commands in [tests/README.md](tests/README.md),
and experiment evidence in `docs/`. Original receipts under `docs/reviews/`
remain unchanged; maintained acceptance summaries explain their scope. This
roadmap points to evidence rather than replacing it.

## Status snapshot

- Early-stage toolkit under active development. **v0.1.0** is published as an
  early GitHub Pre-release, not a stable release, from commit
  `c383f1ae2bab39e4da871ead8baae992b5804c1e`. Its annotated tag, prerelease
  metadata and three downloaded assets have been read back and verified.
  Exact CI and artifact bindings are in [release notes](docs/releases/v0.1.0.md).
- Optional pre-archive subscription selection is the next priority. The
  [implementation contract](docs/subscription-selection.md) separates content
  judgments, execution state and coverage. It is not part of v0.1.0; isolated
  validation and separate private-instance activation approval are required.
- The public tree was created by migrating a frozen, accepted reference
  implementation; provenance and acceptance boundaries are recorded in
  [docs/migration-baseline.json](docs/migration-baseline.json) and
  [docs/acceptance.md](docs/acceptance.md).
- MIT licensing and Linux-only support (macOS removed from the support and CI
  scope by an explicit maintainer decision; native Windows unsupported) are
  settled decisions, not open questions.

## Implemented and evidenced

Everything below is bounded by its linked record; the record, not this list, is
the authority on what was verified and what was skipped or failed.

- Markdown-first CLI workflow — intake with URL canonicalization, deduplication,
  and failure visibility; editorial finalize (Chinese display title and
  validated summary, original title preserved); stable categories, collections,
  and tags; rebuildable indexes; full-text search; audits; soft deletion;
  RSS-first watched-source collection with explicit retry state and honest
  digests. Evidence: [initial migration acceptance](docs/acceptance.md).
- Local reader and reader-experience work — inline-emphasis normalization,
  archived/published ordering, conservative caption recognition, candidate-only
  `format-backfill plan|prepare`, and export-capacity reporting. Evidence and
  remaining converter/browser limits: [reader experience](docs/reader-experience.md).
- Encrypted-reader media prefetch — visible-first scheduling, bounded prefetch
  window, concurrent fetch/decrypt jobs, cancellation on switch or lock, with
  synthetic real-Chromium checks. Evidence:
  [reader media acceptance](docs/reader-media-acceptance.md).
- One canonical Hermes skill — explicit `Save:` / `Archive:` / `收藏：` intake
  commands, no-save counter-triggers, CLI-owned draft creation, and instance
  README delivery before dependent operations. Bounded probe and conversation
  evidence exists: the later conversation run exercised normal discovery and
  real synthetic-fixture CLI intake. The preloaded-skill probe alone does not
  establish discovery, and neither run guarantees live-fetch behavior or full
  response-policy compliance; the reply-quality defect remains disclosed.
  Evidence: [behavior acceptance](docs/behavior-acceptance.md).
- Validation-only CI — Ubuntu/Python 3.13 workflow with read-only permissions,
  no release or deployment step; repository integrity, syntax, and disabled-
  example checks shared with local use. Evidence:
  [CI readiness acceptance](docs/ci-acceptance.md).

## First-release (v0.1.0) completion criteria

The v0.1.0 release required every item below to hold for one candidate commit. No
item, and no subset, substitutes for the whole; a green suite alone is not a
release.

1. **New-instance acceptance (synthetic):** `add → finalize → index → search →
   audit` completes on a fresh archive, a repeated URL returns duplicate status
   without a second entry, and acquisition failure remains visible rather than
   creating fabricated content or a false-success digest.
2. **Existing-instance preservation:** searches, updates, rebuilds, and audits
   on a copied existing archive leave unrelated entry Markdown, snapshots, media
   bytes, and identity/path/first-seen state unchanged; forced replacement
   retains the old entry in trash.
3. **Bounded skill evidence:** within the documented probe and conversation
   harness limits, discovery, intake authorization, cross-project routing, and
   no-save counter-triggers are demonstrated; failures and skipped cases remain
   visible and are not reported as passes.
4. **Boundary integrity:** the public tree and the actual exported artifact
   contain no private articles, `.state` data, credentials, recipients, or
   machine-local configuration; optional publication stays disabled until
   explicitly configured.
5. **Release-procedure gates:** complete local dependency, syntax, example-
   configuration, public-tree, and unittest checks; manifest regenerated only
   through the documented maintenance command after staging; a successful
   hosted Ubuntu CI run for the exact candidate commit; and export/installation
   verification of the actual artifact per
   [docs/releasing.md](docs/releasing.md).
6. **Separate approvals:** tag, GitHub Release, asset upload, and any private-
   instance installation happen only after the above, as individually approved
   operations. None is implied by documentation, local tests, or a push.

The published source commit, hosted CI and verified distribution are bound in
the [release notes](docs/releases/v0.1.0.md). The three assets were downloaded
after publication and compared byte-for-byte with the accepted local artifacts.
Historical model evidence still applies only to its recorded skill/harness/engine
configuration, not to every installation. A release does not approve a private-
instance upgrade, and its tests do not certify later subscription-selection code.

## Next priority: optional subscription selection

Implement the approved [selection contract](docs/subscription-selection.md):
strict opt-in policy, metadata/body evidence, accept/reject/defer decisions,
independent technical failures, separate budgets, durable queue reconciliation,
bounded review/retention and manual-save compatibility. Keep instance preferences
out of generic code. Freeze the candidate before isolated calibration and held-out
evaluation; report program correctness separately from semantic reliability.
Only after those gates may a separately approved instance upgrade enable it.

## Later work (not v0.1.0 promises)

- Core/publication dependency separation (the publication requirements file
  currently overlaps the base requirements).
- Optional, separately opt-in HTML index generation (generation is currently
  bundled with `bin/ca index`).
- Generalization of source, publication, and delivery adapters/channels beyond
  currently verified paths.
- Ongoing code and test maintenance, including dependency updates.
- Cache, sharding, and object-store changes for large archives.

These later directions follow the subscription-selection priority; no dates or
release assignments are promised by listing them here.

## 中文摘要

历史模型证据只适用于其记录的技能、测试装置与引擎配置；发布验收须记录精确提交 SHA、CI 运行及发行包校验值，不能外推历史通过结果。

本文件是项目唯一的发布范围与状态路线图，不承诺日期。**v0.1.0 已作为早期 Pre-release 发布**，不是稳定版；精确提交、CI 和三项附件的回读核验见发布说明。该版本不包含研究／重大性筛选，也不代表私人实例已经升级。下一阶段优先完成可选订阅筛选，先做合成程序验收，再冻结候选并做隔离校准与未见样本评估，最后单独批准实例启用。MIT 许可与仅支持 Linux 的平台范围不变。依赖拆分、可选 HTML 索引、适配器泛化、缓存和对象存储属于更后续方向，不构成当前发布承诺。
