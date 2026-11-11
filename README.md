<p align="right">
  <strong>EN</strong> | <a href="./README.zh-CN.md">简</a>
</p>

<p align="center">
  <img src="./assets/logo.svg" alt="Content Archive Kit logo" width="96" height="96">
</p>

# Content Archive Kit

[![Python 3.13](https://img.shields.io/badge/python-3.13-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A Markdown-first article archiving toolkit with a command-line workflow and an umbrella Hermes skill.

Content Archive Kit separates reusable source code and skills from private articles, runtime state, credentials, and deployment settings. It can be used to create a new archive or maintain an existing one without making GitHub, a database, or a hosted service the source of truth.

## Features

- Manual article intake with URL normalization, deduplication, full text, local media, raw snapshots when available, and source metadata.
- Chinese display titles and validated Chinese summaries, with the original title preserved.
- Stable categories, optional subcategories, collection context, and tags.
- Full-text search, rebuildable indexes, audits, and soft deletion.
- RSS-first watched-source collection with a generic RSS adapter and site-specific HTML/docs adapters, explicit retry state, and honest daily digests.
- A local reader and optional, explicitly configured publication adapters.
- One Hermes skill defining English `Save: <article-url>` / `Archive: <article-url>` and Chinese `收藏：<article-url>` intake commands, with a no-archive policy for bare URLs, one-off summaries, and quoted examples. Model behavior must be verified separately from these written rules.

## Requirements

- Linux (CI runs on Ubuntu). Native Windows is unsupported; use a Linux environment such as WSL instead. macOS is outside the supported platform and CI scope; compatibility is not guaranteed.
- Python 3.13.
- Bash and the dependencies declared in `requirements.txt`.

## Install

```bash
git clone https://github.com/taukemora/Content-Archive-Kit.git
cd Content-Archive-Kit
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

The current base installation includes reader and encryption dependencies, including `markdown-it-py` and `cryptography`; it is not a minimal core-only dependency set. `markdown-it-py` is also imported by article media processing. The separate publication requirements file currently overlaps with the base requirements; it can be installed explicitly when preparing encrypted publication:

```bash
.venv/bin/python -m pip install -r requirements-publish.txt
```

Create an archive instance by copying `bin/`, `scripts/`, and the configuration examples into the instance root. Adapt the copied `taxonomy.yaml`, `sources.yaml`, and `media.yaml` there rather than editing the shipped examples. Each instance also needs its own working interpreter: create `<archive>/.venv` and install `requirements.txt` there, or set `CA_PYTHON` to an interpreter that already has the declared dependencies. Do not rely on an unverified system `python`. Optional publication remains disabled until explicitly configured.

See [the deployment guide](skills/content-archive/references/deployment.md) for instance layout, virtualenv, scheduling, upgrades, and migration.

### Optional behavior and verification limits

`bin/ca index` currently generates `index.csv`, `index.md`, and the local `index.html` reader together; HTML generation is not a separate opt-in step. Hosted publication is a different operation and stays disabled without an enabled instance configuration. Basic archiving does not require a hosting account, NAS, messaging target, or running reader service.

Publication and delivery paths have synthetic tests. These do not establish real-browser, live-delivery, NAS, or online-hosting acceptance for a new installation. The opt-in real-instance test previews daily delivery only; it is not an archive search/update acceptance test. Verify each optional integration in its intended environment before enabling it.

## Verify

```bash
cd tests
../.venv/bin/python -m unittest discover -s . -t .
```

The test suite uses synthetic fixtures only. Browser acceptance and real-instance dry runs are opt-in; see [tests/README.md](tests/README.md) for the complete local/CI checks, including dependency, syntax, example-configuration, and public-tree integrity verification.

## Release readiness

The first planned release is **v0.1.0**, not yet released. See the [changelog](CHANGELOG.md), [bilingual release-notes draft](docs/releases/v0.1.0.md), and [release procedure](docs/releasing.md). The workflow validates Ubuntu with Python 3.13; its presence is not proof of a successful run. [CI readiness acceptance](docs/ci-acceptance.md) distinguishes local results from exact-commit GitHub CI and future artifact verification, and preserves the earlier macOS failures and subsequent Linux-only scope decision. Tags, Releases, asset uploads, and private-instance upgrades require separate approval.

## Hermes skill

Install `skills/content-archive/` into the target Hermes profile's skills directory and start a new session. The skill reads the archive instance's `README.md` for local rules instead of hard-coding one machine's paths or recipients.

A current user instruction such as the following invokes manual intake:

```text
Save: https://example.com/article
```

`Archive: https://example.com/article` is an equivalent English command. English prefixes are case-insensitive (`save:` and `ARCHIVE:` also work), and spaces after the colon are optional. Chinese `收藏：` and `收藏:` remain supported. These are skill-level instructions, not commands to enter into a shell. The input language does not change the Chinese display-title and summary policy.

Loading the skill alone does not authorize a write. Bare URLs, one-off summaries, quoted examples, rule discussions, translation requests, and explicit instructions not to save must not trigger archiving, even when they contain `Save:` or `Archive:`. This policy is not a guarantee for every model or Hermes configuration.

For an authorized save, the agent first reads and receives the complete target instance README, then starts normal intake through `bin/ca add URL --collection manual`. Across projects it uses the resolved absolute CLI path. The CLI owns draft creation and deduplication; subsequent editorial work uses `finalize`, followed by index/audit checks. The agent must not manufacture an entry or edit `.state/seen.json` or indexes as a substitute for those commands, and must not claim a save when execution is unavailable.

## Repository layout

```text
bin/ca                    CLI wrapper
scripts/                  archiving, indexing, digest, audit, and publication code
skills/content-archive/   canonical Hermes skill and references
examples/                 disabled or generic instance configuration examples
tests/                    synthetic tests and fixtures
docs/                     migration and acceptance records
assets/                   public brand assets
```

## Current status

The latest isolated local regression run in a clean Python 3.13 environment completed **730 tests: 728 passed and 2 explicitly skipped**, with no failures or errors. The separate preloaded-skill probe passed **18/18 authorization and CLI-routing cases** (8 save, 10 no-save) on `opencode-go` / `deepseek-v4.1-flash`, under an explicit first-change dry-run protocol. This is not end-to-end conversational intake or automatic skill discovery acceptance. Final-response compliance is not graded; one explicit-no-save response still offered future saving despite the skill's rule. See [behavior acceptance](docs/behavior-acceptance.md) for the full evidence and preserved earlier failures. [CI readiness acceptance](docs/ci-acceptance.md) preserves the earlier CI-preparation results; hosted CI has not verified these uncommitted changes.

The [initial migration record](docs/acceptance.md) preserves earlier fresh-archive and copied-existing-archive CLI evidence; its historical test totals are not the current pass count. [IDEA.md](IDEA.md) describes the project vision and architecture. No production upgrade is implied by an upstream commit or push.

## Privacy and deployment boundary

Never commit private articles, `.state` data, credentials, real publication settings, personal recipients, or machine-local configuration. Optional publication is disabled by default. Upstream changes must not automatically modify a private archive, its cron jobs, its installed skills, or its publication targets.

## License

Content Archive Kit is available under the [MIT License](LICENSE).
