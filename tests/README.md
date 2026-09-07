# Ava Testing Guidelines

## Quick Start

```bash
# Run all tests (excluding e2e)
.venv/bin/pytest tests/ --ignore=tests/e2e -q

# Run a single module
.venv/bin/pytest tests/agent/ -q

# Run tests + coverage report
.venv/bin/pytest tests/ --ignore=tests/e2e -q \
  --cov=agent --cov=ava --cov=gateway --cov=shared --cov=ui \
  --cov-report=term-missing

# Generate HTML coverage report
.venv/bin/pytest tests/ --ignore=tests/e2e -q \
  --cov=agent --cov=ava --cov=gateway --cov=shared --cov=ui \
  --cov-report=html
open htmlcov/index.html
```

## Where to Put Tests

```
tests/
├── {module}/              # Unit tests, directory name corresponds to top-level source module
│   ├── conftest.py        # Shared fixtures for this module
│   └── test_{file}.py     # One test file per source file
├── integration/           # Integration tests (requires Gateway process)
│   ├── conftest.py
│   └── test_{scenario}.py
├── e2e/                   # End-to-end tests (full stack)
├── factories/             # Test data factories (to be created)
│   ├── messages.py
│   └── state.py
└── conftest.py            # Global fixtures (DB/Redis isolation)
```

### Directory Mapping

| Source | Test |
|------|------|
| `agent/graph/_exec.py` | `tests/agent/test_exec_output.py` |
| `gateway/timeline.py` | `tests/gateway/test_timeline.py` |
| `ava/shell.py` | `tests/ava/test_shell.py` |

If adding a new sub-module (e.g., `ava/new_module.py`), create `test_new_module.py` under `tests/ava/`.

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
  ├── frontend: tsc + vitest
  └── e2e: Playwright (required checks accept success or skipped; skips only on docs-only diffs)
```

Pre-commit does not run pytest (needs DB), but runs ruff + pyright + frontend tsc + frontend vitest.

## `.test_durations` — pytest-split duration data

The backend shards (`--splits 16`) and the e2e shards (`--splits 4`) are
balanced by per-test durations from the repo-root `.test_durations`; a test
without an entry is costed at the average of this run's known durations, so a
stale file skews the shards (measured ~20% skew as of 2026-08-30, after the
file went 11 days without a refresh).

Refresh the file manually after a significant test-suite change:

```bash
uv run python scripts/refresh_test_durations.py
```

The nightly workflow runs the CI-shaped backend 12-way and e2e four-way shard
matrices: backend carries `-n 4`, `-m "not flaky"`, and CI's `--cov` module
list (coverage tracing is part of the shard environment); e2e carries `-n 2`.
Each shard records a clean duration artifact with `--store-durations
--clean-durations` and retries independently. The merge accepts only all 16
successful artifacts, keeps entries `>= 0.2s`, and atomically rewrites
`.test_durations` in the committed compact-JSON format (sorted keys,
3-decimal values, one trailing newline). A failed or missing shard therefore
leaves the committed file untouched instead of publishing partial timings.

`.github/workflows/refresh-test-durations.yml` runs this nightly on `main` and
opens one reviewable PR (never auto-merged) when the file changed — follow its
bot-PR pattern for manual refreshes too.

## Host isolation: what a test run may touch

`tests/conftest.py` redirects every host resource the suite could otherwise
share with the operator's live cluster: `$AVA_HOME` (tmpfs), the database and
Redis (throwaway per-worker instances), the cluster registry, every daemon health
port, and the session home. Each of those works because the resource is addressed
by a value the process reads — redirect the value, redirect the resource. Env
vars are set in `os.environ`, not only on the settings singleton, so subprocesses
(the e2e gateway / ops / restarter) inherit them.

**The OS scheduler is the exception, because it cannot be redirected.** launchd
reads one `~/Library/LaunchAgents` per user, `crontab` edits one table per user,
schtasks owns one `\Ava\` folder per user. So the suite does not redirect it — it
refuses to write to it at all, via `AVA_OS_JOBS_ENABLED=false`
(`shared.os_cron.os_jobs_enabled` gates all four registrars; the unregister paths
stay live). `pytest_sessionfinish` then diffs the host's Ava jobs against a
snapshot taken at conftest import and fails the run on anything new, removing the
jobs that name this suite's own homes and reporting anything else.

Adding a new registrar, or a new subprocess that could reach one, means checking
both: the gate is consulted, and the child inherits the env.

## Fixture scope vs. what the fixture mutates

A fixture's teardown fires at the end of its **scope**, and a process global it
reassigns stays reassigned until then. So a session-scoped fixture that layers env
onto the process and restores it in a `finally` restores nothing on behalf of the
tests that follow it: everything collected after its directory, in the same process,
keeps running with the layered values. Enforced by
`scripts/lint_fixture_scope.py` (hook `lint-fixture-scope`), two rules:

1. **`scope="session"` outside `tests/conftest.py` may not mutate a process global**
   (`os.environ`, a `settings` field, a module global). The root conftest is the one
   exemption — there, "the session" and "my directory" are the same blast radius,
   which is why `_provisioned_db` may set `AVA_DB_URL` and never put it back. Deeper
   than that, narrow the scope or hoist the value up to the root conftest. A session
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
env vars and was `scope="session"`, so `tests/test_home_isolation.py` — which sorts
after `tests/e2e/` and exists to notice precisely this — failed on every serial run
while CI stayed green (the backend job passes `--ignore=tests/e2e`, and `-n auto` puts
the two files in different workers). The e2e job now runs `tests/e2e/
tests/test_home_isolation.py` in one serial worker so that guard can fail where merges
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
for the whole bound. `shared.session_backend._graceful_kill_session` is that shape with a
15 s bound per session, and on 2026-07-30 one `tests/cli` test reached it: it spun at
~500k iterations/s appending to the test's own recorder list, `pytest tests/cli` hit
**26 GB** on a 16 GB box, swap ran out, and agent boots went from 850 ms to 78-93 s.
Nothing failed — the suite just got slow.

So when a test needs to shorten or forbid one specific sleep, the product gives that
sleep a name and the test patches the name: `cli/commands/_probe.py` binds
`_poll_sleep = time.sleep` at import, and `monkeypatch.setattr(_probe, "_poll_sleep",
...)` reaches that poll and nothing else. Same reasoning as the
`import cli.commands as _ns` indirection used for `_probe_service` / `_has_session`
— one named seam per patchable behaviour, so a stub's blast radius is stated in the
product rather than inferred from an attribute path.

The related trap in the same incident: patching a name on the **package** when the
caller imported it directly. `cli/commands/update.py` does `from cli.commands.stop
import _do_stop`, so `monkeypatch.setattr(cli.commands, "_do_stop", ...)` never
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
| `monkeypatch.setattr("pkg.mod.time.sleep", ...)` — edits the stdlib module process-wide | Patch a module-local seam (`_probe._poll_sleep`); see above |
| Stub a name on the package when the caller did `from ... import name` | Patch the module that resolves it — a missed stub is silent |
| Share state between tests | Each test independent, using fixture for initial state |
| Wrap test body with `try-except` | Let pytest fail naturally |
| Test against production environment | conftest already isolates test DB |
| Session-scoped fixture that sets env and restores it | Scope it to the directory it serves (see above) |
| Write `test_1`, `test_2` | Use meaningful names |
| Only test happy path | Cover boundary + error paths |
