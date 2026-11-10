# Archiving Papers (arXiv and PDF Sources)

Use when the user sends an arXiv link, or both an HTML rendering and a PDF of the same paper. One paper = one entry: dedupe by arXiv ID, not by URL, and keep the PDF as an attachment instead of creating a second entry.

## Fetch

| Purpose | URL |
|---|---|
| Metadata | `https://arxiv.org/abs/<id>v1` — `citation_title`, `citation_author`, `citation_date` |
| Body | `https://arxiv.org/html/<id>v1` |
| Original | `https://arxiv.org/pdf/<id>` |

## Extraction

- Parse the `<article>` element; drop `nav`, `script`, `style`, and the author/email head block. arXiv emits nested `mailto:` links that Markdownify turns into unreadable garbage; authors belong in frontmatter, not the body.
- Math is `<math alttext="...">`. Replace every node with its `alttext` before conversion, or all equations vanish from the text.
- Figures are not always `<img>`. arXiv renders many as `<object type="image/svg+xml" data="<id>v1/....svg">`, plus a PNG for photo figures. Download each object's `data` URL in figure order to `images/NNN.svg` (or `.png`) and keep the caption as italic text under the image.
- Tables convert to Markdown pipe tables; verify no `<table>` remains in the output.
- Remove the leading H1 (the entry supplies its own Chinese title) and turn `Abstract` into `## Abstract`.

## Entry assembly

- `source: arXiv`, `source_type: report`, authors comma-separated from `citation_author`.
- Copy the PDF into the entry as `paper.pdf` and add one provenance line under `## 全文`: arXiv ID, first author/affiliation, `[paper.pdf](paper.pdf)`.
- Point `raw_file` at the saved HTML; set `images:` to the real count and describe the figure formats in `image_note`.
- Long papers reach 100k+ characters with appendices. Keep the full text; summarize from the abstract plus the results sections.

## Summary grounding

Read the experiments section before writing. Numbers in the abstract are safe; numbers from appendices need the appendix text. Quote exact benchmark figures rather than rounding them, and state the paper's own stated limitations when present (for example a target the method did not reach) instead of presenting only headline gains.
