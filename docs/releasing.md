# Release procedure

This is a maintainer checklist, not an automatic release pipeline. It
generalizes to any approved candidate version; the current candidate is
**v0.2.0** (see [its draft notes](releases/v0.2.0.md)), which is not published.
Preparing these files does not create a tag, GitHub Release, or published
asset. Scope and remaining gates live in [ROADMAP.md](../ROADMAP.md).

## Separate authorization gates

Local edits and tests, commits, pushes, tags, GitHub Releases, asset uploads, and private-instance installation are distinct actions. Obtain explicit approval for each applicable action. Approval for a commit and push does not authorize a release or deployment. Never force-push, rewrite published history, or weaken checks to publish.

The `tests` workflow is validation only. It has no release, notification, installation, or deployment step and requires no repository secrets. An upstream push must not modify an archive, installed skill, cron job, NAS service, or publication target.

## 1. Select and verify the candidate

1. Read `AGENTS.md`, both READMEs, `IDEA.md`, `ROADMAP.md`, `SECURITY.md`, and the relevant acceptance record. Confirm that the changelog and release notes describe implemented, verified behavior rather than roadmap items.
2. Review the complete intended diff, including new files. Audit private-content and configuration boundaries. The public-tree manifest is an integrity inventory, not proof that arbitrary content is safe or that its publisher is authentic.
3. Follow [the test instructions](../tests/README.md) in a clean Python 3.13 environment with `PYTHONPATH` removed. Run dependency, syntax, example-configuration, public-tree, and complete unittest checks. Record actual failures and skips; optional browser and real-instance tests are not silently promoted to verified coverage.
4. After the approved file set is staged, explicitly regenerate `docs/public-tree-sha256.json` with `tools/check_repository.py --update-manifest`, inspect its changes, stage it, and run the normal read-only checker again. CI must never regenerate the manifest to make itself green.
5. Commit and push only when authorized. Read back the remote SHA. Require the successful Ubuntu job for the exact candidate commit, not an earlier green run or a PR's different merge commit. Linux is the supported platform; macOS is not a release gate. A cancelled, skipped, pending, or absent required Ubuntu job is not a pass.
6. Record the full candidate SHA and its Actions run URL in the release evidence. If files change, repeat the affected local checks, regenerate the manifest, commit, push, and obtain new exact-commit CI evidence.

A CI badge may be added only after a genuine workflow pass. It is navigation/status, not evidence that every future commit is accepted. Adding a badge or editing an acceptance record creates a new candidate that must itself pass CI before release.

## 2. Export the approved commit locally

Do not package the working directory with a recursive copy: ignored or untracked private files could enter the archive. Export only the reviewed Git tree. Run this **only at the separately approved artifact-preparation checkpoint**, with a clean checkout at the chosen commit:

```bash
CANDIDATE="$(git rev-parse --verify 'HEAD^{commit}')"
VERSION=v0.2.0
# Confirm CANDIDATE against the approved SHA and exact-commit CI first.
mkdir -p dist
git archive --format=tar.gz \
  --prefix="Content-Archive-Kit-${VERSION}/" \
  --output="dist/Content-Archive-Kit-${VERSION}.tar.gz" \
  "$CANDIDATE"
printf '%s\n' "$CANDIDATE" > "dist/Content-Archive-Kit-${VERSION}.commit.txt"
python3.13 -c "import hashlib, pathlib; p=pathlib.Path('dist/Content-Archive-Kit-${VERSION}.tar.gz'); print(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name)" \
  > "dist/Content-Archive-Kit-${VERSION}.sha256"
gzip -dc "dist/Content-Archive-Kit-${VERSION}.tar.gz" | git get-tar-commit-id
```

The embedded archive commit must equal `CANDIDATE`. Keep the commit and checksum sidecars beside the archive, not inside the source manifest. Do not upload `dist/` automatically. The generated checksum protects integrity when compared with a trusted value; it is not a digital signature.

## 3. Verify the actual exported artifact

Use a fresh directory under a suitable temporary/scratch parent, not a private archive:

1. Inspect all archive members before extraction: only the expected top-level prefix and reviewed regular files/directories; no symlinks, traversal, private article/state/configuration paths, credentials, or unexpected payloads.
2. Extract the exact archive whose checksum was recorded. Compare its contents with the candidate Git tree and public-tree manifest. The manifest excludes itself by design; compare the manifest's own bytes with the candidate commit too. Do not substitute a test of the original checkout.
3. Create a new `.venv` inside the extracted toolkit, install `requirements.txt` and `requirements-publish.txt` with `PYTHONPATH` removed, and run `pip check` and the repository checker. Its exported-tree mode must not borrow an enclosing directory's Git index.
4. Run the complete unittest suite from the extracted `tests/` directory with that environment. Keep the optional real-instance and browser tests disabled unless separately provisioned; ensure Node is present for the interop test.
5. In a second synthetic archive root, copy the shipped launcher, scripts, requirements, and example configurations; provide a working instance interpreter. Exercise `add → finalize → index → search → audit`, then repeat the URL and verify deduplication. Use synthetic input and no live notifications or publication settings.
6. Record interpreter/tool versions, exact commit, archive SHA-256, actual tests and skip reasons, and the smoke-test outcome. If the artifact fails, stop: fix source and return to a new candidate, never hand-edit the tarball.

The source tree includes tests, examples, documentation, and the canonical umbrella skill. Installation into an archive selects the documented runtime files; it must not replace instance configuration or treat the public README as the instance's local rules.

## 4. Tag, publish, and read back — only with separate approvals

- Review the approved candidate's release notes draft (for the current
  candidate, [releases/v0.2.0.md](releases/v0.2.0.md)) and `CHANGELOG.md`;
  finalize only claims supported by the candidate's evidence.
- With tag authorization, create the agreed tag at the exact accepted commit; pushing the tag is a remote operation that must also be authorized. Read back and compare the tag's peeled commit. Do not silently move an existing tag.
- With GitHub Release authorization, create the Release for the approved existing tag. If the approved candidate is an early pre-release, explicitly select **Pre-release** and do not mark it as the latest stable release; read back these fields rather than relying on defaults. Draft Release creation is still an external write and needs approval.
- With asset-upload authorization, attach only the verified toolkit archive and its approved provenance/checksum sidecars. Read back the Release target and asset inventory; download and hash the uploaded archive before claiming delivery.
- Do not describe the release as deployed to a private instance. Pinned installation, backup, copy rehearsal, preservation of instance configuration/state, and rollback are a later, separately approved task; see [deployment and upgrades](../skills/content-archive/references/deployment.md).

## Evidence without a self-referential commit

A file cannot include the hash of the commit that will contain that same file. Keep in-tree evidence explicit about its pre-commit/local scope. Bind final CI and artifact evidence to the exact immutable SHA through the Actions run, checksum/provenance sidecars, and approved Release metadata. Any follow-up in-tree evidence commit becomes a new candidate; rerun CI rather than attributing an earlier run to it.
