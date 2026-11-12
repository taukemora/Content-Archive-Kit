# Content Archive Kit — Project Idea

## Why this project exists

A personal article archive has been proven useful as a Markdown-first collection with full text, local media, Chinese summaries, stable classification, search, and scheduled vendor digests. Its implementation and Hermes skill have evolved together, but the reusable source needs a clean public home separate from private articles, state, and deployment settings.

Content Archive Kit turns that proven workflow into a reusable toolkit. It is intended to help people create a new archive or maintain an existing one without copying private content or depending on one machine's Hermes profile.

This document records the stable vision, principles, architecture, and
non-goals. Release scope, current status, and remaining gaps live in
[ROADMAP.md](ROADMAP.md); contribution rules and security guidance live in
[CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## Vision

Provide a portable article-archiving toolkit consisting of:

- a small command-line entry point;
- the archiving, validation, indexing, search, audit, and digest implementation;
- one umbrella Hermes skill;
- configuration examples and synthetic test fixtures;
- documentation for new archives, existing archives, and Hermes integration.

The toolkit should be useful from the command line on its own. Hermes adds conversational intake, editorial judgment, summaries, classification, and optional scheduled workflows; it should not become the only copy of the archive or its rules.

## Core capabilities

The intended toolkit supports:

1. **Manual article intake** — canonicalize and deduplicate URLs, extract article text, preserve available source snapshots, localize images, and record provenance.
2. **Editorial completion** — create a Chinese display title and a validated Chinese summary while retaining the original title and source metadata.
3. **Classification and retrieval** — use stable broad categories, optional subcategories, collection context, tags, full-text search, and rebuildable indexes.
4. **Scheduled collection** — discover watched-source posts with RSS preferred, retain retry and failure state, and produce an honest digest.
5. **Maintenance** — audit entries and indexes, repair supported forms of state drift, and soft-delete replaced entries.
6. **Optional reading and publishing** — generate a local reader and support explicitly configured deployment adapters without making hosted publication mandatory.

## Simple conversational intake

The Hermes skill should recognize an explicit command such as:

```text
Save: https://example.com/article
Archive: https://example.com/article
收藏：https://example.com/article
```

`Save:` is the primary English entry point; `Archive:` is equivalent. English prefixes are case-insensitive and allow zero or more spaces after the colon. Chinese `收藏：` and `收藏:` remain equivalent. The same intent may include an explicit classification hint. This is a conversational skill contract, not a shell-command parser; input language does not alter the Chinese display-title and summary policy.

The execution boundary matters as much as skill discovery:

- A current user instruction with explicit archive intent and a real HTTP(S) article URL authorizes the normal manual-intake workflow.
- A bare URL, a one-off summary request, a discussion of archiving rules, a translation request, an explicit instruction not to save, or an example inside documentation does not authorize archiving, even if it contains a trigger phrase.
- Loading the skill alone never authorizes a write.
- Read and receive the complete target instance README before applying its rules; do not batch this prerequisite with dependent extraction or mutation. Across projects, resolve the absolute instance CLI path.
- Start normal manual intake through `bin/ca add URL --collection manual`, inspect the real result, then complete the draft through `finalize`, rebuild indexes, and audit. Do not bypass the CLI by manufacturing persistent entries, deduplication state, or indexes. Blocked acquisition may use the documented content-file fallback; inability to execute must be reported, not disguised as a successful save.

## Architecture

There are three distinct roles:

```text
Content Archive Kit (public upstream)
Generic source + skill + examples + tests + releases
                    │
                    │ reviewed, pinned installation or upgrade
                    ▼
A private archive instance
Articles + runtime state + local configuration + deployed toolkit
                    │
                    │ optional, explicitly configured publication
                    ▼
A separate hosting target
Only the selected public program and publication artifacts
```

The public upstream never needs access to private archive contents or credentials. A hosting target is not the source repository and must not become one.

## Design principles

- **Markdown is the content fact source.** Each entry remains readable and portable without a database.
- **One umbrella skill.** Do not create a skill per vendor, category, or channel.
- **Instance rules stay with the instance.** A deployment README and configuration files hold local taxonomy, sources, paths, and delivery choices.
- **Public source contains no private corpus.** Use synthetic fixtures rather than copied user articles.
- **Pinned upgrades.** A private instance installs a reviewed release; it does not track the upstream development branch automatically.
- **Failure is visible.** A blocked or empty acquisition is not reported as successful coverage or as proof that no news exists.
- **Preserve evidence and history.** Keep supported raw snapshots, avoid destructive deletion, and do not rewrite publication history to bypass safety checks.
- **Optional means optional.** Readers, NAS deployment, encrypted hosting, and messaging delivery must not be required for basic archiving.

## Repository and instance boundaries

### Public repository

The public repository is intended to contain:

- `bin/` and `scripts/` for the generic implementation;
- `skills/content-archive/` as the only canonical source of the Hermes skill;
- reusable templates and configuration examples;
- complete dependency declarations;
- sanitized tests and synthetic fixtures;
- generic documentation, contribution rules, security guidance, and release notes.

### Private archive instance

A private instance retains:

- articles, localized media, raw snapshots, and trash;
- deduplication, source, retry, and run state;
- actual taxonomy, source, delivery, and publication configuration;
- generated indexes and readers;
- deployment-specific documentation and credentials outside public source control.

## Distribution and upgrade model

GitHub Releases may provide versioned toolkit archives built from the public source. A release is an export of the same canonical source, not another maintained implementation.

A private instance should record the installed upstream version and commit. Upgrades are applied only after isolated verification and a backup. An upstream push must not automatically modify a running archive, install a global skill, send notifications, or publish articles.

## Non-goals

- Publishing private articles, state, credentials, or deployment identifiers.
- Requiring GitHub Pages, a NAS, a database, or a particular messaging provider.
- Replacing the established Markdown archive with a separate hosted source of truth.
- Automatically importing every upstream change into a private deployment.
- Claiming that a skill document alone can reproduce the executable implementation.

## Migration provenance

The public tree was created by exporting only allowlisted generic source,
skill, template, and test files from a frozen, accepted reference state, with
machine-local paths, personal recipients, real deployment settings, and private
fixtures removed. The accepted source version, file hashes, and acceptance
boundaries are recorded in [docs/migration-baseline.json](docs/migration-baseline.json)
and [docs/acceptance.md](docs/acceptance.md). Ongoing work here keeps the same
boundary: synthetic fixtures, no private material, and optional publication
disabled until explicitly configured. Release criteria and current gaps are
maintained in [ROADMAP.md](ROADMAP.md), not in this document.

## Settled decisions

- The repository license is **MIT**, explicitly selected by the maintainer. The same license applies to the executable code, tests, documentation, and skill.
- **Linux** is the supported platform and CI target, following actual executable behavior and tests rather than a broad metadata declaration; macOS was removed from the support and CI scope by an explicit maintainer decision, and native Windows is unsupported.
- Messaging, NAS, and hosted-publication adapters remain optional. Their documentation must distinguish implemented behavior from verified installation paths; further generalization is future direction, not a release promise.
