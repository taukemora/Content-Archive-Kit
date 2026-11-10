# Taxonomy and Context

Use when designing or revising the archive's categories. The archive must absorb professional and personal reading without splitting into parallel trees. Keep the top level broad and stable; add finer structure later without moving files.

## Three axes

| Axis | Where | Purpose | Change cost |
|---|---|---|---|
| `category` | folder `library/<category>/` | What the item is about | High; keep stable |
| `subcategory` | frontmatter, optional | Finer topic | Low; add later, no file move |
| `context` | frontmatter | Why the user saved it: `work`, `life`, `learning`, `reference` | None |
| `tags` | frontmatter | Entities, products, technologies, concepts | None |

Do not create parallel `work/` and `life/` folder trees. One article can be both professional and personal; `context` records the use without duplicating the entry. Do not make the first taxonomy industry-specific unless the user explicitly wants a single-industry archive.

## Default broad categories

| slug | label | scope | future children (examples) |
|---|---|---|---|
| `tech` | 科技与 AI | AI models & research, AI applications, semiconductors, hardware, science frontiers | `ai`, `semiconductor`, `hardware`, `science` |
| `coding` | 编程与开发 | programming languages, developer tools, web, data, devops, open-source | `languages`, `tools`, `web`, `data`, `devops`, `open-source` |
| `business` | 商业与产业 | companies, markets, startups, industry analysis | `companies`, `markets`, `startups`, `industry` |
| `work` | 工作与成长 | methods, management, communication, career, learning | `methods`, `management`, `communication`, `career`, `learning` |
| `society` | 社会与公共 | policy, media, education, cities, law | `policy`, `media`, `education`, `cities`, `law` |
| `culture` | 文化与阅读 | books, film, art, history, writing | `books`, `film`, `art`, `history`, `writing` |
| `life` | 生活与健康 | health, psychology, relationships, travel, food, home | `health`, `psychology`, `relationships`, `travel`, `food`, `home` |
| `other` | 其他 | genuinely miscellaneous; not a dumping ground for unresolved items | — |

The toolkit has no inbox router: acquisition failures are recorded as `empty`/`error` in run state, and classification uncertainty is reported in the reply without forcing a category. Do not create an `other` entry just to avoid a decision.

`coding` vs `tech` demarcation — decide by use, not topic:

- `coding` accepts items whose reader needs to change, build, or maintain a software artifact: code, tools, frameworks, deploy scripts, reproducible benchmarks, engineering-practice write-ups.
- `tech` accepts items whose reader needs to judge capability, model limits, or industry direction: research findings, model launches, customer cases, evaluation data.
- Hybrids follow the centre of gravity. Shipping a runnable artifact or an engineering practice → `coding`. Experiment design and conclusions → `tech`, optionally with a `coding` tag.
- Worked examples: a framework tutorial with runnable code → `coding` folder; a customer case study of a shipped product → `tech` folder plus a `coding` tag.

## Contexts

Four contexts: `work`, `life`, `learning`, `reference`.

Priority order:

1. **Explicit user hint wins** (for example `--context work`, or "this one is for learning").
2. **Vendor news, tutorials, developer tools** default to `work`.
3. **Personal essays, inspirations, announcements kept for reference** default to `reference`; a personal learning-method tutorial may be `learning` when the user says so.
4. When unclear, prefer `reference`. Do not over-infer a work/life split from topic alone.

## `taxonomy.yaml` schema

```yaml
version: 1
categories:
  - slug: tech
    label: 科技与 AI
    description: AI models and products, semiconductors, software, hardware, science
    keywords: [AI, 大模型, Agent, 芯片, 半导体, 光刻, EDA]
    children: []          # add later: ai, semiconductor, software...
  - slug: business
    label: 商业与产业
    description: companies, markets, startups, industry analysis
    keywords: [融资, 财报, 市场, 创业, 收购]
    children: []
contexts:
  - slug: work
    label: 工作
  - slug: life
    label: 生活
  - slug: learning
    label: 学习
  - slug: reference
    label: 参考资料
```

`children` may stay empty for months. A missing or null `subcategory` is valid. A ready-to-adapt file ships as `examples/taxonomy.yaml`; copy it into a new instance instead of editing the example.

## Classification order

1. Keyword match against the title and the first part of the body; strong multi-keyword hits win.
2. Model judgment using the category descriptions when keywords are insufficient.
3. When uncertain, keep the best-supported category, report the uncertainty in the reply, and never force a category or claim a routing step that does not exist.
4. Set `context` from the user's hint first, then from the item's intended use; default `reference`.
5. Add tags for concrete entities, not restatements of the category.

## Adding a subcategory later

1. Add the child slug, label, and description to `taxonomy.yaml`.
2. Update classifier rules or prompts.
3. Backfill `subcategory` on selected old entries only where useful; leave the rest null.
4. Do not move folders. Paths stay `library/<category>/...`.
5. Rebuild the index and audit after the backfill; paths and links must remain stable.

## Examples

| Item | category | subcategory | context |
|---|---|---|---|
| AI workflow / agent tooling article | `tech` | `null` or later `ai` | `work` |
| Vendor model release | `tech` | `null` or later `ai` | `work` |
| Personal health long-read | `life` | `null` or later `health` | `life` |
| Company earnings analysis | `business` | `null` or later `companies` | `work` |
| Book notes | `culture` | `books` | `learning` |

## Anti-patterns

- One top-level category per industry, vendor, or source.
- `work/` and `life/` as separate roots.
- Deep folder trees that require moving files as the taxonomy evolves.
- Forcing every entry into a child category before the child exists.
- Renaming slugs casually; paths, links, and seen records break.
- Encoding one instance's private vocabulary in a shared template.
