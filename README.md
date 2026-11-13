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

- Manual article intake with URL normalization, deduplication, full text, local images, raw snapshots when available, and source metadata. Optional PDF attachments are entry-level files added through the [paper workflow](skills/content-archive/references/paper-archiving.md), not automatic PDF intake through `add`.
- Chinese display titles and validated Chinese summaries, with the original title preserved.
- Stable categories, optional subcategories, collection context, and tags.
- Full-text search, rebuildable indexes, audits, and soft deletion.
- RSS-first watched-source collection with a generic RSS adapter and site-specific HTML/docs adapters, explicit retry state, and honest daily digests.
- A local reader and optional, explicitly configured publication adapters.
- Encrypted-reader media prefetch: visible first, up to two viewport-heights ahead/behind, with three concurrent fetch/decrypt jobs and cancellation on article switch or lock.
- One Hermes skill defining English `Save: <article-url>` / `Archive: <article-url>` and Chinese `收藏：<article-url>` intake commands, with a no-archive policy for bare URLs, one-off summaries, and quoted examples. Model behavior must be verified separately from these written rules.
- Reader experience: inline-emphasis source-format normalization, archived/published ordering, conservative white-on-grey caption recognition, a candidate-only `format-backfill plan|prepare` CLI, and export-capacity reporting with unchanged publication limits.

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

## Releases and next development

**[v0.1.0](https://github.com/taukemora/Content-Archive-Kit/releases/tag/v0.1.0)** is published as an early GitHub **Pre-release**, not a stable release. See the [bilingual release notes](docs/releases/v0.1.0.md) for its exact source commit, CI, assets and limitations. It does **not** include research/materiality-based subscription selection.

[ROADMAP.md](ROADMAP.md) tracks scope and acceptance gates without promising dates. Tags, Releases, asset uploads, and private-instance upgrades remain separate approval checkpoints; upstream publication never changes an instance's collection behavior automatically.

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
docs/                     release guidance and historical acceptance evidence
assets/                   public brand assets
```

## Current status

Early-stage toolkit under active development. See the [changelog](CHANGELOG.md)
for implemented changes and the documents below for verification boundaries.

- Tests: [tests/README.md](tests/README.md) documents the complete local/CI checks.
- CI workflow: [.github/workflows/test.yml](.github/workflows/test.yml) runs the synthetic suite on Ubuntu with Python 3.13; the optional browser and real-instance suites stay off in CI.
- Acceptance boundaries and known limitations: [reader experience](docs/reader-experience.md), [reader media acceptance](docs/reader-media-acceptance.md), and [behavior acceptance](docs/behavior-acceptance.md).
- Current CI results are available in [GitHub Actions](https://github.com/taukemora/Content-Archive-Kit/actions/workflows/test.yml). Check the run for the commit you intend to use; historical acceptance records do not certify later changes.

[IDEA.md](IDEA.md) holds the stable vision and architecture; [ROADMAP.md](ROADMAP.md) holds release scope and remaining work. No production upgrade is implied by an upstream commit or push.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for synthetic fixtures, checks and review expectations. Report potential vulnerabilities using [SECURITY.md](SECURITY.md); do not include private archive data or exploit details in public issues.

## Privacy and deployment boundary

Never commit private articles, `.state` data, credentials, real publication settings, personal recipients, or machine-local configuration. Optional publication is disabled by default. Upstream changes must not automatically modify a private archive, its cron jobs, its installed skills, or its publication targets.

## License

Content Archive Kit is available under the [MIT License](LICENSE).
