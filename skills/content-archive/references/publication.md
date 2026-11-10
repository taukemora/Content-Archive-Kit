# Optional Publication

Use when the user asks for a hosted reader, a NAS-hosted copy, or publication of selected entries. Publication is an adapter over the Markdown archive: it must stay optional, re-buildable, and unable to become the fact source. No real repository, host, credential path, or recipient belongs in this repository.

## Before anything: is it a transport problem?

If the archive already generates a searchable HTML reader, separate a broken transport from a missing product feature before proposing another application.

1. Obtain the exact browser, address, and access method: local disk, network/WebDAV mount, attachment preview, downloaded HTML, or HTTP. Test the specific failing article; not every image fails for the same reason.
2. Confirm the referenced image exists and inspect its resolved URL. Working text with broken relative images is a packaging problem, not a missing archive.
3. Compare the complete archive copied to local disk with the copy the user actually opens, preserving the HTML/`library/` positions.
4. Inspect normal lazy loading before forcing eager loading; a manipulated test does not describe the user's reading path.
5. Only then propose a read-only static HTTP service reusing the existing HTML and image tree. Hosting cannot recover images that were never archived, and a single-file base64 bundle is not automatically simpler — measure the expansion and the transport limits.

## If publication is wanted

### Configuration

An instance opts in with a `publish.yaml` beside `taxonomy.yaml`. The outcome is explicit, not silent:

- **No file** → publication is skipped as "not configured". Report that instead of guessing a target.
- **`enabled: false`** → skipped, and basic archiving is unaffected.
- **A file that does not satisfy the schema** → recorded as a failure with the validation message. An invalid configuration is visible, never quietly treated as disabled.

`examples/publish.yaml` documents the shape without containing a real target, account, or usable path. The loader is strict about unknown keys, and the required keys must be present even while disabled.

Rules that hold for every adapter:

- **Explicit target.** Confirm owner/repository, branch, visibility, and URL before connecting anything; a private repository does not imply a private site, and an unlisted URL is not an access boundary.
- **Allowlist export.** Export only the reader program and the artifacts the user approved. Never export the archive root, `.state/`, raw snapshots, trash, configuration, or credentials.
- **Keep the archive separate.** Do not initialize a repository inside the archive to enable publishing, and never let hosting history hold plaintext of content the user wanted private.
- **Failures are local first.** Persist the successful local archive before any network step, and record publication separately as skipped, failed, pushed-but-unverified, or verified-online. A failed upload must leave local files and the previous site usable.
- **Publish at completed-work boundaries** — after a validated manual batch, or once after a passing daily batch — and suppress nested publication during inner steps so one checkpoint publishes once.
- **Verify the deployed artifact, not the push.** A successful push is not a deployed site; check the exact online marker the adapter writes.

### Shipped adapter: encrypted reader on GitHub Pages

The adapter bundled with this toolkit exports an encrypted reading site to a GitHub Pages repository. Its configuration fields are fixed; the loader rejects unknown keys.

| Field | Meaning |
|---|---|
| `enabled` | Master switch; false means skipped, not broken. |
| `repository` | `OWNER/REPOSITORY` — the one repository this archive may push to. It is the allowlist. |
| `branch` | Branch the export is pushed to; validated as a safe ref name. |
| `site_url` | Must equal the Pages URL derived from `repository` (`https://<owner>.github.io/<repo>/`), so a stale or copied URL is rejected. |
| `key_file`, `ssh_key_file`, `known_hosts_file` | Absolute paths outside the archive; the three must be distinct. |
| `workspace` | Absolute scratch checkout outside the archive; holds the export's only Git worktree, the encrypted build output, a state file with no secrets, and a publish lock. |
| `git_name`, `git_email` | Commit identity inside the publication workspace; neutral defaults apply when omitted. |
| `pages_timeout_seconds` | Optional timeout for the deployed-site verification step. |

Operational notes that follow from the adapter's design:

- The remote is derived from `repository`; there is no independent remote setting to mismatch.
- The adapter's own commands separate the steps: `build` exports locally without touching the network, `status` reports configuration and key readiness, `init` sets the passphrase and creates the external site key on a TTY, and `run` builds, validates the allowlist, pushes, and verifies the deployed Pages version. `build` and `status` are allowed while disabled, so a first launch can be prepared and checked without publishing; none of them flips the switch.
- Passphrases are refused as command-line arguments and are requested interactively, so a scheduled job must not try to pass one.
- Key material is never read during configuration loading; only the publish path touches it.

### Readiness for a frontend password

Generated HTML readers embed full text and search data. Publishing the HTML alone republishes the article text even if the article UI is hidden, and `noindex` or an unlisted URL is not authentication.

- For a summary-only public view, omit unapproved full text from the generated data and search index at build time, and exclude original images and snapshots unless rights permit sharing.
- For an owner-only reader, enforce authorization at the platform or gateway level and verify it against HTML, search payloads, and direct image URLs using an unauthorized session.
- Client-side encryption can protect a payload, but separately referenced images, attachments, and search indexes stay unprotected unless included. Keep keys out of deployed bundles, and do not claim immunity from a compromised hosted decryptor.
- Confidentiality controls reduce dissemination; they do not create copyright permission for third-party uploads. Assess recipients and rights, not the presence of a login screen.
- Middleware on prerendered pages runs at build time, not per visit; keep a static reader behind whole-site authorization or deliberately serve authorization at runtime, and separately protect static assets.

### Encrypted publication, when explicitly requested

Ciphertext-only hosting is a defensible choice for an owner-only reader; implement it only when the user asks for it, and never substitute a public origin or a gateway-only design silently.

- Encrypt locally before upload, including titles, summaries, search data, and supported media. A build service that receives the key and publishes decrypted data defeats the boundary.
- Export one allowlisted program plus ciphertext; account for offline guessing, non-revocable downloaded key/ciphertext pairs, and the host's deployment snapshot behaviour.
- Audit every commit that would become public, not only the staged diff: a clean tree can still carry a plaintext parent. Enforce a closed path set and an exact manifest schema, reject foreign or symlinked history, and preserve retry after a failed first push.
- Treat the generated shell as part of the contract: require the encrypted marker and empty data placeholders, and reject duplicate attributes before converting them, because browsers and parsers disagree on which duplicate wins.
- Measure scaling at separate boundaries — locked shell, unlocked catalog/search, selected article/media, transfer, deployment snapshot, retained history. Lazy-loading HTML does not defer an earlier JavaScript fetch, and reuse of unchanged ciphertext does not prove the exporter avoided re-rendering the whole archive.
- Benchmark with a frozen copy and a test-only key; never use production credentials for a performance probe, and state that local browser timings are not network performance guarantees.

## Static hosting on a private machine (NAS, home server)

These are requirements and acceptance steps, not a claim that any particular host has passed them.

1. **Freeze scope and ownership.** Confirm the real filesystem path, listening interface, an uncommon host port (avoid default 8080/8088), and who starts the service. When the user reserves startup, prepare artifacts without taking over the deployment; explain that the container's internal port is separate from the host port.
2. **Keep the runtime small.** Reuse the existing index generation and synchronization. Prefer one maintained non-root static-server image, a Compose file, and a server configuration; do not add a database, backend, Dockerfile, watcher, or a second publication tree without a concrete need. Pin a real image digest, and verify architecture and configured user.
3. **Mount for atomic updates.** Bind-mount the containing directory read-only rather than a single `index.html`, because an atomic replace swaps the inode while a single-file mount keeps the old one. Disable host-path auto-creation so a wrong source fails loudly. Read-only storage is not access control.
4. **Serve an allowlist.** Permit only the reader routes and existing article-image paths; deny directory listing, traversal, symlink escapes, and write methods. Keep the server's image-extension set equal to the generator's, including case handling. Never answer a missing image with the reader HTML.
5. **Keep security without breaking graphics.** Do not weaken CSP to work around a path problem. Constrain image responses separately, including direct SVG navigation, and test both a normal graphic and a synthetic hostile one. Remember that a nested location with its own headers usually stops inheriting parent headers — restate the ones you still need.
6. **Minimize writable areas.** Check the chosen image's PID and temp paths before adding tmpfs mounts; unprivileged web-server images often relocate them. Never fix unreadable host content with world-writable permissions; propose a narrow read ACL or group and get approval. Verify the bind source's owner/UID against the container user instead of adding root.
7. **Keep update semantics explicit.** Revalidate mutable HTML and image paths rather than caching them immutably, and test same-path replacement without a restart. Cross-file synchronization is not a transaction: wait for it to finish before reading.
8. **Publish privately.** Bind a confirmed interface, not all interfaces. A required Compose variable prevents omission, not an unsafe address, and LAN binding does not imply remote-access support. State plainly that an unauthenticated private reader is reachable by anyone who can reach its port.

## Layered acceptance and honest handoff

Report these layers separately; never let a green lower layer stand in for a higher one.

- **Configuration:** parse the real Compose file (`docker compose config -q`). This validates syntax, not bind paths, permissions, or behaviour.
- **Local server:** run the server's configuration check and a standalone binary; this establishes HTTP behaviour only.
- **Container:** require actual startup and a successful HTTP response before calling container execution verified.
- **Content:** enumerate image references from the generated HTML, count and deduplicate programmatically, GET each, and compare bytes plus MIME/security/cache headers. In a real browser test filters, search, Chinese paths, and SVG display; keep remote-only media separate from broken local media.
- **Boundaries:** on a scratch copy, probe hidden/config/state/raw paths, directories, missing images, write methods, encoded and double-encoded traversal, and image-named symlinks. Preserve production hashes across destructive probes.
- **Host endpoint:** browser acceptance on the real address remains the user's or owner's step. A refused remote connection proves neither host-local port availability nor filesystem readability.

Deliver exact start/check/log/stop commands, the confirmed path/address/port, rollback behaviour, and a short list of target-only prerequisites that remain unverified.
