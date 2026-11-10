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
Node interop test runs whenever `node` is on `PATH`.

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
