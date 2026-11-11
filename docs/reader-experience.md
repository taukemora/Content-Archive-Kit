# Reader formatting, ordering and export capacity

This document describes the locally accepted development candidate, not a
released or deployed update. The first independent review was conditional;
its targeted repairs passed parent reproduction, whole-candidate regression
and focused independent re-review. Private-instance upgrades, source-entry
changes, publication and storage migration require separate approval.

## Source formatting

Article extraction now normalizes supported inline `font-weight` and
`font-style` declarations before Markdown conversion. Cross-block emphasis is
split into paragraph-local runs; explicit `normal` overrides and `!important`
precedence are respected. Native heading tags retain their levels. A short bold
paragraph does **not** automatically become a heading.

This is a bounded converter, not a CSS rendering engine. It does not resolve
external stylesheets, CSS classes, `font` shorthand, font families or colors.
The existing caption/video pipeline remains authoritative, and raw article HTML
is still disabled in the reader. The original HTML snapshot is retained by the
existing intake path when available. Existing Markdown entries do not change
merely because the extractor is upgraded.

The normalizer is called once per extraction. Re-normalizing HTML that still
carries inherited root CSS is not guaranteed idempotent; this is distinct from
rerunning extraction against the unchanged original snapshot.

For inline emphasis next to ordinary text, boundary punctuation can make
CommonMark display the emphasis delimiters literally. The converter moves the
affected leading/trailing Unicode punctuation outside the emphasis range;
visible characters and their order stay unchanged, but that punctuation loses
its bold/italic styling. Punctuation-only emphasis becomes plain text. No spaces
or invisible characters are inserted. This local compatibility rule does not
move text across link/code boundaries; emphasis whose edge is itself an inline
element, unknown inline elements and complex nested delimiter combinations are
not comprehensively normalized and can still show literal markers.

## Reader ordering

The filter area provides **最近归档** (recently archived, the default) and
**最近发布** (recently published). Switching order retains the search/filter,
selected article and article scroll position.

- Archived ordering uses archive time, then publication time, then stable ID.
- Published ordering uses publication time, then archive time, then stable ID.
- Unknown, invalid or explicitly fetch-fallback publication dates appear last
  in published ordering and display **日期未知**.
- Offset-bearing timestamps are compared as UTC instants. Legacy naive
  timestamps use the archive's existing `+08:00` convention. The displayed date
  remains the source-local date, without displaying an invented time of day.
- Date-only values remain date-only in the catalog. For ordering, they use the
  start of that UTC calendar day and sort below an exact timestamp at that same
  boundary; this is a comparison convention, not newly established date evidence.
- Supported timestamp fractions have one to nine digits. Invalid offsets and
  conversions outside years 0001–9999 are rejected rather than aborting export.
- New catalog fields `published_sort` and `archived_sort` carry comparison
  values. The reader falls back to older catalog date fields when needed;
  precision already lost in an old export cannot be recovered in the browser.

Only `ca.reader.sort=archived|published` is persisted in local storage. Article
text, search text, credentials and decryption material are not stored there.
Missing, disabled or corrupt preference storage falls back safely. Preference
persistence may differ between browser origins and `file://` implementations.
The encrypted shell remains empty of article data until unlock.

## Read-only backfill candidate workflow

The new CLI entry point is:

```bash
bin/ca format-backfill plan --source "$ARCHIVE" --out "$REVIEW/plan.json"
bin/ca format-backfill prepare --source "$ARCHIVE" \
  --plan "$REVIEW/plan.json" --dest "$REVIEW/new-candidate"
```

`ARCHIVE` is an explicit archive root, not an inferred working directory.
`REVIEW` must be outside both the source and this toolkit checkout. `prepare`
requires a new destination and refuses an existing directory, symlinks and
source overlap. There is deliberately **no `apply` command**.

`plan` records per-entry status (`changed`, `unchanged`, `needs_review` or
`unsupported`), reasons, source/raw/candidate hashes, a readable diff and a plan
digest. Explicit reviewed heading selections may be supplied with
`--selections selections.json`; the JSON maps a relative entry path to exact
standalone heading text, for example:

```json
{"library/tech/synthetic/entry.md": ["合成小节标题"]}
```

Ambiguous, missing, edited or unsupported source must be reported rather than
reconstructed by guesswork. `prepare` rechecks the source and plan before
creating a candidate patch tree; it does not update source entries, state or
indexes, download media, or publish. The receipt identifies copied candidates
and whether an eventual separately approved application would require index
rebuilding. This patch tree is not a complete replacement archive.

The first backfill implementation failed independent checks for unmarked
captions, heading-only promotion, repeat planning after promotion, CRLF
preservation and truncated gzip handling. Targeted repairs passed those five
checks, followed by the whole-candidate regression and independent reviews
recorded below. These results do not authorize application to a live archive.

Caption recovery requires exact raw-derived text and a recognized caption
shape (native caption, the existing small/grey heuristic, or an image-adjacent
literal `图注：` paragraph). Ordinary neighboring prose is not guessed into a
caption. Entries containing carriage returns or invalid UTF-8 are refused
without rewriting them. A damaged/truncated gzip is reported per entry rather
than aborting the batch. Heading-only promotions are changes, and planning
again with the same reviewed selections must be byte-idempotent.

Punctuation-boundary repairs can also be proposed for old entries when their
snapshots match: a fallback alignment accepts only emphasis-star insertions or
deletions, never a punctuation or narrative rewrite. Unreadable `entry.md` files
produce an `unsupported` row with an unknown source hash, while other readable
entries continue through planning.

Plans and candidate trees can contain private article text and local paths.
Keep them outside the public repository, in owner-only review storage. Review
individual diffs before authorizing any live application. Neither a candidate
receipt nor a passing synthetic test establishes that a live article or website
changed.

## Export capacity

`bin/ca publish status` reports capacity for the current validated export without
building or publishing it. `publish build` and `publish run` include the report
when the corresponding export exists; their normal build/publish side effects
remain unchanged, so do not use `run` merely to inspect capacity.

The `capacity` object reports actual allowlisted file count, total bytes, static
shell bytes, ciphertext-object bytes, largest file, the local limit and usage.
It does not decrypt objects or invent a split between article text and images.
Missing or invalid exports report `status: unavailable` with a reason, not a
misleading zero.

The existing **512 MiB local total limit** is unchanged, as are the per-file and
file-count safety limits. At **75%** the report is `warning`; at **90%** it is
`critical`. Thresholds use integer comparisons; the displayed percentage is
truncated so rounding cannot falsely cross a threshold. These warnings do not
raise or bypass an existing publication limit.

This measures one current export. It does not measure Git history, hosting
bandwidth, account quota or future growth. The toolkit's 512 MiB guard is not a
statement of GitHub Pages' platform limit. Consult the current
[official Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)
when planning hosting. Splitting the shell from encrypted object storage or
moving the site to another host is a separate migration, not implemented by
this capacity report.

## Local candidate acceptance

The tested implementation is based on
`d48ec273691275afb6e12c7b2306020412eec570`. A clean, frozen Python 3.13 export
installed both declared requirements files with `PYTHONPATH` cleared; `pip
check` reported no broken requirements. The full suite completed **870 tests:
869 passed, one skipped, zero failures or errors** with
`CA_RUN_BROWSER_TESTS=1` and `CA_RUN_INSTANCE_TESTS=0`. The sole skip is the
deliberately disabled production-instance dry run. The browser tests use real
Chromium with synthetic fixtures, not the production reader or its keys.

- Source-format tests exercise the actual extractor and safe Markdown renderer,
  not only the generated Markdown string. Parent probes independently reproduced
  and then closed the Unicode punctuation-flanking and unreadable-entry findings.
- Backfill tests exercise the actual CLI `plan → prepare → plan` transition,
  exact candidate reconstruction, unchanged source/raw/media bytes, reviewed
  heading idempotence and refusal of ambiguous or unsafe inputs. This proves
  the candidate workflow, not a live `apply` operation.
- Ordering browser checks cover offline, legacy and encrypted reader modes,
  preserving search, filters and selected-article state. Separate date-boundary
  checks cover invalid offsets, small years, UTC overflow and fractional seconds.
- Capacity tests cover exact warning boundaries, invalid/missing exports and
  unchanged publication limits. Storage negative controls reject writes outside
  the one approved sort-preference module, key or enumerated value.

The implementation author used `deepseek-v4.1-flash`; the independent reviewer
used `z-ai/glm-5.3-flash`. The first whole-candidate review returned
**CONDITIONAL**, identifying literal emphasis markers beside Unicode
punctuation and entry-read errors aborting a batch. After repairs, the focused
review returned **PASS** for those findings and the marker-only backfill
integration, with no new blocking finding in that scope. It did not repeat the
earlier sorting/capacity review or the parent's full browser regression.

Accepted implementation SHA-256 values (subsequent acceptance-document and
public-manifest edits do not change these runnable bytes):

| File | SHA-256 |
| --- | --- |
| `bin/ca` | `fdeb6acbb31174e0225f04fc7765b16b187383a5243c72f98c28f68c96cd72ee` |
| `scripts/archive_page.html` | `f7de4b8821056b87e40d98ea4bcde37cdba3da2da91b15b8be56a063ef31ef85` |
| `scripts/archive_url.py` | `5d5f0d54fe5e197361dc633a9e85fc21ff67e0eebe2c4e3911516e3c4d9d7b17` |
| `scripts/article_formatting.py` | `018f0499eb112499f7a4ca453a2e8974dcd04306e5634b79e7efdc04b601f4fe` |
| `scripts/build_html.py` | `f2980320d803d4840242b5f7734d2423243711d497a33810b449ae75dc9566fa` |
| `scripts/format_backfill.py` | `2097b6bfc1a60d983b093a3dda623538ed3e6dfbde93c6b800002921ade6243c` |
| `scripts/publish_site.py` | `b08d3dc0684d4a1f9c081c1895d0a93781cdcf7025a46fd9e3b8a82a7c9759f9` |

### Remaining boundaries

This is not exact-commit hosted CI, a release, a production unlock test, a
private-instance installation or a storage migration. No production archive,
installed skill, scheduled job or publication target is changed by this local
acceptance. The bounded converter limitations above remain: complex nested
emphasis can still show markers, affected boundary punctuation loses emphasis,
and unknown CSS/inline structures are not reconstructed by guesswork. Review
every proposed old-entry diff; `needs_review` and `unsupported` are deliberate
refusals, not successful repairs. Preserve private plans outside this repository.
