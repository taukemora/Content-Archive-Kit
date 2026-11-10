# Example configuration

Templates for a new archive instance. They are examples, not live configuration: copy them into the archive root and adapt them there. Do not edit these files to configure a deployment, and do not copy an adapted instance configuration back into this repository.

| File | Purpose |
|---|---|
| `taxonomy.yaml` | Broad categories, optional children, contexts, and classification keywords. |
| `sources.yaml` | Watched sources with disabled placeholder entries for the three supported strategies (RSS, HTML listing, docs sections). |
| `media.yaml` | Default image budgets and download policy. |
| `publish.yaml` | Optional publication adapter. Disabled and value-free by design; enabling requires an explicit owner decision and a confirmed target. |

All four files contain no real paths, recipients, credentials, or deployment identifiers. Keep it that way: a filled-in publication or source file belongs to the instance, not to this repository.

Related documentation:

- `../skills/content-archive/SKILL.md` — the umbrella operating procedure.
- `../skills/content-archive/references/deployment.md` — creating an instance, scheduling, upgrade and backup.
- `../skills/content-archive/references/taxonomy-and-context.md` — the three-axis model behind `taxonomy.yaml`.
- `../skills/content-archive/references/vendor-sources.md` — probing a source before adding it.
- `../skills/content-archive/references/publication.md` — hosting and publication requirements.
