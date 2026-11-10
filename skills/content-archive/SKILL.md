---
name: content-archive
description: "Use when saving links or maintaining a content archive."
version: 2.0.0
author: taukemora, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [content-archive, read-it-later, rss, wechat, summarization, knowledge-management, taxonomy, messaging]
    related_skills: [blogwatcher, competitor-news-monitor, hermes-project-onboarding, hermes-agent]
---

# Content Archive

Operate a portable, Markdown-first article archive: ad-hoc saved links plus posts pulled from watched sources. Each entry owns a display title, source, publish date, canonical URL, full text, localized images, a fixed-length summary, a broad category, an optional subcategory, and a context. A scheduled run turns new posts into archived entries plus an honest digest.

This skill is the umbrella procedure for intake, collection, maintenance, and optional publication. The executable toolkit (`bin/ca`, `scripts/`) belongs to the archive instance, not to this document. Detailed deployment, publication, and repair procedures live in `references/` — read them when the task reaches that stage instead of improvising.

## When to Use

Use for:

- **Explicit archive command:** a current user message `收藏：<article-url>` or `收藏:<article-url>`, optionally with a classification or context hint.
- An equivalent current instruction with explicit archive intent, such as "archive this article" plus a real HTTP(S) article URL.
- Building a new archive, adding a watched source, running or repairing the daily digest.
- Repairing an existing archive: missing summaries, duplicates, broken image links, stale index, state drift.

Do not use for:

- A **bare URL** with no instruction — it establishes nothing.
- A **one-off summary** request (`web_extract` is enough; nothing is written).
- A **quoted example**, a rule discussion, a README/doc snippet, or a URL that appeared in a feed listing or an earlier assistant message.
- Plain feed reading (`blogwatcher`) or materiality-scored competitor monitoring (`competitor-news-monitor`) — this skill archives every selected post, not only material events.

## Instance Contract

This skill is installed into many archives. It must never assume one machine's layout.

1. **Locate the archive root, do not assume it.** Use the current project session's working directory when it is the archive, an explicit path the user gives, or `bin/ca root` from a known install. Never write a hard-coded home directory into a command, an entry, or documentation.
2. **Read `<archive>/README.md` before applying any rule.** It is the instance fact source: taxonomy slugs, summary length, category/context policy, directory conventions, schedule and timezone, delivery targets, publication state, and any local hard bans. Where the instance README and this skill disagree, the instance wins — then report the drift instead of silently following either.
3. **Configuration lives in the instance:** `taxonomy.yaml`, `sources.yaml`, `media.yaml`, and an optional `publish.yaml`. Example shapes ship in this repository's `examples/`; copy and adapt them per instance rather than editing the examples.
4. **Loading this skill authorizes nothing.** A write needs a current explicit instruction (see Execution Gate). Reading this file, opening the archive, or seeing a link in a feed is not authorization.
5. **Runtime requirements and support scope:** use Linux; the maintained CI runs on Ubuntu. The CLI wrapper requires Bash (`#!/usr/bin/env bash`, `set -euo pipefail`), and the locking code imports `fcntl` for advisory `flock`. Native Windows requires a Linux environment such as WSL. POSIX prerequisites do not establish macOS acceptance: macOS is outside the maintained support and CI scope, so `platforms` is limited to Linux. The wrapper picks an interpreter in the order `CA_PYTHON` → `<archive>/.venv/bin/python` → `python3`; verify the toolkit's imports (notably `lxml.etree` for `trafilatura`) in the interpreter that will actually run rather than trusting a system default.

## Non-negotiables

- **Plan, then wait.** Before creating a project, directory tree, configuration file, desktop Project, cron job, or publication target, present the layout, taxonomy, summary length, source list, and delivery target. Apply only after explicit confirmation.
- **Markdown is the fact source.** One entry = one portable `entry.md` with YAML frontmatter, summary, and full text. `index.csv` and any HTML reader are derived and rebuildable; never let a database or a third-party app become the only copy.
- **Thin project context.** The instance `README.md` holds taxonomy, summary spec, and usage rules; `AGENTS.md` is a short pointer plus hard bans. Do not duplicate the README into it.
- **Fixed-length summary is enforced in code.** Default range 200–250 counted characters (articles under 800 characters: 100–150); the instance README may set a different range. Count CJK characters plus Latin word tokens plus numeric tokens, ignoring punctuation and whitespace. Rewrite shorter instead of truncating, and never bypass the validator with `--force`.
- **Chinese display title, original preserved.** `title` is the Chinese display title; `original_title` keeps the source-language title and product/model names stay in their original form. Never use an English title as the display title of a non-Chinese source.
- **Chinese summary for every source,** including English articles.
- **Ground every summary in that entry's archived text.** Verify each number and substantive claim against the same article before promoting an editorial draft; another article about the same project is not evidence. A valid character count does not establish factual support.
- **Report source failures.** A failed fetch is unknown coverage, not "no news". Empty and blocked sources appear in the digest as `empty`/`blocked`.
- **Optional stays optional.** Reading UI, hosted publication, messaging delivery, and container hosting must not be prerequisites for basic archiving.
- **Do not rewrite the instance on autopilot.** An upstream skill or toolkit change never automatically edits a private archive, its cron jobs, its installed skills, or its publication targets.

## Project Shape

```text
<archive>/
├── README.md              # fact source: taxonomy, summary spec, commands, schedule
├── AGENTS.md              # thin pointer + hard bans
├── taxonomy.yaml          # broad categories, contexts, optional children, keywords
├── sources.yaml           # watched sources: feed/page/docs, extractor, limits, status
├── media.yaml             # image budgets and download policy
├── publish.yaml           # optional publication config; absent or disabled by default
├── index.csv              # derived catalog, rebuildable
├── library/<category>/<YYYY-MM-DD>-<slug>/
│   ├── entry.md           # frontmatter + summary + full text
│   ├── raw.html.gz        # original snapshot when directly fetchable
│   ├── images/            # localized images
│   └── paper.pdf          # optional attachment for paper entries
├── trash/                 # soft-deleted entries; never hard-delete
├── .state/                # seen/sources/pending/candidates/raw/backups/runs + .lock
├── bin/ca                 # CLI wrapper: add/finalize/index/fetch/search/digest/audit/daily/...
└── scripts/               # archive_url, finalize_entry, fetch_sources, rebuild_index, ...
```

Why: `entry.md` is readable in any editor and greppable; the raw snapshot survives extraction bugs; localized images survive link rot; the CSV stays convenient without becoming authoritative.

## Execution Gate

**Authorizes a manual save:** a current user message that starts with `收藏：` or `收藏:` followed by an HTTP(S) article URL, and the same intent expressed as an explicit "save/archive this article" instruction with a real URL.

**Does not authorize anything:** a bare URL; a request to summarize, translate, or comment on an article; a URL inside a quotation, code block, README/skill example, or feed listing; a discussion of archiving rules ("what would you do if I sent…"); a URL mentioned by an assistant or a scheduled job; a link the user asks you to inspect. In those cases answer the actual request and, at most, offer to archive.

When the instruction arrives outside the archive's project session (messaging bot, another project, a cron context), read that instance's `README.md` first and call the CLI by resolved absolute path — the archive root, not the chat's working directory, decides where files land.

## Manual Intake

1. **Normalize and dedupe.** Strip tracking parameters, fragment/image indices, and platform noise (`scene`, `from`); compare canonical URL and content hash against `.state/seen.json`. If the article is already archived, say so and offer to re-summarize or re-fetch instead of creating a duplicate. An explicit user save of an auto-collected entry promotes its `collection` origin to manual without duplicating it.
2. **Fetch and extract.** WeChat links: `references/wechat-article-extraction.md`. X/Twitter: `references/x-article-extraction.md`. arXiv/PDF: `references/paper-archiving.md`. Otherwise extract the main article with `trafilatura`, save the raw HTML snapshot when direct fetching works, download images within the instance media budget, and rewrite image links to local relative paths. When a site blocks direct HTML (403/Cloudflare), fall back to `web_extract`, set `extraction: web_extract`, and strip its leading `H1`/`URL:`/`Published:` header block before storing.
3. **Capture metadata.** Chinese display `title`, `original_title`, `title_status` (`original|translated|pending`), source/site, author, published timestamp, `date_source` (`feed|list|url|page|fetched`), `published_unknown`, archived timestamp, canonical URL, `source_type` (`wechat|vendor_blog|tech_blog|report|docs|other`), `category`, optional `subcategory`, `context` (`work|life|learning|reference`), optional short `relevance`, tags. Missing publish date: use the fetch date with `date_source: fetched` and `published_unknown: true`.
4. **Write the Chinese summary.** Conclusion first, one or two concrete facts, significance last. For non-Chinese sources, translate the title first and keep the original in `original_title`. Validate the counted length with the toolkit (`validate_summary.py` / `count_summary_chars`); if out of range, rewrite — never cut mid-sentence, never `--force` past the validator, never estimate the count by eye.
5. **Classify.** Apply `taxonomy.yaml` keyword rules first, then model judgment. Set `category` from the instance's broad taxonomy, `subcategory` only when a child is defined and confident, `context` by priority: user hint > vendor/tutorial/developer-tool default > `reference`. If uncertain, keep the best-supported category and report the uncertainty; do not force a category and do not promise an inbox route the code does not have.
6. **Persist and reply.** Write `entry.md`, snapshot, images; keep the body's first H1 equal to the final Chinese title; rebuild the index; reply with title, date, source, category/context, character count, link, and the summary.
7. **Screenshot provenance is part of the entry.** When the user sends a comment-section screenshot for an archived item and asks to add it, append a new section (`## 评论区（…原文照录）`) with the commenters' names, region, relative time, and like counts verbatim, mark author replies, store the screenshot under `images/`, recompute `content_hash`, bump `updated`, then rebuild the index and run the audit. Do not edit existing body text to fit new evidence.

## Titles, Summaries, Classification

- The display title is Chinese; `original_title` is not touched later, and a title change is a two-place write (frontmatter `title` and the body H1 together).
- Summary length is configuration, not taste. Read the instance README for the active range; the accepted default is 200–250 characters, or 100–150 for short pieces. Measure with the toolkit counter while drafting, because Latin words and numeric tokens each count once and an English-heavy Chinese paragraph lands well below the target.
- A summary of a partially extracted body must say so **above** the `## 摘要` heading (`> ⚠️ 摘要基于部分抓取正文`), in addition to the full-text banner; a note inside the summary section inflates the counted length and breaks the validator.
- Broad-first taxonomy: one stable folder level (`library/<category>/`), optional `subcategory` in frontmatter, `context` for the work/life/learning distinction, free-form `tags`. Do not split `work/` and `life/` into parallel trees, and do not build the first taxonomy around one industry. Schema, migration, and classification detail: `references/taxonomy-and-context.md`.

## Daily Collection and Digest

- **Checkpoints, not one shot.** `bin/ca daily start` selects and persists candidates (a bounded unseen lookback when a valid watermark exists, a shorter guard without one, per-source and global quotas, round-robin selection) and records source health; the agent archives each candidate through the same extract/summarize/classify/write path; `bin/ca daily finish` reconciles `seen.json`, verifies every selected candidate is either archived or explicitly registered as failed, rebuilds the index, audits, builds the digest, snapshots state, and prunes old run files.
- **Rebuild the index before auditing.** New entries are absent from `index.csv` until the rebuild runs; auditing first produces a false `index_row_count_mismatch` on every run with new items.
- **A failed run must stay failed.** On `daily start` failure overwrite the gate with `status: failed` rather than leaving yesterday's gate; on finish require a fresh `started_at`, a matching `fetch_generated_at`, and no failed status. Emit the toolkit's failure marker and let the scheduler treat it as a failure.
- **Digest content:** global item numbering, optional "why it matters" line, partial warning tags, a coverage section for partial and already-saved items, a header with date/new-count/dedupe-skipped/empty-or-failed source counts, and a source-status section with `ok|empty|error`, stale days, undated counts, and quota deferrals. Never silently drop a source.
- **Count semantics:** "new" counts only entries added by this run; an item archived earlier the same day is listed as already saved without incrementing the count; an unknown dedupe count prints as unknown, never `0`.
- **Scheduling, delivery targets, and asymmetric multi-channel delivery** are instance configuration — see `references/deployment.md` and `references/messaging-channels.md`. Read the instance README for its actual schedule, timezone, recipients, and enabled channels.

## Watched Sources

RSS first, in this order: explicit `feed:` → autodiscovery (`link rel=alternate` with `application/rss+xml` or `atom+xml`) → common paths (`/feed`, `/rss`, `/rss.xml`, `/feed.xml`, `/atom.xml`, `/index.xml`) → official HTML listing with a configured selector → `manual` with the gap reported. Never invent a feed.

- **Probe before editing, then wait.** Fetch the candidate feed, count items, confirm dates and whether full content is carried, and present the probe result, the YAML change, and the backfill scope for confirmation.
- **A feed works only when it yields items with a sane newest date.** HTTP 200 or a valid empty channel is not a working source; label it `empty`/`blocked`.
- **One adapter reads the source dict** for `extractor`, `language`, and `source_type`; do not hard-code one extraction path for every feed.
- **Backfill to a clean watermark.** An explicit backfill still filters `seen`, and a partial backfill leaves old posts trickling into later digests; archive the unarchived items in the current feed and require a dry run to select zero before enabling the source.
- **Trace the consumer.** Adding a row to `sources.yaml` does not prove the scheduler can archive it; exercise one candidate end to end in isolation before declaring onboarding complete.
- Adapter shapes, per-vendor status, and probe checklists: `references/vendor-sources.md`; budgets and caps: the instance `media.yaml`.

## Repair and Review

Do not mix a redesign with a data migration, and do not start with a generic phase list when a reproduced data-loss or security defect exists. Each batch needs a written spec, frozen file ownership, independent review, and acceptance commands the main agent reruns itself: entries, index rows, and seen records must agree, recorded paths must resolve, and raw/image bytes must be unchanged for untouched entries. Rehearse on a baseline copy in scratch, never against the live library. Templates, sandbox rules, negative tests, and verification commands: `references/repair-and-review.md`.

## Optional Publication

Hosting is an adapter, never a requirement. The archive is complete and correct on local disk; publication adds reach and must not become the fact source.

- **Disabled until explicitly configured.** Without an enabled `publish.yaml`, report publication as not configured instead of guessing a target. Enabling requires confirmed target identity, visibility, credentials, and a first-acceptance run.
- **Publish at completed-work boundaries** (after a validated manual-save batch, or once after a passing daily batch), not on every intermediate write; distinguish skipped, failed, pushed-but-unverified, and verified-online outcomes.
- **A clean working tree is not a review of reachable history.** Audit every commit that would become public, export only an allowlisted set of files, and keep credentials, snapshots, state, and private entries out of the publication workspace.
- Reader diagnosis, static hosting, encrypted hosting, and the publication config fields: `references/publication.md`.

## Pitfalls

- **WeChat headless browsers hit a captcha.** Use a direct HTTP GET with a desktop User-Agent and parse `#js_content`; do not loop retries against the challenge page.
- **WeChat images live in `data-src`.** Rewrite `data-src` to `src` before Markdown conversion and send `Referer: https://mp.weixin.qq.com/` when downloading.
- **`#publish_time` can be empty.** Parse `var ct = "<unix-seconds>"` or fall back to the structured metadata, then to the fetch date.
- **HTTP 200 is not a working feed, and feeds keep only recent items.** Count items, check the newest date, and keep an overlap window with dedupe.
- **Do not truncate a summary to hit the cap** and do not reach for `--force`; rewrite it shorter. A mid-sentence cut is not a summary.
- **Do not estimate summary length.** Measure it with the toolkit counter while drafting and add another grounded fact instead of stretching sentences.
- **Never make `index.csv` authoritative.** Rebuild it from entries; hand edits are lost.
- **Never store only extracted text.** Keep the compressed raw snapshot — extraction bugs and site changes are recoverable only from the original.
- **H1 scanning and intake normalization must skip fenced code.** A `#` inside a shell or Python comment is not a heading: strip a leading H1 that duplicates the title, demote remaining non-fenced body H1s to H2, and leave fenced comments intact.
- **Normalize image targets before hashing full text.** Replace `![alt](target)` with a stable placeholder and strip partial banners in the hash so localization and finalize do not change `content_hash`.
- **A re-fetch with `--force` must soft-move the old directory to `trash/`.** A `<slug>-2` sibling leaves two entries with one canonical URL and breaks library/seen parity.
- **Reserve and finalize atomically.** Exclusive directory creation with collision retry, `entry.md` via temp + `os.replace`, directory moves via rename that refuses an existing target, then the seen update under the state lock.
- **A corrupt `seen.json` must never become an empty archive.** Write a `.bak` before every save, load from it on parse failure, and stop loudly if both fail; `reconcile-seen` repairs drift against `library/`.
- **A stale gate can make a failed fetch look successful.** Require a fresh gate before treating a digest as green.
- **A complete article can end with a Markdown link.** Strip a trailing `[text](url)` or `![alt](path)` before flagging truncation, and inspect the last ~200 characters.
- **A concurrent manual save can make the audit fail spuriously.** Re-run the audit before reporting a one-off `draft`/length/index mismatch as a defect.
- **Use watermarks to choose coverage width, not as strict publication gates.** Select every unseen item in the bounded lookback, including items at or before the watermark; exclude unknown and future dates from watermark arithmetic.
- **No-date items need a visible fallback.** Fetch date plus `date_source: fetched` and `published_unknown: true`, counted in the digest; otherwise an undated item is selected once and disappears.
- **Cross-check the extracted date against article-level metadata.** Related-post cards and embedded JSON can win the extractor's scan; when `meta[itemprop="datePublished"]`, `article:published_time`, and the headline year disagree with the entry, trust the article-level metadata and re-add the entry so the id, directory, and frontmatter move together.
- **Do not fetch arbitrary URLs or local files.** Reject non-HTTP(S) schemes, loopback/private/link-local/metadata addresses, and re-check every redirect hop; restrict `--content-file`/`--raw-file`/`--summary-file` to scratch or the archive's `.state`, bounded in size and UTF-8. Resolve those paths against the archive root before the allowlist check, and validate `--subcategory` as a slug, never a path segment.
- **Clearing partial status must clear `content_note`,** or a later re-partial reuses a stale banner and count.
- **A manual frontmatter edit leaves the body H1 stale.** Re-run finalize with the same title instead of hand-editing the heading.
- **Diagnose media budgets separately.** Image count, per-file size, aggregate bytes, and download time are distinct limits; an omitted oversized image is not evidence of incomplete article text — record the reason and keep the remote URL.
- **Report scope honestly.** Local tests, parsed configuration, and a remote TCP refusal prove less than runtime acceptance; say which layer you actually verified.

## Verification

- [ ] Instance `README.md` was read and its taxonomy, summary range, and schedule were followed.
- [ ] Project layout, README, AGENTS, taxonomy, sources (plus media and any publication config) exist.
- [ ] One WeChat article and one generic blog article archived with title, date, URL, full text, and localized images.
- [ ] Summary passes the instance counter and is not truncated mid-sentence.
- [ ] Every entry's body H1 equals its frontmatter title; `status: partial` entries carry a reason in `content_note` and in the digest.
- [ ] Re-submitting the same URL does not create a second entry.
- [ ] A scheduled run produces a digest with title/date/link/summary, source status, and a failure or coverage section.
- [ ] The digest count equals entries added by this run, not every entry archived that calendar day.
- [ ] `index.csv` row count equals the number of `entry.md` files.
- [ ] The audit reports zero title/summary/heading/image/raw/seen issues; snapshots and images resolve.
- [ ] A zero-item source is `empty` (not `ok`), does not advance the watermark, and appears in the digest.
- [ ] An undated item is retained with `date_source: fetched` and `published_unknown: true`.
- [ ] The CLI rejects `127.0.0.1`, `169.254.169.254`, and `--content-file /etc/hostname`.
- [ ] Finalize leaves no half-moved directory; `reconcile-seen` reports no drift on a clean library.
- [ ] The taxonomy has broad top-level categories plus `context`, with no `work/` and `life/` folder split.
- [ ] One umbrella skill covers manual and scheduled paths; no per-vendor or per-category skills were created.
- [ ] Publication is disabled or explicitly configured; no credentials, recipients, or machine paths were written into the repository.

## References

- `references/deployment.md` — new-instance layout, virtualenv, cron checkpoints, delivery configuration, upgrade and backup rules.
- `references/taxonomy-and-context.md` — three-axis model, broad categories, `taxonomy.yaml` schema, subcategory migration.
- `references/vendor-sources.md` — source status, probe checklist, adapter shape, run policy.
- `references/messaging-channels.md` — chat intake and digest delivery, routing caveats, secrets handling.
- `references/repair-and-review.md` — phased repair, subagent protocol, sandbox rules, negative tests, acceptance commands.
- `references/publication.md` — reader hosting, optional static host, encrypted publication, config fields, layered acceptance.
- `references/self-hosted-archive-tools.md` — companion-app capability matrix and decision rules.
- `references/wechat-article-extraction.md` — selectors, metadata, image localization, failure modes.
- `references/x-article-extraction.md` — X posts and long-form Articles, image unwrapping, metadata.
- `references/paper-archiving.md` — arXiv/PDF metadata, SVG figures, math alttext, PDF attachment.
- `templates/entry.md` — starter entry with frontmatter and summary/full-text sections.
