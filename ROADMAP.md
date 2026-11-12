# Roadmap

The single release-scope and status roadmap for Content Archive Kit. It records
existing evidence, remaining gaps, and completion criteria without dates. Usage
lives in [README.md](README.md), commands in [tests/README.md](tests/README.md),
and experiment evidence in `docs/`. Original receipts under `docs/reviews/`
remain unchanged; maintained acceptance summaries explain their scope. This
roadmap points to evidence rather than replacing it.

## Status snapshot

- Early-stage, pre-release toolkit under active development. No version has been
  released: there is no tag, GitHub Release, or published release asset.
- **v0.1.0** is planned as an early GitHub Pre-release, not a stable release.
  No release candidate commit, verified distribution artifact, or release-bound
  CI evidence exists yet. Preparing documents and running the local suite do not
  create any of these.
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

v0.1.0 is ready only when every item below holds for one candidate commit. No
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

Existing source-level evidence supports items 1–4 only at the scope of the
linked records; it is not clean-distribution acceptance. The remaining work is
to select a final candidate, verify applicable source bindings, rerun affected
checks, and validate its actual exported artifact, including fresh-instance and
existing-instance preservation checks. Historical model evidence applies only
to its recorded skill/harness/engine configuration, not to every installation.
The final-candidate CI, artifact checks and publication approvals in items 5–6
are still pending. Record the exact SHA, Actions run and artifact checksums at
that checkpoint, not an inferred pass from this roadmap.

## Later work (not v0.1.0 promises)

- Core/publication dependency separation (the publication requirements file
  currently overlaps the base requirements).
- Optional, separately opt-in HTML index generation (generation is currently
  bundled with `bin/ca index`).
- Generalization of source, publication, and delivery adapters/channels beyond
  currently verified paths.
- Ongoing code and test maintenance, including dependency updates.
- Cache, sharding, and object-store changes for large archives.

These are directions with a defined intent, not commitments: no dates, owners,
or sequencing are established here, and listing an item does not promise it will
land in any release.

## 中文摘要

历史模型证据只适用于其记录的技能、测试装置与引擎配置；发布验收须记录精确提交 SHA、CI 运行及发行包校验值，不能外推历史通过结果。

本文件是项目唯一的发布范围与状态路线图，不含日期。当前状态：项目处于发布前早期阶段，尚无任何已发布版本；**v0.1.0** 计划作为早期 GitHub Pre-release，尚无候选提交、已验证的发行工件或针对候选的确切 CI 证据。MIT 许可与仅支持 Linux 的平台范围是既定决策。已实现并有验收记录的能力（CLI 收藏/定稿/索引/检索/审计、阅读体验、加密阅读器图片预加载、单一 Hermes 技能、验证性 CI）均以其链接的验收记录为准。v0.1.0 完成标准包括：合成新实例全流程与重复拒绝、既有实例内容保持不变、有边界的技能发现/收藏/拒存证据、公开树与导出工件无私人材料且发布默认禁用、按发布流程完成完整本地检查与精确提交的托管 CI 及工件验证；标签、Release、附件上传与私人实例安装需另行授权。核心/发布依赖拆分、可选 HTML 索引生成、适配器与通道泛化、缓存/分片/对象存储改造属于后续方向，不构成 v0.1.0 承诺。
