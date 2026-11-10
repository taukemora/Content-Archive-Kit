# Content Archive Kit

A Markdown-first article archiving toolkit with a command-line workflow and an umbrella Hermes skill.

The project is being separated from a proven private archive so that reusable source code and skills can be maintained publicly without exposing articles, runtime state, credentials, or personal deployment settings.

## Intended capabilities

- Manual article archiving with deduplication, full text, local media, and source metadata.
- Chinese display titles, validated Chinese summaries, stable classification, and tags.
- Search, rebuildable indexes, audits, and soft-deletion workflows.
- RSS-first watched-source collection and honest daily digests.
- An explicit Hermes intake command: `收藏：<article-url>`.
- Optional local reading and explicitly configured publication adapters.

## Current status

**Initial migration and isolated acceptance are complete.** The generic implementation, tests, dependency manifests, configuration examples, and installable skill source are present. The repository is ready for its initial public-source commit review; no versioned release or production deployment has been approved yet.

See [IDEA.md](IDEA.md) for the project vision, architecture, non-goals, migration approach, and acceptance criteria. See [tests/README.md](tests/README.md) for the isolated test workflow, [examples/README.md](examples/README.md) for instance configuration templates, and [docs/acceptance.md](docs/acceptance.md) for the verified scope.

## Safety and privacy boundary

This public repository must not receive private archive content, `.state` data, credentials, real publication settings, personal recipients, or machine-local deployment configuration. Tests must use synthetic fixtures.

No upstream update should automatically modify a private archive or its Hermes installation.

## License

Content Archive Kit is available under the [MIT License](LICENSE).
