# Ava Testing Guidelines

## Quick Start

```bash
# Run all tests (excluding e2e): bare pytest collects the top-level tests/
# and every package's own <pkg>/**/tests/ (`testpaths` in pyproject.toml)
.venv/bin/pytest --ignore=tests/e2e -q

# Run a single module (a package's tests sit inside it)
.venv/bin/pytest tests/components/agent/ -q
.venv/bin/pytest base/packages -q

# Run tests + coverage report
.venv/bin/pytest --ignore=tests/e2e -q \
  --cov=agent --cov=ava --cov=gateway --cov=base --cov=ui \
  --cov-report=term-missing

# Generate HTML coverage report
.venv/bin/pytest --ignore=tests/e2e -q \
  --cov=agent --cov=ava --cov=gateway --cov=base --cov=ui \
  --cov-report=html
open htmlcov/index.html
```

## Where to Put Tests

A test lives in a `tests/` directory: the top-level `tests/`, or a package's own
`<pkg>/**/tests/` beside the code it proves. What the test needs decides which:

- **Unit test**: the `tests/` directory of the package it tests
  (`base/packages/plugins/tests/test_manifest.py` tests
  `base/packages/plugins/manifest.py`).
- **Integration test across packages**: the lowest package that may legally import
  everything the test uses under the repo's import layering.
- **End-to-end tests, and contract tests that read repository artifacts** (workflows,
  schedules, migrations, `pyproject.toml`, `ui/`): the top-level `tests/`.

A new test in the top-level `tests/` is refused unless it is e2e or UI, or is registered in
`scripts/structure/tests_location_allowed.py` as `contract` or `integration` with a reason
(`scripts/structure/tests_location.py`, run by the pre-commit hook; `--suggest <file>` names the
package a test belongs in). There is no list of debt to add to: a new test is registered or
refused.

Package `tests/` directories have no `__init__.py` (`--import-mode=importlib`), and
the repo-root `conftest.py` plugins apply to them exactly as to the top-level tree.
The `tests/components/{module}/` directories hold only the registered contract and integration tests. Cross-cutting contracts live in `tests/contracts/`, and test infrastructure checks in `tests/harness/`.

```
tests/
├── {module}/              # Unit tests, directory name corresponds to top-level source module
│   └── test_{file}.py     # One test file per source file
├── integration/           # Integration tests (requires Gateway process)
│   └── test_{scenario}.py
├── e2e/                   # End-to-end tests (full stack; keeps its own conftest.py)
├── factories/             # Test data factories (to be created)
│   ├── messages.py
│   └── state.py
├── fixtures/              # Global fixture plugins (DB/Redis isolation, guards), loaded by the repo-root conftest.py
└── path_scoped/           # Per-directory fixtures (the former conftests), registered by the `path_scopes.toml` files next to the tests they govern
```

Fixtures that only some directories' tests take (autouse isolation stand-ins, `short_tmp`,
`as_machine`, ...) are not in a `conftest.py`, because a conftest does not follow a test into a
package's `tests/` directory. They live in `tests/path_scoped/`; a `path_scopes.toml` in a test
directory names the fixture modules that apply there (`tests/fixtures/path_scopes.py` reads them
all); a test moved elsewhere gets its file named in the destination directory's `path_scopes.toml`
(that file's docstring says how).

### Directory Mapping

| Source | Test |
|------|------|
| `base/packages/plugins/manifest.py` | `base/packages/plugins/tests/test_manifest.py` |
| `agent/graph/exec/node.py` | `agent/graph/exec/tests/test_exec_output.py` |
| `gateway/agents/history/timeline.py` | `gateway/agents/history/tests/test_timeline.py` |
| `ava/shell.py` | `ava/shell/tests/test_shell.py` |

If adding a new sub-module (e.g., `ava/new_module.py`), create `test_new_module.py` in
the package's own `tests/` directory when it has one (`ava/tests/`), otherwise under
`tests/components/ava/`.

## Test Naming

```
test_{function_under_test}_{scenario}_{expected_result}
```

Examples:
- `test_generate_summary_returns_summary_and_tail_messages` ✅
- `test_empty_string_content_no_item` ✅
- `test_404_for_unknown_thread` ✅
- `test_1` ❌ (cannot tell what is being tested from the name)

## What Counts as Pass

Each test file should cover:

1. **Happy path** — normal input → normal output
2. **Boundary conditions** — empty input, single element, very large/small values, None
3. **Error paths** — at least one test for each `raise` statement
4. **Branch coverage** — verify that every `if` branch is exercised via coverage report

**Not required to test:**
- `__main__.py` entry points
- empty `__init__.py` files
- `__repr__` / `__str__` pure display methods
- `TYPE_CHECKING` blocks
- `@abstractmethod` declarations

## Fixture Usage

### DB Tests

```python
# Tests requiring DB — declare db_conn dependency
def test_create_agent(db_conn):
    tid = create_agent(db_conn)
    assert tid > 0

# async DB operations — use adb_conn
async def test_claim_pending(adb_conn):
    ...
```

`db_conn` automatically TRUNCATEs all tables — each test starts with an empty DB, no manual cleanup needed.

### Mock External Dependencies

```python
from unittest.mock import AsyncMock, MagicMock

def test_my_function():
    mock_llm = AsyncMock(return_value=AIMessage(content="ok"))
    result = await my_function(llm=mock_llm)
    assert result == "ok"
```

### Testing AgentState

```python
from agent.state import AgentState
from langchain_core.messages import HumanMessage

state = AgentState(messages=[HumanMessage(content="hello")])
```

## Coverage Thresholds

`ci.yml` is the source of truth. The backend has an 85% hard line-coverage
gate across all shards (about 90.5% measured on 2026-08-23). The frontend has
an 81% hard line-coverage gate (86.14% measured on 2026-08-24).

Each release cycle, an owner re-measures while the gate stays green and raises
the threshold by about one point toward the measured value. Never converge to
100%: line coverage is not quality, so retain a buffer. Per-module numbers live
in the CI coverage report and `coverage.xml`, not here.

## CI Pipeline

```
Push/PR → CI
  ├── backend: pytest + pyright + coverage
  │   coverage output to CI log + xml (can be integrated with external services)
  ├── frontend: eslint + tsc + vitest
  └── e2e: Playwright (required checks accept success or skipped; skips only on docs-only diffs)
```

Pre-commit runs lint and codegen checks; pre-push runs pyright and frontend
tsc, eslint and the full Vitest suite. Local development tests are targeted;
see the [hook runbook](../docs/conventions/runbook.md#git-hooks-pre-commit--pre-push).

## `.test_durations` — pytest-split duration data

The backend shards (`--splits 16`) and the e2e shards (`--splits 4`) are
balanced by per-test durations from the repo-root `.test_durations`; a test
without an entry is costed at the average of this run's known durations, so a
stale file skews the shards (measured ~20% skew as of 2026-08-30, after the
file went 11 days without a refresh).

Refresh the file manually after a significant test-suite change:

```bash
uv run python scripts/ci/refresh_test_durations.py
```

Successful main-push CI records clean timings during its existing backend
16-way and e2e four-way runs. After 20 first-parent main changes since the last
published measurement, the refresh workflow reuses those 20 artifacts without
running the suites again. A squash or merge PR counts as one change; direct
main commits count too. Backend retries reseed the committed input weights so
a failed attempt cannot change its shard's selection.

Daily isolated measurements remain the fallback, with a two-hour backstop.
Backend measurements carry `--omit-static-tests`, `-n 4`, `-m "not flaky"`,
and CI's coverage module list; e2e carries `-n 2`. The merge requires every
artifact, rejects empty or overlapping measurements, retains fast and zero
entries, and atomically writes compact JSON. All measurements and publication
use one immutable source SHA.

[Duration refresh policy](../scripts/ci/docs/duration-refresh.md) explains
cadence and provenance. `.github/workflows/refresh-test-durations.yml` updates
one reviewable bot PR and records its source in `.test_durations.source.json`.
Only complete published measurements reset freshness; skipped runs and failed
measurements do not. Main uses the new weights after that PR merges; it is never
auto-merged.

## Host isolation: what a test run may touch

The repo-root `conftest.py` loads the suite's global fixtures as plugins from `tests/fixtures/`
(`env_bootstrap.py` first, then `provisioning.py`, `guards.py`, ...), so they apply to every test in the
repository. Together they redirect every host resource the suite could otherwise
share with the operator's live cluster: `$AVA_HOME` (tmpfs), the database and
Redis (throwaway per-worker instances), every daemon health port, and the
session home (the vendored runtime, initdb template and PTY freeze live in the
home, so `$AVA_HOME` redirects them too). Each of those works because the resource is addressed
by a value the process reads — redirect the value, redirect the resource. Env
vars are set in `os.environ`, not only on the settings singleton, so subprocesses
(the e2e gateway / ops / restarter) inherit them.

**The OS scheduler is the exception, because it cannot be redirected.** launchd
reads one `~/Library/LaunchAgents` per user, and `crontab` edits one table per user.
So the suite does not redirect it — it
refuses to write to it at all, via `AVA_OS_JOBS_ENABLED=false`
(`base.host.system.cron.os_jobs_enabled` gates all five registrars). Labels and
crontab markers name a job, not a home, so a second gate covers both directions:
only the default home (`~/.ava`) may register or remove a job
(`base.host.system.cron.owns_os_jobs`), and a test of a registrar takes the
`default_home` fixture to be that home. `pytest_sessionfinish` then diffs the
host's Ava jobs (plist contents included) against a snapshot taken when the
provisioning plugin is imported and fails the run on anything new or changed. It
removes nothing: a leaked job replaced the host's real one.

Adding a new registrar, or a new subprocess that could reach one, means checking
all three: the switch and the default-home gate are consulted, and the child
inherits the env.

## Fixture scope vs. what the fixture mutates

A fixture's teardown fires at the end of its **scope**, and a process global it
reassigns stays reassigned until then. So a session-scoped fixture that layers env
onto the process and restores it in a `finally` restores nothing on behalf of the
tests that follow it: everything collected after its directory, in the same process,
keeps running with the layered values. Enforced by
`scripts/lint/fixture_scope.py` (hook `lint-fixture-scope`), two rules:

1. **`scope="session"` outside `tests/fixtures/provisioning.py` may not mutate a process global**
   (`os.environ`, a `settings` field, a module global). That plugin is the one
   exemption — the repo-root conftest loads it once per process, so "the session" and "my directory" are the same blast radius,
   which is why `_provisioned_db` may set `AVA_DB_URL` and never put it back. Deeper
   than that, narrow the scope or hoist the value up to `tests/fixtures/provisioning.py`. A session
   fixture that owns only an expensive resource (`playwright_browser`,
   `frontend_proc`) and hands it back through the return value is fine and is not
   flagged.
2. **`scope="package"` requires an `__init__.py` in the directory.**
   `_pytest.fixtures.get_scope_package` looks for a parent `Package` node and
   **returns the session node when it finds none**, with no warning. pytest builds a
   `Package` node only for a directory containing `__init__.py`, so in a package-less
   directory the keyword reads `package` and means `session`. `tests/e2e/` is the only
   test directory here with an `__init__.py`, and that file exists for exactly this
   reason — it is load-bearing, not cruft.

Both rules come from one incident: `tests/e2e/conftest.py:_e2e_process_env` layers ten
env vars and was `scope="session"`, so `tests/harness/test_home_isolation.py` — which sorts
after `tests/e2e/` and exists to notice precisely this — failed on every serial run
while CI stayed green (the backend job passes `--ignore=tests/e2e`, and `-n auto` puts
the two files in different workers). The e2e job now runs `tests/e2e/
tests/harness/test_home_isolation.py` in one serial worker so that guard can fail where merges
are gated.

Getting the scope right fixes *when* the restore fires, not *what* it covers: the
restore list is checked separately against the fixture body by
`test_the_e2e_fixture_restores_every_env_key_it_assigns`, which derives the assigned
keys by AST so the list cannot fall behind the code.

## Patch the seam, not the module it was imported from

`monkeypatch.setattr("pkg.mod.time.sleep", ...)` does not patch `pkg.mod`. Attribute
paths resolve through, and `pkg.mod.time` **is** the stdlib `time` module object — so
that line replaces `time.sleep` for the whole process, for every test that fixture
covers. The same goes for `mod.subprocess`, `mod.os`, `mod.socket`: a module a file
imported is shared, not owned.

Removing sleep process-wide is not "the tests run faster". Every wall-clock-bounded
retry loop in the product has the shape

```python
deadline = time.monotonic() + timeout
while time.monotonic() < deadline:
    ...
    time.sleep(interval)
```

and it keeps its deadline while losing its only throttle — so it spins at full speed
for the whole bound. `base.sessions.posixproc._terminate_tree` — the loop a graceful
`kill_session(graceful=True)` reaches, with a 15 s default bound per session — is that shape, and on 2026-07-30 one `tests/cli` test reached it: it spun at
~500k iterations/s appending to the test's own recorder list, `pytest tests/cli` hit
**26 GB** on a 16 GB box, swap ran out, and agent boots went from 850 ms to 78-93 s.
Nothing failed — the suite just got slow.

When a test needs control over one operation's clock or transport, pass that
capability through the product's actual input boundary. Verify the public result
or capture the submitted operation. A public readiness call such as
`cli.commands.probe.probe_service` belongs to its definition owner; replacing a
shared imported module affects every consumer in the process.

The approved [component public contract](../docs/decisions/engineering/design/simplification/2026-10-10-component-public-contracts.md)
makes `_symbol` and `_module` file-local, including tests. Cross-component access
uses an explicitly declared entry module and its definition owner's static
`__all__`. A test's directory or inferred placement does not grant private access.
Removing an underscore, adding a barrel or forwarding a private object through a
test helper does not create a legitimate capability.

The existing patch-target gate still implements its legacy package calculation
during migration. The new public-contract audit fails on known violations and
unresolved recognized inputs; it is not yet an enabled repository gate. A clean
legacy check therefore does not establish the complete component contract.

The related trap in the same incident: patching a name on the **package** when the
caller imported it directly. A caller that does `from pkg.owner import run`
holds its own binding, so `monkeypatch.setattr(pkg, "run", ...)` never
reaches it — the stub is a silent no-op and the real function runs. Patch the module
that resolves the name, or have the caller look it up dynamically. `monkeypatch` will
not tell you the stub was unused.

`pytest_sessionfinish` fails any run whose peak memory crosses
`_PEAK_MEMORY_CEILING_MB` (6 GiB, against a healthy `tests/cli` of ~0.16 GB) — a
runaway detector, not a budget. The gauge is `footprint(1)`'s `phys_footprint_peak` on
macOS and `ru_maxrss` on Linux: macOS compresses cold anonymous pages and RSS does not
count them, so `ps`/`ru_maxrss` under-report a runaway there by roughly 10x. Profile
with `footprint -p <pid>`, never `ps -o rss`.

## Anti-patterns

| Don't | Do |
|------|-----|
| `time.sleep(n)` | `asyncio.sleep(0)` or mock |
| `monkeypatch.setattr("pkg.mod.time.sleep", ...)` — edits the stdlib module process-wide | Patch a module-local seam (`root_driver._poll_sleep`); see above |
| Stub a name on the package when the caller did `from ... import name` | Patch the module that resolves it — a missed stub is silent |
| Share state between tests | Each test independent, using fixture for initial state |
| Wrap test body with `try-except` | Let pytest fail naturally |
| Test against production environment | conftest already isolates test DB |
| Session-scoped fixture that sets env and restores it | Scope it to the directory it serves (see above) |
| Write `test_1`, `test_2` | Use meaningful names |
| Only test happy path | Cover boundary + error paths |
