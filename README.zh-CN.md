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

- 手动收藏文章：URL 规范化、去重、全文、本地媒体（图片及可选 PDF 附件）、可用时的原始快照和来源元数据。
- 中文展示标题和经代码校验的中文摘要，同时保留原文标题。
- 稳定的一级分类、可选二级分类、收藏场景和标签。
- 全文检索、可重建索引、审计和软删除。
- RSS 优先的监视来源采集，内置通用 RSS 适配器和特定站点的 HTML/文档适配器，保留明确的失败重试状态并如实呈现日报。
- 本地阅读页，以及需要显式配置的可选发布适配器。
- 一个 Hermes 技能，通过 `收藏：<文章链接>` 触发；裸链接、一次性摘要和引用示例不会误触发归档。

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

如需可选的加密发布能力，再安装附加依赖：

```bash
.venv/bin/python -m pip install -r requirements-publish.txt
```

创建收藏库实例时，将 `bin/`、`scripts/` 和配置示例复制到实例根目录，再修改实例内的 `taxonomy.yaml`、`sources.yaml` 和 `media.yaml`。不要直接修改仓库中的示例。每个实例还需要独立的可用解释器：在 `<实例目录>/.venv` 中安装 `requirements.txt`，或者把 `CA_PYTHON` 指向已经安装这些依赖的解释器；不要依赖未经确认的系统 `python`。可选发布默认保持关闭，只有显式配置后才启用。

实例目录、虚拟环境、定时任务、升级和迁移见[部署指南](skills/content-archive/references/deployment.md)。

## 验证

```bash
cd tests
../.venv/bin/python -m unittest discover -s . -t .
```

全部测试使用合成 fixtures。浏览器验收和真实实例 dry run 需要显式开启；完整的本地/CI 检查（包括依赖、语法、示例配置和公开树完整性）见 [tests/README.md](tests/README.md)。

## 发布准备

首个计划版本为 **v0.1.0**，目前尚未发布。详见[更新记录](CHANGELOG.md)、[双语发布说明草稿](docs/releases/v0.1.0.md)和[发布流程](docs/releasing.md)。工作流在 Ubuntu 上验证 Python 3.13，但存在工作流不等于已经通过。[CI 准备验收](docs/ci-acceptance.md)区分本地结果、精确提交的 GitHub CI 与后续工具包验收，并保留此前 macOS 失败和随后收敛为 Linux-only 的范围决定。标签、Release、附件上传及私人实例升级均须另行授权。

## Hermes 技能

将 `skills/content-archive/` 安装到目标 Hermes profile 的技能目录，然后开启新会话。技能会读取收藏库实例的 `README.md` 获取本地规则，不写死某台机器的路径或接收人。

当前用户明确输入以下指令时，执行手动收藏：

```text
收藏：https://example.com/article
```

仅加载技能不产生写入授权。裸链接、一次性摘要、引用示例和规则讨论均不会触发归档。

## 仓库结构

```text
bin/ca                    命令行入口
scripts/                  抓取、索引、日报、审计和发布实现
skills/content-archive/   Hermes 技能原件及参考文档
examples/                 禁用或通用化的实例配置示例
tests/                    合成测试与 fixtures
docs/                     迁移和验收记录
assets/                   公开品牌资源
```

## 当前状态

CI 准备单元已在干净的 Python 3.13 环境中通过本地隔离验收：**共 615 项测试，613 项通过、2 项明确跳过，零失败或错误**。[CI 准备验收](docs/ci-acceptance.md)记录首次远程失败、scratch 环境修正，并区分本地验证与精确提交的远程 CI。

[初始迁移记录](docs/acceptance.md)保留了此前新建收藏库和既有库副本的 CLI 验收证据；其中历史测试总数不是当前通过数。项目愿景和架构见 [IDEA.md](IDEA.md)。上游提交或推送不代表私人实例已自动升级。

## 隐私与部署边界

不得向本仓库提交私人文章、`.state` 数据、凭据、真实发布配置、个人接收人或本机部署配置。可选发布默认关闭。上游变更不得自动修改私人收藏库、cron、已安装技能或发布目标。

## 许可证

Content Archive Kit 采用 [MIT License](LICENSE)。
