# Changelog / 更新记录

## Unreleased / 未发布

The first planned release is **v0.1.0**. No release date, final release commit, tag, or published asset is established by this document. See the [bilingual release-notes draft](docs/releases/v0.1.0.md) and [release procedure](docs/releasing.md).

首个计划版本为 **v0.1.0**。本文不代表已确定发布日期、最终发布提交、标签或已公开的附件。详见[双语发布说明草稿](docs/releases/v0.1.0.md)及[发布流程](docs/releasing.md)。

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

### Reader-experience candidate / 阅读体验候选

- Supported inline CSS emphasis and cross-block emphasis normalization, without guessing headings from short bold paragraphs.
- Archived/published ordering with preserved timestamp precision, unknown-date handling and one validated preference-storage key.
- Candidate-only `format-backfill plan|prepare` CLI; supported caption/heading recovery and marker-only punctuation-boundary repairs preserve source files, with per-entry handling of unreadable entries and damaged snapshots.
- Validated current-export capacity reporting with 75%/90% warnings; existing publication safety limits remain unchanged.
- Frozen local acceptance: 870 tests (869 passed, one production-instance skip), real-Chromium checks enabled; targeted fixes passed focused independent re-review. This is not hosted CI or deployment.
- 边界、验收结果和使用方式见 [reader experience](docs/reader-experience.md)：源格式归一化、归档/发布时间排序、仅候选回填和体积报告；冻结候选完成 870 项测试（869 通过、1 项生产实例测试跳过），真实浏览器检查已启用，补修的异模型聚焦复审通过；不是已发布或已部署的更新。

### Encrypted-reader media prefetch / 加密阅读器图片预加载

- Visible-first scheduling, two viewport-heights of nearby prefetch, and at most three concurrent fetch/decrypt jobs; distant media waits until it enters the window.
- Shared requests for repeated media, paused new downloads in hidden panes, measurable placeholders until decoding completes, and stale-completion protection on switch/lock.
- Synthetic real-Chromium checks cover actual decoded pixels and decode failures separately. Local acceptance and the two-file incremental-upgrade rehearsal are recorded in [reader media acceptance](docs/reader-media-acceptance.md); this is not a deployed update.

- 可见图片优先，邻近上下两屏预加载，最多三路并发抓取与解密；远处媒体进入窗口后再请求。
- 重复媒体共用请求，隐藏阅读区暂停新下载，解码前保留占位，切文或锁定阻止迟到写回。
- 合成真实浏览器测试分别验证像素解码成功与解码失败；[图片验收记录](docs/reader-media-acceptance.md)保留本地验证及双文件增量升级演练，不代表已经部署。

### CI and release preparation / CI 与发布准备

- Python 3.13 validation workflow for Ubuntu, with explicit Node setup, read-only repository permissions, and no release/deployment step.
- Support and release acceptance narrowed to Linux by maintainer decision; historical macOS failures remain recorded, not relabeled as passes. All tests remain in the Ubuntu suite.
- Shared syntax, example-configuration, and public-tree integrity checks, with synthetic failure-path tests.
- Recorded the CLI launcher's executable Git mode so clean checkouts and source exports retain a runnable `bin/ca`; launcher contents are unchanged.
- Documented exact-commit CI gate, artifact export/installation verification, and separate tag, Release, asset, and deployment approvals.

- Ubuntu 上的 Python 3.13 验证工作流，显式配置 Node，仓库权限只读，不含发布或部署步骤。
- 经维护者确认，将支持与发布验收范围收敛为 Linux；保留历史 macOS 失败记录，不改写为通过，全部测试仍在 Ubuntu 套件中运行。
- 本地与 CI 共用的语法、示例配置、公开树完整性检查，以及合成失败路径测试。
- 修正命令行入口的 Git 执行权限，确保新克隆和源码导出保留可运行的 `bin/ca`；入口内容不变。
- 明确精确提交的 CI 门槛、工具包导出与安装验证，以及标签、Release、附件和部署的独立授权。

Verification boundaries and actual results belong to [initial migration acceptance](docs/acceptance.md) and [CI readiness acceptance](docs/ci-acceptance.md). Planned release-artifact verification is not a completed release or production upgrade.

验证边界及实际结果见[初始迁移验收](docs/acceptance.md)与[CI 准备验收](docs/ci-acceptance.md)。计划中的发布包验证不等于已经发布或升级生产实例。
