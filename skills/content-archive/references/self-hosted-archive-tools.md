# Self-Hosted Archive Tools — Capability Matrix

Use when deciding whether the archive needs a companion application. Verify current feature sets and activity before quoting them; this table is a decision aid, not a permanent spec.

| Tool | What it is | Full-text / page archive | AI summary | Categorization | RSS ingestion | Chat intake | Best role |
|---|---|---|---|---|---|---|---|
| Karakeep (formerly Hoarder) | Bookmark-everything app | Readability + monolith full page | AI tagging + summarization; length not per-item controllable | Tags, lists, rule engine | Hourly feed subscriptions | No | Closest single product; optional UI/search layer |
| Linkwarden | Collaborative bookmark manager | Screenshot, PDF, single-file HTML | AI tagging only | Collections, sub-collections, tags | RSS subscriptions (~60 min) | No | Polished collections UI + preservation |
| ArchiveBox | Web archiving | WARC, PDF, HTML, screenshot, media | No | Tags, weak taxonomy | Scheduled RSS imports | No | Preservation layer |
| Readeck | Read-later | Readable content + images | No | Labels, collections | No native subscriptions | No | Lightweight reader |
| wallabag | Read-later | Extracted content | No | Tags | No native import | No | Mature reader/ecosystem |
| FreshRSS | RSS aggregator | Feed cache, XPath scraping | No | Tags/categories | Native, plus HTML+XPath for no-feed sites | No | Collection layer |
| Wechat2RSS | WeChat → RSS | Full-text feed output | No | — | Subscription service | No | Fixed WeChat accounts only |
| RSSHub | Feed generator | No archive | No | — | Routes | No | Supplement for sites without feeds |
| Indelible | Read-later/archiver | Full content | AI assistant | — | Feeds | No | Watch; too new for production |

## Decision rules

- If the user wants **chat intake + fixed-length summaries + taxonomy + project-scoped digest**, start with this toolkit. No single app covers all four.
- Add **Karakeep** when browser/mobile capture, highlights, OCR, or semantic search is worth a multi-container service. Keep the Markdown archive as the editorial fact source and push copies to Karakeep; do not make it the only store.
- Add **Linkwarden** when collections and page preservation matter more than AI summaries.
- Add **ArchiveBox** only as a preservation backstop for high-value pages; it is not a reading workflow.
- Add **FreshRSS/RSSHub** only for sources that are otherwise hard to poll; the archive toolkit still owns dedupe and storage.
- For WeChat, prefer direct article fetch for ad-hoc links (`references/wechat-article-extraction.md`). Use Wechat2RSS only for a fixed set of accounts the user already subscribes to, and treat its availability as fragile.
- Beware dual-store drift: designate one store as canonical (Markdown) and one as mirror (app), and never edit both by hand.

A short version often suffices in conversation: Karakeep is the closest single product but has no chat intake or per-item fixed-length summary; Linkwarden is strong for collections and preservation without per-item summaries; ArchiveBox is a preservation layer; FreshRSS is a collection layer.

## Questions to resolve before recommending a hybrid

1. Does the user need mobile/browser capture or a web UI?
2. Is semantic or OCR search required, or is ripgrep over Markdown enough?
3. Is the user willing to run and maintain container services on their own hardware?
4. Who owns the canonical copy, and how will the mirror be rebuilt?
5. Does any candidate preserve article images and a raw snapshot, or only a readable text rendering?

## Hosting the existing reader

If the question is really "how do I host the reader I already generate" rather than "which app should I add", do not add an app. Reader diagnosis, a minimal read-only static host, and acceptance layering are in `references/publication.md`.
