# Reader formatting, ordering and export capacity

This document describes implemented reader behavior and its verification
boundaries. Installing an update does not apply old-entry repairs or publish
an archive; those operations require separate review and approval.

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


The **浏览藏文** heading row provides one native dropdown at its right edge:
**最近归档优先** (recently archived, the default) and **最近发布优先**
(recently published). It replaces the former hint and separate two-button row.
The control uses the existing muted palette, regular 12px text, a subtle border
and chevron, a 44px minimum hit height and the shared keyboard-focus ring.
The browser/OS owns the native popup's appearance. Switching order retains the
search, category, publisher and collection filters, selected article and article
scroll position; the comparator and preference key are unchanged.

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

## White-on-grey caption badges

In addition to explicit `figcaption` and the existing caption heuristic, the
WeChat extractor recognizes a narrow image-adjacent badge shape: a standalone
short paragraph whose meaningful text runs all have small white text on a
neutral grey background. Foreground, background and size must come from the
same run's inline element chain **inside the candidate**, not unrelated siblings
or a tiny decorative span. Right alignment or shortness alone is insufficient.
Headings, lists, quotations and mixed block content do not qualify through this
new branch. Recognized text becomes the existing explicit Markdown caption and
safe `figure`/`figcaption`; the source's grey badge styling is not copied.

The supported sizes are strictly positive and at most 14 CSS px (`pt` is
converted at 96/72). `em`/`rem` are not inferred from an assumed root size. White
supports `white`, hex, comma-separated `rgb` and opaque `rgba`; grey supports
hex/rgb/opaque rgba. Transparent, translucent, non-finite and unsupported values
are rejected without aborting extraction. Same-element declaration order and
`!important` are respected, and explicit descendant overrides remain local to
their own runs. `background`, `font` and `all` declarations anywhere on the run
chain conservatively reject this branch, even when they might be harmless.

This is still a bounded structural/style heuristic, not computed browser CSS or
semantic understanding. External stylesheets/classes, visual effects and
shorthand expansion are not evaluated; unsupported structures can be missed.
Identically styled adjacent prose can still be indistinguishable from a caption.
The historical grey/source/centred heuristic is unchanged, including its own
limitations. Existing entries require a reviewed backfill; upgrading extraction
does not automatically rewrite them.

## Candidate-only backfill workflow

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

Earlier backfill candidates failed checks for unmarked
captions, heading-only promotion, repeat planning after promotion, CRLF
preservation and truncated gzip handling; the acceptance record below covers
these five checks in the current candidate. These results do not authorize
application to a live archive.

Caption recovery requires exact raw-derived text and a recognized caption
shape (native caption, the existing small/grey heuristic, the bounded white-on-
grey badge above, or an image-adjacent literal `图注：` paragraph). Unstyled
neighboring prose is not guessed into a
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

## Caption/dropdown local acceptance

Implementation and tests were frozen on baseline
`79cc04edfb825d7fa5520b5ba3917d2e8259136c`. Local Python 3.13 regression
with real Chromium and process-local CJK fonts completed **903 tests: 902
passed, one intentional production-instance skip, zero failures or errors**.
The reviewed pre-documentation snapshot inventory digest was
`21c0e48544a7fafbdbd39090568ffc638ca709d27cb7a29a2b228db70046f562`.
Final documentation and its checksum inventory are separate from that digest;
executable, test and workflow bytes must remain identical to the tested snapshot.

- Eight badge boundary probes were replayed independently, all passing. These
  include non-finite colors, shorthand/reset overrides, pt conversion and zero
  size; extraction/rendering/backfill regressions are in the full suite.
- Six offline/encrypted viewport combinations (320, 390 and 1280px) produced
  twelve screenshots, inspected for intact Chinese labels, no heading/control
  overlap, a restrained palette and visible keyboard focus. Real-browser tests
  also cover stored preference, disabled storage, filtering, selection and
  reading-position preservation.
- A separate rendering check confirms that no active script or
  JavaScript-scheme link is emitted.
- The media-browser probe observes geometry when each request starts and holds
  response bytes until the mobile pane is hidden. It requires a pending eligible
  job to resume without remount or scroll. This replaces stale-placeholder
  geometry and timing assumptions that caused false failures in the previous
  probe. Scratch-only fetch-all, missing-resume and hidden-fetch mutants fail
  the intended behavioral assertions. Production scheduling code is unchanged;
  reruns also pass without the preview font override.
- Caption recognition does not replace renderer sanitization. Some existing
  extraction paths can retain unsafe Markdown links; the safe renderer rejects
  JavaScript-scheme links and active scripts.

Non-blocking limits remain: mobile request-time observations do not have the
desktop phase's observer/server-count equality check (the current transport is
`fetch`, and hidden/resume assertions use server events); dropdown style tests
pin the current accent and tab order; identically styled adjacent prose remains
semantically ambiguous. Native OS popup appearance and physical devices were
not accepted. These local results do not establish hosted CI, a release,
private-instance installation, live article backfill or publication.

## Original formatting/ordering/capacity acceptance

The tested implementation is based on
`d48ec273691275afb6e12c7b2306020412eec570`. A clean, frozen Python 3.13 export
installed both declared requirements files with `PYTHONPATH` cleared; `pip
check` reported no broken requirements. The full suite completed **870 tests:
869 passed, one skipped, zero failures or errors** with
`CA_RUN_BROWSER_TESTS=1` and `CA_RUN_INSTANCE_TESTS=0`. The sole skip is the
deliberately disabled production-instance dry run. The browser tests use real
Chromium with synthetic fixtures, not the production reader or its keys.

- Source-format tests exercise the actual extractor and safe Markdown renderer,
  not only the generated Markdown string. Independent probes reproduced and
  then closed the Unicode punctuation-flanking and unreadable-entry findings.
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

Regression coverage includes literal emphasis markers beside Unicode
punctuation, unreadable entries, and marker-only backfill integration.

Historical implementation SHA-256 values for the original candidate, not the
later caption/dropdown candidate (the current inventory is
`docs/public-tree-sha256.json`):

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
