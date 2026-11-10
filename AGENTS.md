# AGENTS.md

Read `README.md` first for current status, then `IDEA.md` for the intended architecture and acceptance criteria. `IDEA.md` is the product-direction source; it does not prove that a capability is implemented.

## Public-repository rules

- Keep private articles, images, snapshots, `.state`, credentials, recipients, machine-local paths, and real deployment settings out of this repository.
- Use synthetic fixtures, not copied private archive entries.
- Do not copy implementation files from the reference project while another task is modifying them; use a frozen, accepted source snapshot.
- Do not claim that planned capabilities work until the corresponding implementation and tests exist.
- Keep one umbrella `content-archive` skill; do not split it by vendor, category, or channel.
- Do not modify a private archive, its cron jobs, installed skills, or publication targets as a side effect of working here.
- Present project and configuration changes for confirmation before applying them. Do not commit, push, create releases, or change remote settings without explicit approval.

## Commit, push, and release cadence

- Default to local work. File writes, tests, commits, pushes, tags, and releases are separate checkpoints; completing one never automatically authorizes the next.
- Commit a complete, coherent, verified unit of work rather than individual file edits or tool calls. Keep unrelated features, fixes, documentation, and mechanical reformatting in separate commits.
- Before proposing a commit, inspect `git status` and the full diff, run the relevant checks, and audit the staged scope for private content, credentials, and machine-local configuration. Report the changed files and actual verification results.
- Commit only after explicit approval, then read back the commit hash and summary. A commit may remain local while later work continues.
- Push at user-approved acceptance boundaries, not automatically after every commit. Several related verified commits may be reviewed and pushed together; unfinished or unverified work must not be pushed as if it were ready.
- Treat tags, GitHub Releases, and publication of a toolkit archive as separately approved release operations. A normal push does not create a release.
- Never force-push, rewrite published history, or change branch protections to bypass review or verification.
