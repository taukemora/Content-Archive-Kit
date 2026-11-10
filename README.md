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
- One Hermes skill with an explicit `收藏：<article-url>` intake command and counter-triggers for bare URLs, one-off summaries, and quoted examples.

## Requirements

- Linux or macOS (POSIX); native Windows requires WSL.
- Python 3.13.
- Bash and the dependencies declared in `requirements.txt`.

## Install

```bash
git clone https://github.com/taukemora/Content-Archive-Kit.git
cd Content-Archive-Kit
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

For optional encrypted publication, install the additional requirements:

```bash
.venv/bin/python -m pip install -r requirements-publish.txt
```

Create an archive instance by copying `bin/`, `scripts/`, and the configuration examples into the instance root. Adapt the copied `taxonomy.yaml`, `sources.yaml`, and `media.yaml` there rather than editing the shipped examples. Each instance also needs its own working interpreter: create `<archive>/.venv` and install `requirements.txt` there, or set `CA_PYTHON` to an interpreter that already has the declared dependencies. Do not rely on an unverified system `python`. Optional publication remains disabled until explicitly configured.

See [the deployment guide](skills/content-archive/references/deployment.md) for instance layout, virtualenv, scheduling, upgrades, and migration.

## Verify

```bash
cd tests
../.venv/bin/python -m unittest discover -s . -t .
```

The test suite uses synthetic fixtures only. Browser acceptance and real-instance dry runs are opt-in; see [tests/README.md](tests/README.md) for the complete local/CI checks, including dependency, syntax, example-configuration, and public-tree integrity verification.

## Release readiness

The first planned release is **v0.1.0**, not yet released. See the [changelog](CHANGELOG.md), [bilingual release-notes draft](docs/releases/v0.1.0.md), and [release procedure](docs/releasing.md). The workflow validates Ubuntu/macOS with Python 3.13; its presence is not proof of a successful run. [CI readiness acceptance](docs/ci-acceptance.md) distinguishes local results from exact-commit GitHub CI and future artifact verification. Tags, Releases, asset uploads, and private-instance upgrades require separate approval.

## Hermes skill

Install `skills/content-archive/` into the target Hermes profile's skills directory and start a new session. The skill reads the archive instance's `README.md` for local rules instead of hard-coding one machine's paths or recipients.

A current user instruction such as the following invokes manual intake:

```text
收藏：https://example.com/article
```

Loading the skill alone does not authorize a write. Bare URLs, one-off summaries, quoted examples, and rule discussions do not trigger archiving.

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

The CI-preparation unit passed isolated local acceptance in a clean Python 3.13 environment: **615 tests total, 613 passed and 2 explicitly skipped**, with no failures or errors. See [CI readiness acceptance](docs/ci-acceptance.md) for the initial hosted failure, its scratch-environment correction, and the distinction between local verification and exact-commit hosted CI.

The [initial migration record](docs/acceptance.md) preserves earlier fresh-archive and copied-existing-archive CLI evidence; its historical test totals are not the current pass count. [IDEA.md](IDEA.md) describes the project vision and architecture. No production upgrade is implied by an upstream commit or push.

## Privacy and deployment boundary

Never commit private articles, `.state` data, credentials, real publication settings, personal recipients, or machine-local configuration. Optional publication is disabled by default. Upstream changes must not automatically modify a private archive, its cron jobs, its installed skills, or its publication targets.

## License

Content Archive Kit is available under the [MIT License](LICENSE).
