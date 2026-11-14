# Roadmap

The single release-scope and status roadmap for Content Archive Kit. It records
existing evidence, remaining gaps, and completion criteria without dates. Usage
lives in [README.md](README.md), commands in [tests/README.md](tests/README.md),
and experiment evidence in `docs/`. Original receipts under `docs/reviews/`
remain unchanged; maintained acceptance summaries explain their scope. This
roadmap points to evidence rather than replacing it.

## Status snapshot

- Early-stage toolkit under active development. **v0.2.0** is published as an
  early GitHub **Pre-release**, not a stable release, from commit
  `4b3ddcfa376a870b784736c5a7a35edec7370d96`. Its annotated tag, prerelease
  metadata, exact-commit CI, and three downloaded assets have been read back and
  verified in the authoritative public [Release](https://github.com/taukemora/Content-Archive-Kit/releases/tag/v0.2.0);
  local [notes](docs/releases/v0.2.0.md) remain a historical preparation snapshot.
- Optional pre-archive subscription selection and live retry-queue digest
  reporting are published in **v0.2.0**. Selection is opt-in and disabled by
  default. The [implementation contract](docs/subscription-selection.md)
  separates content judgments, execution state, and coverage. Upstream
  publication does not install or activate selection in any existing deployment;
  instance activation requires explicit local configuration and verification.
  Earlier **[v0.1.0](https://github.com/taukemora/Content-Archive-Kit/releases/tag/v0.1.0)**
  (from commit `c383f1ae2bab39e4da871ead8baae992b5804c1e`, [release notes](docs/releases/v0.1.0.md))
  does not include subscription selection.
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

## Published release: v0.2.0 (Early Pre-release)

The [selection contract](docs/subscription-selection.md) is published in v0.2.0
from commit `4b3ddcfa376a870b784736c5a7a35edec7370d96`: strict opt-in policy,
metadata/body evidence, accept/reject/defer decisions, independent technical
failures, separate budgets, durable queue reconciliation, bounded
review/retention, and manual-save compatibility. A digest repair reports the
live retry queue for the latest run — or an overview without `--run` — instead
of a stale fetch-time snapshot: a valid zero omits the line, a missing queue
keeps its snapshot, an unreadable queue is reported as unknown, historical or
unidentifiable runs keep their own snapshot, and rendering never mutates state.
The canonical `content-archive` skill in this tree is at 2.1.0, a skill version
independent of the toolkit release number. The authoritative public
[v0.2.0 Release](https://github.com/taukemora/Content-Archive-Kit/releases/tag/v0.2.0)
binds the exact source commit, CI run, and three verified download assets;
local [notes](docs/releases/v0.2.0.md) remain a historical preparation snapshot.
Engineering tests provide bounded correctness evidence, not editorial or
model-accuracy guarantees. Instance installation and activation remain separately
approved and verified operations.

## Later work (future directions)

- Core/publication dependency separation (the publication requirements file
  currently overlaps the base requirements).
- Optional, separately opt-in HTML index generation (generation is currently
  bundled with `bin/ca index`).
- Generalization of source, publication, and delivery adapters/channels beyond
  currently verified paths.
- Ongoing code and test maintenance, including dependency updates.
- Cache, sharding, and object-store changes for large archives.

These directions are future work not committed to any version; no dates or
release assignments are promised by listing them here.

## 中文摘要

历史模型证据只适用于其记录的技能、测试装置与引擎配置；发布验收须记录精确提交 SHA、CI 运行及发行包校验值，不能外推历史通过结果。

本文件是项目唯一的发布范围与状态路线图，不承诺日期。**v0.2.0 与 v0.1.0 均已作为早期 Pre-release 发布**，不是稳定版。**v0.2.0** 从精确提交 `4b3ddcfa376a870b784736c5a7a35edec7370d96` 发布，其附注标签、预发布元数据、CI 与三项下载附件已在公开 Release 中核验；本地说明文件保留为历史准备快照而非当前权威。该版本发布了可选的入库前订阅筛选（默认关闭、须显式启用）与实时待补队列日报；早期版本 v0.1.0 不包含订阅筛选。上游发布不代表任何既有部署已升级或启用，在实例中启用筛选仍须独立配置并验证；工程测试提供有界的程序正确性证据，不构成编辑判断或模型准确性保证。MIT 许可与仅支持 Linux 的平台范围不变。依赖拆分、可选 HTML 索引、适配器泛化、缓存和对象存储属于后续未来方向，不绑定至任何具体版本，亦不构成发布承诺。
