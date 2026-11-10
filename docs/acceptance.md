# Initial Migration Acceptance

## Scope

This record covers the initial migration from the accepted private reference implementation into the public Content Archive Kit source tree. It does not approve a versioned release, production upgrade, live notification, or publication.

## Verified results

- **Reference-source integrity:** all 53 source/test files covered by the reference acceptance record matched their accepted hashes at migration time. Generated Python bytecode was explicitly excluded from the migration baseline.
- **Reference-environment regression:** 567 tests passed, 2 environment-dependent tests skipped.
- **Clean-environment regression:** a new Python 3.13 virtual environment installed the declared dependencies and completed the same 567 tests with 2 skips. `PYTHONPATH` was removed for installation and execution.
- **Dependency correction:** the initial clean install identified an incompatible `charset-normalizer` pin. Raising it from 3.4.4 to 3.5.2 satisfied the declared Trafilatura requirement and the clean install then passed.
- **Fresh-archive CLI:** a synthetic archive completed `add → finalize → index → search → audit`. The entry contained a validated 201-character Chinese summary; a repeated URL returned `duplicate` and left one entry and one index row.
- **Existing-archive CLI:** the toolkit ran against an isolated copy of the private 173-entry archive. Reindexing and audit passed; all entry bytes and the deduplication state remained unchanged. No publication configuration was copied and no publication ran.
- **Syntax and configuration:** the Bash launcher, JavaScript reader, and all Python sources passed syntax checks. Example configuration loaded successfully, with optional publication disabled.
- **Public boundary:** no private article directories, runtime state, credentials, real recipients, machine-local paths, symlinks, or files over 2 MiB were found in the public tree. Tests use synthetic fixtures.

## Independent review

An independent code review using a different model from the implementation agents reproduced the 567-test result and found no evidence of weakened coverage. Its initial **CONDITIONAL PASS** identified two governance blockers:

1. The migration-source hashes could be misread as the accepted public-tree hashes. `docs/migration-baseline.json` is now explicitly marked as pre-generalization provenance; `docs/public-tree-sha256.json` separately records the accepted public tree.
2. The repository lacked an overall license. The maintainer selected **MIT**, and `LICENSE` now applies to the code, tests, documentation, and skill.

The public skill author is `taukemora, Hermes Agent`, as confirmed by the maintainer. The skill's 2.0.0 version reflects the intentional generic rewrite and new instance contract.

## Follow-up challenge review

A separate challenge review covering the full day's development confirmed the public migration, initial commit, deploy-key boundary, and bilingual documentation work. It reproduced two documentation issues: the test command could select an unprovisioned system Python, and the instance-copy instructions did not explicitly require a working instance interpreter. Both READMEs now use `../.venv/bin/python` for tests and require an instance virtualenv or `CA_PYTHON`. The capability text now distinguishes the generic RSS adapter from site-specific HTML/docs adapters and describes local media more precisely. A delivery-test hostname was replaced with `example.com`, and common private-key filenames were added to `.gitignore`.

The changed delivery module was rerun after these edits: **33 tests passed, 1 environment-dependent test skipped**. The complete current tree was then rerun in the clean Python 3.13 environment: **567 tests passed, 2 environment-dependent tests skipped**. The public-tree hash manifest was regenerated after all documentation and test edits.

## Remaining boundaries

- Two optional tests remain skipped by default: a real-instance dry run and a browser acceptance test requiring Chromium and Playwright.
- Browser performance, future performance optimization, production installation, live delivery, NAS state, and hosted publication are outside this acceptance.
- The source project and existing public Pages deployment were not modified by this migration.
- Supported platforms are currently Linux and macOS (POSIX); native Windows requires WSL rather than an unverified platform claim.
- An upstream commit, push, tag, release, or installation into a private archive requires separate approval under `AGENTS.md`.
