# Changelog / 更新记录

## Unreleased / 未发布

Optional pre-archive subscription selection is under development, separately from v0.1.0. See its [design and implementation status](docs/subscription-selection.md); no private-instance activation is implied.

可选的入库前订阅筛选正在独立开发，不属于 v0.1.0；详见[设计与实现状态](docs/subscription-selection.md)，不代表私人实例已经启用。

- Canonical skill candidate 2.1.0 documents optional metadata/body screening, separate editorial and execution outcomes, and the independent calibration/activation gates. The installed skill is unchanged; written guidance is not model-behavior acceptance.
- 技能原件候选 2.1.0 补充元数据／正文筛选流程，区分编辑性取舍与执行状态，并保留隔离校准及独立启用闸门；未更新已安装技能，文档规则不等于模型行为已验收。

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
