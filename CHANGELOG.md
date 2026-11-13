# Changelog / 更新记录

## Unreleased / 未发布

The next release candidate, **v0.2.0**, is **not published yet**; see the candidate notes in [docs/releases/v0.2.0.md](docs/releases/v0.2.0.md). The following is implemented on this development branch, separately from published v0.1.0:

下一个候选版本 **v0.2.0** **尚未发布**；候选说明见 [docs/releases/v0.2.0.md](docs/releases/v0.2.0.md)。以下能力已在本开发分支实现，独立于已发布的 v0.1.0：

- Optional pre-archive subscription selection ([contract](docs/subscription-selection.md)): an opt-in, disabled-by-default `selection.yaml` policy (`schema 1`, `enforce` mode only) with metadata/body-evidence `accept`/`reject`/`defer` decisions, per-run judgment and archive budgets, a single structurally validated ledger, bound preview caches, explicit reassessment with bounded attempts and pending-queue capacity refusal, retention/cleanup, admission enforcement before any automatic archive write, and manual-save compatibility — an invalid policy or ledger never blocks a manual save and is never silently repaired.
- Daily reconciliation and digest: each unique candidate is classified once, incomplete accounting fails the checkpoint, and the report distinguishes execution states (`budget_wait`, `judgment_wait`, `execution_expired`). Zero-new reporting keeps historical open work visible. A pending-queue repair reads the live retry queue for the latest fetch run or an overview without `--run`, omits the line on a valid zero, keeps the fetch-time snapshot when the queue file is absent, reports an unreadable queue as unknown, and rendering never mutates state.
- The canonical `content-archive` skill in this tree is at 2.1.0 — a skill version independent of the toolkit release number — documenting the screening workflow and its calibration/activation gates. Written skill guidance is not model-behavior acceptance; upstream changes never automatically install or activate the skill in any deployment.

- 可选的入库前订阅筛选（[契约](docs/subscription-selection.md)）：默认关闭、须显式启用的 `selection.yaml` 策略（`schema 1`，仅 `enforce` 模式），基于元数据／正文证据的 `accept`/`reject`/`defer` 判定、每轮判断与归档预算、单一结构化校验账本、绑定预览缓存、显式重评（有尝试上限与待补队列容量原子拒绝）、保留与清理、任何自动归档写入前的准入校验，以及手动收藏兼容——策略或账本损坏时手动收藏不受阻，且不会被静默修复。
- 日报对账与呈现：每个去重后的候选归入一个结果，对账不完整会使检查点失败，并区分执行状态（`budget_wait`、`judgment_wait`、`execution_expired`）、诚实的零新增报告、历史未结工作可见，以及待补队列修复——日报对最新抓取轮次（或不带 `--run` 的总览）读取实时待补队列，有效零值省略该行，队列文件缺失时保留抓取时快照，不可读队列报告为未知，且渲染不修改任何状态。
- 仓库内 `content-archive` 技能原件版本为 2.1.0——技能版本与工具包发行号相互独立——记录筛选流程及隔离校准与启用闸门。技能文档不等于模型行为已验收；上游变更不会自动安装或启用任何部署中的技能。

## v0.1.0 — Early Pre-release / 早期预发布

Published from `c383f1ae2bab39e4da871ead8baae992b5804c1e`. The [bilingual release notes](docs/releases/v0.1.0.md) bind exact-commit CI and verified download assets. This release does not include research/materiality-based selection.

已从上述精确提交发布；[双语发布说明](docs/releases/v0.1.0.md)记录 CI 与已核验下载附件。本版本不包含研究／重大性自动订阅筛选。

### Initial toolkit / 初始工具包

- Markdown-first CLI for article intake, deduplication, Chinese-title/summary finalization, classification, indexes, search, audits, and daily digests.
- Localized media and available raw snapshots; generic RSS and site-specific HTML/docs source adapters.
- Local reader and optional, explicitly configured encrypted publication.
- One canonical `content-archive` Hermes skill, generic disabled configuration examples, declared dependencies, synthetic tests, bilingual READMEs, and MIT licensing.
- Separation of public source, private archive instances, and publication targets; pinned upgrades rather than automatic deployment.

- 以 Markdown 为事实源的命令行流程：收藏、去重、中文标题与摘要完成、分类、索引、检索、审计和日报。
- 本地媒体、可用时的原始快照、通用 RSS 及特定站点 HTML/文档适配器。
- 本地阅读页，以及须显式配置的可选加密发布。
- 单一 `content-archive` Hermes 技能原件、通用禁用配置示例、依赖声明、合成测试、双语 README 和 MIT 许可证。
- 公开源码、私人实例及发布目标相互分离；采用固定版本升级而非自动部署。

### Caption badges and understated sorting / 图注徽标与轻量排序

- A narrow image-adjacent small white-on-grey caption rule checks each text run's inline style chain; unsupported overrides, non-finite colors and ambiguous sizes are rejected. The existing safe caption-rendering/backfill pipeline remains authoritative.
- One native sorting dropdown now sits at the right of the Browse Articles heading, replacing the hint and prominent two-button row; preference, filtering, selection and reading position are preserved.
- 保守识别紧接图片的小字号白字灰底图注，不跨无关文字拼接样式证据；异常颜色值、覆盖简写和不明确字号不触发新规则。
- 「浏览藏文」标题行右侧改为低调的原生排序下拉，删除原提示与整行双按钮；排序偏好、筛选与阅读位置保持。旧文须先审阅候选差异，不自动回填或上线。行为与限制见 [reader experience](docs/reader-experience.md)。
- The media-browser acceptance probe now observes request-time geometry and uses response barriers for deterministic hidden-pane recovery, avoiding false failures caused by stale placeholder geometry and timing assumptions. Production scheduling is unchanged. Coverage and limits are recorded in [reader experience](docs/reader-experience.md).
- 媒体浏览器验收探针改用请求时刻的真实几何和响应屏障，避免占位布局过期和时序假设造成的误报失败；生产调度器不变。覆盖范围与限制见 [reader experience](docs/reader-experience.md)。

### Reader-experience candidate / 阅读体验候选

- Supported inline CSS emphasis and cross-block emphasis normalization, without guessing headings from short bold paragraphs.
- Archived/published ordering with preserved timestamp precision, unknown-date handling and one validated preference-storage key.
- Candidate-only `format-backfill plan|prepare` CLI; supported caption/heading recovery and marker-only punctuation-boundary repairs preserve source files, with per-entry handling of unreadable entries and damaged snapshots.
- Validated current-export capacity reporting with 75%/90% warnings; existing publication safety limits remain unchanged.
- Boundaries, acceptance coverage and known limits are recorded in [reader experience](docs/reader-experience.md). Existing archives require a separately reviewed upgrade and backfill.
- 边界、验收覆盖和使用方式见 [reader experience](docs/reader-experience.md)；既有收藏库须单独审阅升级与回填。

### Encrypted-reader media prefetch / 加密阅读器图片预加载

- Visible-first scheduling, two viewport-heights of nearby prefetch, and at most three concurrent fetch/decrypt jobs; distant media waits until it enters the window.
- Shared requests for repeated media, paused new downloads in hidden panes, measurable placeholders until decoding completes, and stale-completion protection on switch/lock.
- Synthetic real-Chromium checks cover actual decoded pixels and decode failures separately. Local acceptance and the two-file incremental-upgrade rehearsal are recorded in [reader media acceptance](docs/reader-media-acceptance.md); this is not a deployed update.

- 可见图片优先，邻近上下两屏预加载，最多三路并发抓取与解密；远处媒体进入窗口后再请求。
- 重复媒体共用请求，隐藏阅读区暂停新下载，解码前保留占位，切文或锁定阻止迟到写回。
- 合成真实浏览器测试分别验证像素解码成功与解码失败；[图片验收记录](docs/reader-media-acceptance.md)保留本地验证及双文件增量升级演练，不代表已经部署。

### CI and release preparation / CI 与发布准备

- Python 3.13 validation workflow for Ubuntu, with explicit Node setup, read-only repository permissions, and no release/deployment step.
- Support and release acceptance are narrowed to Linux by maintainer decision; all tests remain in the Ubuntu suite.
- Shared syntax, example-configuration, and public-tree integrity checks, with synthetic failure-path tests.
- Recorded the CLI launcher's executable Git mode so clean checkouts and source exports retain a runnable `bin/ca`; launcher contents are unchanged.
- Documented exact-commit CI gate, artifact export/installation verification, and separate tag, Release, asset, and deployment approvals.

- Ubuntu 上的 Python 3.13 验证工作流，显式配置 Node，仓库权限只读，不含发布或部署步骤。
- 经维护者确认，支持与发布验收范围收敛为 Linux；全部测试仍在 Ubuntu 套件中运行。
- 本地与 CI 共用的语法、示例配置、公开树完整性检查，以及合成失败路径测试。
- 修正命令行入口的 Git 执行权限，确保新克隆和源码导出保留可运行的 `bin/ca`；入口内容不变。
- 明确精确提交的 CI 门槛、工具包导出与安装验证，以及标签、Release、附件和部署的独立授权。

Historical verification boundaries belong to [initial migration acceptance](docs/acceptance.md) and [CI readiness acceptance](docs/ci-acceptance.md); final release bindings belong to the [release notes](docs/releases/v0.1.0.md). Publication is not a production upgrade.

历史验证边界见[初始迁移验收](docs/acceptance.md)与[CI 准备验收](docs/ci-acceptance.md)，最终发布绑定见[发布说明](docs/releases/v0.1.0.md)。上游发布不等于生产实例升级。
