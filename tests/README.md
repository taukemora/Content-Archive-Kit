# Tests

Unit, integration, and acceptance tests for Content Archive Kit. Everything here
is synthetic: no article, image, state file, credential, recipient, or
machine-local path from a real archive is used.

## Running

```bash
cd tests
../.venv/bin/python -m unittest discover -s . -t .
```

`discover` must run from this directory (the checkout path may contain spaces,
which breaks package discovery from above). Any single module works the same
way:

```bash
cd tests
../.venv/bin/python -m unittest test_phase2 -v
```

The suite needs the toolkit's own runtime dependencies (`beautifulsoup4`,
`PyYAML`, and `cryptography` for the publication tests). Run it with the
interpreter that has them installed; see the root `requirements*.txt` files.

### Reading the result

`unittest` prints `Ran N tests` where **N includes skipped tests**. A run that
ends `OK (skipped=2)` means `N - 2` tests actually executed and passed; a
skipped test is not a passing test. Only quote a pass count when the summary is
`OK`; quote failures, errors, and skips from the same summary line. CI fails on
any non-zero exit, so a green workflow already means no failures or errors, but
the skip count still has to be read separately.

## Repository checks

`tools/check_repository.py` is the public-tree gate used locally and in CI:

```bash
cd /path/to/Content-Archive-Kit
env -u PYTHONPATH .venv/bin/python tools/check_repository.py
```

Run it from the repository root with the project's virtualenv; `env -u
PYTHONPATH` keeps a caller's import path out of the check. The default command
is read-only: it does not rewrite the manifest and, because it sets
`sys.dont_write_bytecode` before importing the example loaders, it leaves no
`__pycache__` or `.pyc` files in the checked tree. It checks:

- exact file membership in **both** directions against
  `docs/public-tree-sha256.json` (missing entries and unlisted files both fail);
- byte counts and SHA-256 digests for every listed file;
- canonical relative POSIX paths: no absolute paths, drive letters, backslashes,
  `.`/`..` components, traversal, or non-canonical spellings;
- no symbolic links (the file itself or any ancestor) and no non-regular files;
- private-path screens (`.state`, `library`, `trash`, `.handoff`, live root
  configuration such as `publish.yaml`, and key/environment filenames) plus a
  private-key content screen. Private-key findings name the path without
  printing matched bytes; syntax and loader diagnostics can include values;
- syntax: every shipped `.py` file is compiled in memory, `bin/ca` is checked
  with `bash -n`, and shipped `.js`/`.mjs` files with `node --check`;
- the launcher: `bin/ca` must be tracked as mode `100755`, and an extracted
  artifact must have the executable bit set;
- `examples/` configuration loaded through the real loaders: every watched
  source must be disabled and `publish.yaml` must stay disabled.

The checks run in a fixed order: path safety, membership, byte counts, SHA-256
and private-material screens first; Python/Bash/JavaScript syntax second; the
`examples/` loaders last. The loader step imports and executes
`scripts/common.py`, `scripts/media_policy.py`, and `scripts/publish_site.py`,
so it runs only when the earlier steps are clean and every loader and example
path is a regular file reached without a symlink. An unsafe or syntactically
invalid tree never executes them.

A checkout with `.git` uses `git ls-files` and rejects untracked non-ignored
files, so a new file must be staged before the manifest can include it. A
read-only extracted artifact without `.git` compares the manifest against the
filesystem and ignores only local environment/cache entries: `.venv/`, `venv/`,
`__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `*.pyc`,
`*.pyo`, and `.DS_Store`.

Use `--root PATH` to check a synthetic tree instead of this checkout:

```bash
cd /path/to/Content-Archive-Kit
env -u PYTHONPATH .venv/bin/python tools/check_repository.py --root /path/to/synthetic-root
```

`--update-manifest` is an explicit maintenance command, never a CI step. It
enumerates the reviewed file set, checks path safety and private-material
screens, then validates syntax and example configuration **before** writing
anything. Unlike read-only verification, it computes new byte counts and hashes
instead of requiring them to match the old manifest; that is how an approved
source change becomes the next inventory. It refuses an unsafe, untracked, syntactically invalid, or
enabled-example tree, so invalid code or configuration leaves the previous
manifest bytes intact (or leaves an absent manifest absent). On success it
preserves the existing JSON metadata and file mode, records a new
`captured_at`, excludes `docs/public-tree-sha256.json` itself, and replaces the
file atomically. Stage approved files first:

```bash
git add <approved-files>
env -u PYTHONPATH .venv/bin/python tools/check_repository.py --update-manifest
```

The checks are for reviewed public code, not an untrusted-code sandbox: the
example loaders execute Python, and arbitrary replacement code could write
files despite the normal checker's read-only behavior.
The checks are risk screens, not proof that a tree is free of private material.
They do not inspect git history, article or media semantics, deployment state,
or remote services, and they cannot prove that a path is not private by
content. The workflow file's YAML is also not proven semantically valid by
running the test suite. `actionlint` 1.7.12 is a separate maintainer validation
step run outside the GitHub workflow; the workflow itself does not invoke it.

## Environment switches

| Variable | Purpose |
| --- | --- |
| `CA_TEST_SCRATCH` | Parent directory for throwaway project trees. Defaults to `content-archive-kit-tests` under the platform temp dir (`TMPDIR`). |
| `CA_TEST_PYTHON` | Interpreter used for subprocess-level tests. Defaults to `<checkout>/.venv/bin/python` when present, else the running interpreter. |
| `CA_RUN_INSTANCE_TESTS=1` | Enables the read-only dry run that needs a real, configured archive (skipped in a public checkout). |
| `CA_RUN_BROWSER_TESTS=1` | Enables the real-Chromium reader test. |
| `CA_PLAYWRIGHT_PYTHON` | Interpreter that has Playwright installed (only used with `CA_RUN_BROWSER_TESTS=1`). |

Two tests are skipped by default: the instance dry run (no archive in a public
checkout) and the browser acceptance test (needs Chromium + Playwright). The
Node interop test runs whenever `node` is on `PATH`. The repository checker
needs `node` whenever the tree contains JavaScript, and the JavaScript syntax
tests skip themselves when `node` is absent.

## Continuous integration

`.github/workflows/test.yml` runs on pushes to `main`, pull requests, and manual
dispatch. It uses Ubuntu and macOS with Python 3.13, installs Node.js 22 for the
Node interop and checker syntax steps, installs both `requirements.txt` and
`requirements-publish.txt`, runs `pip check`, then `tools/check_repository.py`,
then the complete suite from `tests/`.

The workflow reads repository contents only (`permissions: contents: read`),
does not persist checkout credentials, has no repository secrets, and has no
publish, deploy, tag, release, or artifact-upload step. `CA_RUN_INSTANCE_TESTS`
and `CA_RUN_BROWSER_TESTS` are explicitly `0`; `CA_SKIP_AUTO_PUBLISH` is
deliberately not set, so the tests that exercise the enabled publication path
still run. Throwaway trees go under the runner's temporary directory via
`CA_TEST_SCRATCH`; `CA_SCRATCH_ROOT` points to that same dedicated directory so
runtime input/output checks recognize the fixture location even when the
runner directory is outside the platform's default temp directory. This uses
the existing explicit scratch option, not a weaker path check. The workflow
environment regression exercises both acceptance of fixture input/output and
rejection of project configuration and unrelated paths. Python commands clear
`PYTHONPATH` with `env -u`.

## Layout the tests expect

- `scripts/` holds the implementation. Scripts derive their project root from
  their own location, so a test that runs a script as a subprocess copies
  `scripts/` into its throwaway project and runs **that** copy. Running the
  checkout's script against sandbox paths would make the checkout the archive
  root — that is why several fixtures build a complete project tree instead of
  pointing a script at a stray directory.
- Sample configuration (`taxonomy.yaml`, `sources.yaml`, `media.yaml`) is read
  from `examples/` first and from the archive root second
  (`_support.template()`), so the suite runs both in this repository and in an
  instance that keeps its own copies.
- `requirements-publish.txt` must exist at the repository root
  (`test_publish_dependencies.py` compares it against what the publish modules
  actually import).
- `bin/ca` is exercised only as a copied launcher inside a sandbox.

## Shared helpers

`_support.py` provides the only path/interpreter plumbing:

- `ROOT` — repository root, derived from the file location;
- `SCRATCH` / `scratch_dir(prefix)` — throwaway trees outside the checkout;
- `archive_project(prefix, scripts=..., configs=..., state=...)` — a throwaway
  archive root with its own `scripts/`, sample config, and optional `.state/`;
- `template(name)` — sample configuration lookup (`examples/` then root);
- `PYTHON` — interpreter for subprocess tests;
- `state_digest(path)` — sha256 of a state file, `None` when absent, so the
  "tests did not touch instance state" guards also hold in a checkout with no
  state at all.

## Fixtures

- `fixtures/latest-fetch-synthetic.json` — a synthetic `fetch_sources.py` run
  (three invented vendors, six candidates) used by the digest numbering test.
  Its candidates are paired with index rows generated in the test; edit both
  together.

## Privacy rules for new tests

- Never read `library/`, `.state/`, `index.csv`, `index.md`, or an instance
  configuration from the checkout; build them inside a scratch project.
- Never assert on a personal recipient, repository, or host: assert on values
  the test configured itself, or on module constants (e.g. the publish allowlist)
  via `getattr` fallbacks.
- Never copy an article, image, or raw snapshot into this repository. Fixtures
  are invented text such as `example.com`, `Vendor Alpha`, and `archive@example.com`.
- New repository-check tests build a synthetic tree under `CA_TEST_SCRATCH` and
  invoke `tools/check_repository.py --root <tree>`; they must not rewrite the
  real manifest or touch the checkout's files.
