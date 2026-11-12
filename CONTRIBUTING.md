# Contributing

Thanks for considering a contribution. This repository separates generic
toolkit source from private archive instances; the rules below keep that
boundary intact.

## Ground rules

- **No private material.** Never commit private articles, images, raw snapshots,
  `.state` data, credentials, personal recipients, machine-local paths, or real
  deployment settings — in source, tests, documentation, or issue reports.
  Tests use synthetic fixtures only; never copy content from a real archive.
- **Narrow, coherent changes.** One feature, fix, or documentation topic per
  change. Keep unrelated features, fixes, docs, and mechanical reformatting
  separate. Do not drive-by refactor unrelated code.
- **No overclaiming.** Do not describe planned capabilities as working until the
  implementation and tests exist, and do not present synthetic-test passes as
  browser, real-instance, live-delivery, or hosted-publication acceptance.
- **One umbrella skill.** Keep a single canonical `content-archive` skill; do
  not split it by vendor, category, or channel.
- **Local work by default.** Commits, pushes, tags, GitHub Releases, manifest
  regeneration, and any private-instance change (installed skills, cron jobs,
  publication targets) are separate, explicitly approved steps — never side
  effects of an accepted code change. See [AGENTS.md](AGENTS.md) and the
  [release procedure](docs/releasing.md). Never force-push or weaken checks to
  get work in.
- **Platform scope.** Linux is the supported platform and CI target. Native
  Windows is unsupported (use a Linux environment such as WSL); macOS is
  outside support and CI scope. Discuss any expansion of platform support
  before implementing or claiming it.

## Setup and checks

Use Linux, Python 3.13 and Bash. Install Node.js 22 for the full validation
suite and repository JavaScript checks (matching CI); ordinary archiving does
not require Node.

```bash
git clone https://github.com/taukemora/Content-Archive-Kit.git
cd Content-Archive-Kit
python3.13 -m venv .venv
env -u PYTHONPATH .venv/bin/python -m pip install -r requirements.txt -r requirements-publish.txt
env -u PYTHONPATH .venv/bin/python -m pip check
cd tests
env -u PYTHONPATH ../.venv/bin/python -m unittest discover -s . -t .
```

`discover` must run from `tests/` (a checkout path containing spaces breaks
discovery from elsewhere). Read the result the way
[tests/README.md](tests/README.md) explains: `Ran N tests` includes skips, and
a skipped test is not a passing test — quote the exact summary line, including
failures and skips.

The public-tree gate runs read-only from the checkout root:

```bash
env -u PYTHONPATH .venv/bin/python tools/check_repository.py
```

It verifies public-tree membership, byte counts, SHA-256 digests, path/mode
safety, syntax, and that example configurations stay disabled. Optional
browser and real-instance tests stay off unless you provision them separately
(see [tests/README.md](tests/README.md)); a documentation-only change does not
need them.

## Making changes

- **Code** (`scripts/`, `bin/`): match the existing style and failure-visible
  error handling. Extend the relevant tests in the same change; keep scripts
  deriving their project root from their own location.
- **Tests** (`tests/`): synthetic fixtures only; follow the privacy rules in
  [tests/README.md](tests/README.md). Build throwaway trees under the scratch
  mechanism, never inside a private archive or the checkout.
- **Skill** (`skills/content-archive/`): the canonical source is here;
  installed copies in a Hermes profile or private instance are not edited as
  part of a code change. Skill-behavior changes need the documented probe or
  conversation evidence before any behavioral claim.
- **Docs**: keep claims within actual evidence; link the acceptance record that
  supports a status statement. [ROADMAP.md](ROADMAP.md) is the single
  release-scope/status roadmap — put release-scope or status changes there, not
  in scattered files.
- **Manifest** (`docs/public-tree-sha256.json`): after an approved change is
  staged, regenerate it only with `tools/check_repository.py --update-manifest`
  as [tests/README.md](tests/README.md) describes. Never edit it by hand, and
  never let CI regenerate it to make itself green.

## Submitting

Before requesting review: run the relevant checks, inspect the full diff, audit
it for private content and machine-local configuration, and report the changed
files with actual results (including skips and failures). Report what you
verified, not what you intend to verify later.

## 中文摘要

通用源码与私人实例分离，测试一律使用合成材料；不得提交私人文章、图片、快照、状态、凭据或真实部署配置。每次改动保持窄而完整，不把计划或合成测试说成线上验收。维护唯一的 `content-archive` 技能；提交、推送、发布及实例升级分别授权。支持 Linux；完整验证还需 Node.js 22。检查命令和清单维护方式见上文与 [tests/README.md](tests/README.md)，如实报告实际结果及跳过项。
