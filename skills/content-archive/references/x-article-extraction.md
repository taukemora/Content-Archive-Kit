# X (Twitter) Post and Article Extraction

Use for `x.com/<handle>/status/<id>` links. Short posts carry only text; long-form X Articles are server-rendered into the page HTML and can be archived the same way.

## Fetch

- GET the status URL with a desktop Chrome User-Agent; X serves the page and its Article body without login.
- Read `og:type`: `article` means a long-form Article. `og:title` is `<author> on X: "<headline>" / X`, and `article:published_time` is the UTC timestamp.
- Keep the HTML as the raw snapshot.

## Extract

| Field | Source |
|---|---|
| Author display name | `og:title` segment before ` on X:` |
| Handle | the `/<handle>/status/` path segment |
| Headline | quoted text in `og:title`, or the Article's first heading |
| Published | `meta[property="article:published_time"]` (UTC) |
| Article body | `div.x-article-body` |
| Short post body | `div[data-testid="tweetText"]` |

Convert `div.x-article-body` with `markdownify(..., heading_style="ATX")`. The page can carry two such blocks (desktop and mobile layouts); take the first and verify the char count and tail before using it. The body block is clean; navigation and follow prompts sit outside it. Drop the trailing author-promo block only if the project strips promos elsewhere.

## Images

- Article images live on `pbs.twimg.com` and are commonly wrapped in an internal link: `[![](img)](/<handle>/article/<id>/media/<media-id>)`.
- Unwrap to a bare `![alt](img)` first; a wrapped image leaves a relative link that only resolves on x.com.
- Resolve any remaining relative links against `https://x.com`.
- If the archive's direct-egress session cannot reach the image host (it reports a connection failure while the same URL fetches fine through the proxy), pre-localize:
  1. Use the exact `https://pbs.twimg.com/media/<id>.png` URL from the page HTML. Guessing a size variant (`?name=large`, `?format=jpg`) can return 404 for the same asset.
  2. Download to scratch with `curl` (the egress that reaches the host), one file per image, keep article order, and confirm the PNG magic bytes and a non-zero size.
  3. Rewrite the Markdown to `images/001.png` … **before** `bin/ca add --content-file`; the localizer skips `images/` targets, so the add finishes immediately instead of paying one timeout per image.
  4. `add` creates the entry directory, so copy the downloaded files into `<entry>/images/` afterwards and set the frontmatter `images: <n>` and `image_note` yourself — add recorded `images: 0` because it skipped them. Re-run index and audit.
  If the entry was already created with failed image downloads, re-run add with `--force` (the failed draft is soft-moved to `trash/`) using the rewritten content file, then copy the images into the new directory and fix the count.

## Metadata conventions

- `source`: `X（@handle）`; `author`: display name plus handle; `source_type: other`.
- `title`: Chinese restatement of the headline; the original headline goes in `original_title`.
- A short post (<800 characters) uses the 100–150-character summary range; an Article uses 200–250.

## Failure modes

- A deleted or protected status returns a login shell with no `x-article-body`; report it, never fabricate the body.
- The HTML also embeds the post as escaped JSON (`full_text`); use it to confirm the visible text, not as the primary body.
- `og:description` often repeats the headline only; never summarize from it.
- Bulk adds against X can leave an entry directory without `entry.md` when image downloads stall; treat that directory as partial, move it to `trash/`, and retry after pre-localizing images.
