# Contributing to CLINX

Use the setup below, then [the test guide](docs/TESTING.md). Read applicable `AGENTS.md` files and inspect
`git status`/`git diff` before editing a shared checkout. Preserve unrelated work.

## Development setup

Use Python 3.12 and Git. From the repository root:

```sh
python3.12 -m venv .venv-test
.venv-test/bin/python -m pip install -r requirements-dev.txt
.venv-test/bin/python scripts/run_tests.py basic
.venv-test/bin/python scripts/run_tests.py collect
```

If your Python distribution omits `venv`/`ensurepip`, install its venv package or
use `uv venv --python 3.12 --seed .venv-test`. On P620 put environments/builds in
your task-owned LAB directory and use that Python path. Keep `.venv-node` under
its deployment contract; it may link to an installed release.

The basic profile uses disposable SQLite and scripted transports and needs no
production CLINX, Linear account, Codex daemon, API key or production database.
`collect` discovers tests without running them. Profiles are listed in
[tests/suites.toml](tests/suites.toml). For isolated Owner integration and special
prerequisites, see [Testing](docs/TESTING.md); runtime setup remains in [README.md](README.md).

## Before submitting a change

1. Install `requirements-dev.txt` in a separate virtual environment.
2. Run `python scripts/run_tests.py basic` and the tests for the changed behavior.
3. Run `python scripts/run_tests.py collect`; compare node IDs if changing collection.
4. Run `python -m ruff check --no-cache --config ruff-discovery.toml <changed-python-files>`
   and `git diff --check`. Use the repository configuration, not Ruff defaults.
5. Report exact commands, interpreter/dependency versions, failures, existing
   skips and any prerequisites you could not exercise. Keep failure evidence.

Do not convert missing dependencies, authority failures or uncertain recovery
into skips or weaker assertions. A portable smoke profile is not the complete
regression suite. A passing fixture is not real Provider/device acceptance.

## Test layout and naming

Legacy root `test_*.py` files stay in place. Many export fixtures and are imported
by other tests; scripts and dated qualification documents also use those paths.
Do not rename milestone tests simply because their names are historical.

New independent tests belong in `tests/unit/`, `tests/integration/`,
`tests/regression/` or `tests/e2e/`, named `test_<behavior>.py`. Create directories
only when adding a test. Keep one canonical definition of each fixture; import
shared fixture modules explicitly. Avoid importing `unittest.TestCase` classes
into another test module under collectable names: use a module alias instead.
Move a legacy fixture only with an import/reference audit and unchanged node-ID
coverage, including external callers. The current index is [tests/README.md](tests/README.md).

Use the markers registered in `pytest.ini`: `unit`, `integration`, `regression`,
`e2e`, `historical`. Markers describe behavior and may overlap. Existing modules
are indexed in place, not retroactively filtered by an automatic marker hook;
`-m integration` is therefore not an exhaustive legacy integration selection.

## State and authority

Use temporary SQLite databases and local fake Providers. Owner tests should start
their own Owner thread/socket with an explicit temporary registry, assert shutdown,
and test missing Owner as a failure boundary. Never point fixtures at production
state, replay an old event to obtain a pass, or start real Provider tests by default.
Retain unique regressions for ownership, transport, idempotency, WAL/transactions,
recovery, uncertain delivery and stale writers.

Generated reports may go in `.test-results/` (ignored). Ignored is not disposable:
delete only output created by your own task after checking references and ownership.
Do not delete unknown caches, release packages, manifests, DB/WAL/SHM files,
archives, logs, backups or rollback evidence. On P620 follow the task-owned artifact
and LAB storage rules in `AGENTS.md`; never create another long-lived CLINX checkout.
