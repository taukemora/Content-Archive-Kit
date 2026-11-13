# Watched Sources and Adapters

Use when adding or repairing a watched source. RSS is preferred only when the official site actually publishes a working feed; an official HTML or documentation page is a better primary source than an unofficial feed.

The table below is an illustrative starting set collected from public vendor sites. Treat it as evidence of how the probe behaves, not as a shipped configuration: statuses, paths, and item counts change, and each instance keeps its own `sources.yaml`.

## Illustrative sources

| id | official entry | feed | extraction | notes |
|---|---|---|---|---|
| `openai` | https://openai.com/news/ | https://openai.com/news/rss.xml | RSS for discovery + project `bin/ca extract` with an explicit upstream budget | The feed carries short descriptions, not article bodies; direct HTML may be blocked. Acquisition metadata stays completeness-unverified until the article body is reviewed. |
| `google` | https://blog.google/innovation-and-ai/ | https://blog.google/innovation-and-ai/rss/ | RSS for discovery + direct article fetch | Section feed with ~20 dated items and no `content:encoded`; article HTML is directly fetchable. The section also carries a few non-AI posts. |
| `anthropic` | https://www.anthropic.com/news | none official | HTML listing + `trafilatura` | The listing has titles and dates. An OpenRSS feed can be a fallback, but label it unofficial. |
| `kimi` | https://www.kimi.com/blog/ | none | HTML listing + `trafilatura` | Server-rendered; post links and dates are present. |
| `glm` | https://www.zhipuai.cn/blogs + https://docs.bigmodel.cn/cn/update/new-releases | none | `docs` release list + direct extraction | The blog index is a JS shell. The official docs release page is the primary dated source; `source_type` is `docs`. |

A disabled, generic starting file ships as `examples/sources.yaml`; adapt ids, URLs, and extractors per instance.

## Probe checklist

1. If an explicit feed exists, fetch and parse the XML. Count `<item>`/`<entry>` and inspect the newest date.
2. If no feed, try homepage autodiscovery (`link rel=alternate` with `application/rss+xml` or `atom+xml`) and the common paths `/feed`, `/feed/`, `/rss`, `/rss.xml`, `/feed.xml`, `/atom.xml`, `/index.xml`.
3. If still no feed, load the official listing page, count selector matches, and sample title, link, and date.
4. Record `status: ok|empty|blocked|no-feed|selector-miss` with the newest date and probe time.
5. Enable only `ok` sources by default; re-probe on repeated failure and periodically.

A feed counts only when it yields items with a sane newest date. HTTP 200, a valid channel element, or a stale `lastBuildDate` is not enough. Where entries exist alongside a parser `bozo` warning, log the warning and continue — treating `bozo or not entries` as the empty condition silently discards a usable feed.

## Adapter shape

```yaml
- id: example_rss
  label: Example RSS
  homepage: https://example.com/blog/
  feed: https://example.com/blog/rss.xml
  type: rss            # rss | html | docs
  extractor: direct    # direct | web_extract
  language: en
  category_hint: tech
  context_hint: work
  enabled: false
  notes: "Why this source was chosen and what the probe found."
```

Keep selectors and parsing in the adapter code, not in YAML. YAML describes where to look and which strategy to use.

One RSS adapter should serve every feed: read `extractor`, `language`, and `source_type` from the source dict instead of hard-coding them, so `direct` and `web_extract` sources share a code path. Keep a legacy adapter name registered as an alias while existing configs still reference it.

## Run policy

- **Discovery:** a valid archived watermark enables a bounded unseen lookback that includes items at or older than the watermark; it is not a timestamp exclusion gate. Initialized sources without a watermark use a short guard window; unknown and future dates never advance a watermark. Date-only inputs compare calendar days.
- **Optional editorial selection:** with an explicitly enabled `selection.yaml` and matching engine, discovery candidates must be judged before archiving. Follow `subscription-selection.md` in this directory; accept/reject/defer is separate from technical failure or budget waiting. Without the policy, retain the legacy archive-each-candidate path.
- **Persistence:** retain owed work and its acquisition evidence across interrupted runs, with explicit quota waits, expiry and exit reasons. Pending TTL uses the first enqueue time across fetch and failure writers; recovery must not reset that clock or requeue seen/terminal identities. With selection, the ledger keeps editorial decisions separate from queue execution.
- **First run:** the latest 2 items per source by default. A later explicit backfill must still filter `seen` and must not re-select the same top N.
- **Caps:** per-source and per-batch limits, allocated round-robin, reporting dropped candidates when the batch cap is hit.
- **Zero parsed items:** record `status: empty`; do not mark the source initialized or advance the watermark; report it in the digest.
- **Dedupe:** canonical URL plus content hash; the same article from two sources is one entry recording both labels. Automated dedupe never demotes a manual save.
- **Full text:** prefer `content:encoded` when the feed carries it; otherwise fetch the article page.
- **Dates:** missing or malformed dates use the fetch date with `date_source: fetched` and `published_unknown: true`; keep the item and report the count instead of dropping it.
- **Docs sections:** keep the real DOM id in the URL; without an id, reject visibly rather than guessing an anchor. A real id without a date uses the fetched/unknown fallback and is counted. Persist section Markdown plus the whole-page raw snapshot before committing pending state, and protect both references across page rolloff and failures.

## Pitfalls

- **HTTP 200 with an empty channel.** Count items; do not enable an empty feed or mark a zero-item source `ok` or initialized.
- **Watermark advanced at fetch time.** Advance the newest marker only after an entry is archived; otherwise a failed extraction silently skips the item forever.
- **`--max` silently truncating.** Allocate the cap round-robin and report dropped candidates; do not let the first source consume the batch.
- **Description-only feeds.** Many vendors publish summaries; fetch the article page for the archive.
- **Cloudflare/403 on article HTML.** Use a server-side extractor and set the extraction field accordingly; do not declare the source dead.
- **JS-shell blog index.** Use the official documentation or release-notes page for dated discovery, then fetch the article page.
- **Unofficial feed as primary.** Label it, keep the official page as the fallback, and verify it still matches the site.
- **Full historical feed on first run.** Cap the backfill; otherwise the digest and archive flood.
- **Section feeds mix topics.** A category feed can carry unrelated posts. Decide whether to accept the mix or filter on `<category>` before enabling, and record the choice in the YAML `notes` rather than filtering silently at runtime.
- **Transient TLS/timeout on large pages.** Retry the add or fetch two or three times with a short delay before marking a source `error`; one network blip should not become a missing day.
- **A partial backfill leaves owed work in the horizon.** The lookback keeps discovering unseen items after the requested window. Complete the legacy backfill or account for each item under the enabled selection policy before scheduling; report unresolved work and coverage limits. Do not archive excluded items merely to make a dry run select zero.
