# Contributing to CLINX

Use the setup below, then [the test guide](docs/TESTING.md). Read applicable `AGENTS.md` files and inspect
`git status`/`git diff` before editing a shared checkout. Preserve unrelated work.

The Python pytest suite is local-only under the ignored `tests/` directory.
It is not distributed through Git. The commands below require that retained
local directory; installing dependencies after a fresh clone does not recreate it.

## Development setup

Use Python 3.12 and Git. From the repository root:

```sh
python3.12 -m venv .venv-test
.venv-test/bin/python -m pip install -r requirements-dev.txt
.venv-test/bin/python -B -m pytest -c tests/pytest.ini tests/test_rust_native_routing.py tests/test_rust_request_contract.py
.venv-test/bin/python tests/run_tests.py collect
```

If your Python distribution omits `venv`/`ensurepip`, install its venv package or
use `uv venv --python 3.12 --seed .venv-test`. On P620 put environments/builds in
your task-owned LAB directory and use that Python path. Keep `.venv-node` under
its deployment contract; it may link to an installed release.

The Rust adapter fixtures use disposable state and scripted transports and need no
production CLINX, Linear account, Codex daemon, API key or production database.
`collect` discovers tests without running them. Profiles are listed in
the local `tests/suites.toml`. For isolated Owner integration and special
prerequisites, see [Testing](docs/TESTING.md); runtime setup remains in [README.md](README.md).
The retained historical profiles include retired Python experiments and currently
fail collection. Preserve these local files; report their retirement separately
from current runtime regressions as described in the phase 2 cleanup record.

## Before submitting a change

1. Install `requirements-dev.txt` in a separate virtual environment.
2. Run the Rust adapter fixtures above and the tests for the changed behavior.
3. Run `python tests/run_tests.py collect`; compare node IDs if changing collection.
4. Run `python -m ruff check --no-cache --config ruff-discovery.toml <changed-python-files>`
   and `git diff --check`. Use the repository configuration, not Ruff defaults.
5. Report exact commands, interpreter/dependency versions, failures, existing
   skips and any prerequisites you could not exercise. Keep failure evidence.

Do not convert missing dependencies, authority failures or uncertain recovery
into skips or weaker assertions. A portable smoke profile is not the complete
regression suite. A passing fixture is not real Provider/device acceptance.

## Test layout and naming

All Python pytest modules, shared fixtures, profiles, configuration and the
runner belong directly under the local `tests/` directory. Name modules
`test_<behavior>.py`; preserve existing module basenames because tests import
fixtures from one another. Repository-relative paths resolve from the parent of
`tests/`, and `tests/conftest.py` supplies imports and marker registration.
Do not add root-level test modules or force-add this ignored directory to Git.

Run `python tests/run_tests.py collect` after changes and compare case identities.
Markers describe behavior and may overlap; no automatic hook filters old tests.
Keep unique ownership, transport, SQLite, idempotency and recovery assertions.

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
