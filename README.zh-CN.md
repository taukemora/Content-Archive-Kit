<p align="right">
  <a href="./README.md">EN</a> | <strong>简</strong>
</p>

<p align="center">
  <img src="./assets/logo.svg" alt="Content Archive Kit 标志" width="96" height="96">
</p>

# Content Archive Kit

[![Python 3.13](https://img.shields.io/badge/python-3.13-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

一套以 Markdown 为事实源的文章收藏库建设工具，提供命令行工作流和一个伞形 Hermes 技能。

Content Archive Kit 将可复用的代码和技能与私人文章、运行状态、凭据及部署配置分离。你可以用它新建收藏库，也可以维护已有收藏库，而不必让 GitHub、数据库或托管服务成为事实源。

## 功能

- 手动收藏文章：URL 规范化、去重、全文、本地图片、可用时的原始快照和来源元数据。可选 PDF 附件须按[论文流程](skills/content-archive/references/paper-archiving.md)作为条目文件另行加入，不是通过 `add` 自动收藏 PDF。
- 中文展示标题和经代码校验的中文摘要，同时保留原文标题。
- 稳定的一级分类、可选二级分类、收藏场景和标签。
- 全文检索、可重建索引、审计和软删除。
- RSS 优先的监视来源采集，内置通用 RSS 适配器和特定站点的 HTML/文档适配器，保留明确的失败重试状态并如实呈现日报。
- 本地阅读页，以及需要显式配置的可选发布适配器。
- 加密阅读器图片预加载：可见图片优先，向前后最多预加载两个阅读区视口高度，最多三路并发抓取/解密，切换文章或锁定时取消调度。
- 一个 Hermes 技能，定义中文 `收藏：<文章链接>` 和英文 `Save: <文章链接>` / `Archive: <文章链接>` 入口，以及裸链接、一次性摘要和引用示例不得触发归档的规则；模型实际行为须另行验证。
- 阅读体验：行内强调源格式归一化、归档/发布时间排序、保守的白字灰底图注识别、仅生成候选副本的 `format-backfill plan|prepare` 回填 CLI，以及发布体积报告（既有发布上限不变）。

## 环境要求

- Linux（CI 使用 Ubuntu）。不支持原生 Windows；Windows 用户须使用 WSL 等 Linux 环境。macOS 不在支持平台和 CI 范围内，不承诺兼容。
- Python 3.13。
- Bash，以及 `requirements.txt` 中声明的依赖。

## 安装

```bash
git clone https://github.com/taukemora/Content-Archive-Kit.git
cd Content-Archive-Kit
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

当前基础安装已经包含阅读器与加密依赖，包括 `markdown-it-py` 和 `cryptography`，并非最小核心依赖集；正文媒体处理也会导入 `markdown-it-py`。独立的发布依赖文件目前与基础依赖重叠，准备加密发布时可显式安装：

```bash
.venv/bin/python -m pip install -r requirements-publish.txt
```

创建收藏库实例时，将 `bin/`、`scripts/` 和配置示例复制到实例根目录，再修改实例内的 `taxonomy.yaml`、`sources.yaml` 和 `media.yaml`。不要直接修改仓库中的示例。每个实例还需要独立的可用解释器：在 `<实例目录>/.venv` 中安装 `requirements.txt`，或者把 `CA_PYTHON` 指向已经安装这些依赖的解释器；不要依赖未经确认的系统 `python`。可选发布默认保持关闭，只有显式配置后才启用。

实例目录、虚拟环境、定时任务、升级和迁移见[部署指南](skills/content-archive/references/deployment.md)。

### 可选行为与验证边界

`bin/ca index` 当前会一起生成 `index.csv`、`index.md` 和本地阅读页 `index.html`，HTML 生成不是单独启用的步骤。托管发布是另一项操作，在实例配置显式启用之前保持关闭。基础收藏不要求托管账户、NAS、消息接收目标或运行中的阅读服务。

发布与投递路径已有合成测试，但这不代表新安装环境的真实浏览器、实际消息投递、NAS 或线上托管已经验收。可选的真实实例测试仅预览日报投递，不是归档搜索与更新验收。各项可选集成应在目标环境验证后再启用。

## 验证

```bash
cd tests
../.venv/bin/python -m unittest discover -s . -t .
```

全部测试使用合成 fixtures。浏览器验收和真实实例 dry run 需要显式开启；完整的本地/CI 检查（包括依赖、语法、示例配置和公开树完整性）见 [tests/README.md](tests/README.md)。

## 发布准备

首个计划版本 **v0.1.0** 拟作为早期 GitHub **Pre-release**，不作为稳定版，目前尚未发布。[ROADMAP.md](ROADMAP.md)统一记录首版范围、剩余门槛和完成判据，不承诺日期。计划产物及限制见[双语发布说明草稿](docs/releases/v0.1.0.md)，精确提交的 CI 与干净发行包验收见[发布流程](docs/releasing.md)。标签、Release、附件上传及私人实例升级均须另行授权。

## Hermes 技能

将 `skills/content-archive/` 安装到目标 Hermes profile 的技能目录，然后开启新会话。技能会读取收藏库实例的 `README.md` 获取本地规则，不写死某台机器的路径或接收人。

当前用户明确输入以下指令时，执行手动收藏：

```text
收藏：https://example.com/article
```

也可使用英文主入口 `Save: https://example.com/article` 或同义入口 `Archive: https://example.com/article`。英文前缀不区分大小写（如 `save:`、`ARCHIVE:`），冒号后可有空格；中文 `收藏：` 和 `收藏:` 均保留。这些是技能层面的用户指令，不是 shell 命令。输入语言不改变现有中文展示标题和摘要政策。

仅加载技能不产生写入授权。裸链接、一次性摘要、引用示例、规则讨论、翻译请求和明确要求不保存，均不得触发归档，即便其中出现 `Save:` 或 `Archive:`。这不构成对所有模型或 Hermes 配置的行为保证。

获得收藏授权后，代理须先完整读取并收到目标实例 README，再通过 `bin/ca add URL --collection manual` 启动正常收藏；跨项目使用解析后的绝对 CLI 路径。草稿创建和去重由 CLI 负责，后续编辑使用 `finalize`，再重建索引并审计。不得自行构造条目、修改 `.state/seen.json` 或手写索引来替代这些命令；无法执行时不得宣称已保存。

## 仓库结构

```text
bin/ca                    命令行入口
scripts/                  抓取、索引、日报、审计和发布实现
skills/content-archive/   Hermes 技能原件及参考文档
examples/                 禁用或通用化的实例配置示例
tests/                    合成测试与 fixtures
docs/                     发布指南与历史验收证据
assets/                   公开品牌资源
```

## 当前状态

处于早期、持续开发阶段。已实现的改动见[更新记录](CHANGELOG.md)，验证边界见下列文档。

- 测试：完整本地/CI 检查见 [tests/README.md](tests/README.md)。
- CI 工作流：[.github/workflows/test.yml](.github/workflows/test.yml) 在 Ubuntu + Python 3.13 上运行合成测试套件；浏览器与真实实例套件在 CI 中默认关闭。
- 验收边界与已知限制：[阅读体验](docs/reader-experience.md)、[阅读器图片验收](docs/reader-media-acceptance.md)、[行为验收](docs/behavior-acceptance.md)。
- 当前 CI 结果见 [GitHub Actions](https://github.com/taukemora/Content-Archive-Kit/actions/workflows/test.yml)。请核对拟使用提交对应的运行记录；历史验收记录不能证明后续改动已经通过。

[IDEA.md](IDEA.md)记录稳定的项目愿景与架构；[ROADMAP.md](ROADMAP.md)记录版本范围与剩余工作。上游提交或推送不代表私人实例已自动升级。

## 贡献与安全

合成测试资料、检查命令和审阅要求见 [CONTRIBUTING.md](CONTRIBUTING.md)。潜在漏洞请按 [SECURITY.md](SECURITY.md) 报告，不要在公开 issue 中附上私人收藏内容或漏洞利用细节。

## 隐私与部署边界

不得向本仓库提交私人文章、`.state` 数据、凭据、真实发布配置、个人接收人或本机部署配置。可选发布默认关闭。上游变更不得自动修改私人收藏库、cron、已安装技能或发布目标。

## 许可证

Content Archive Kit 采用 [MIT License](LICENSE)。
