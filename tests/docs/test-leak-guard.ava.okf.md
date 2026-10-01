---
type: doc
title: "Test Leak Guard — Root Plugin"
description: "The root plugin that names the test which leaves process-global state different (environment, module attributes, cwd, signal handlers): what it compares, its modes, where its findings go, the order it depends on and what it cannot see."
tags:
- evaluation
- quality-assurance
---

# Test Leak Guard — Root Plugin

## What it is

A test that leaves an environment key, a stored module attribute, the cwd or a signal handler different from how it found it changes what every later test in the same xdist worker sees. The red test is then an innocent victim, and only in the runs where leaker and victim share a worker, so moving test files (new path order, new shard composition) is enough to expose a leak the old order hid. `tests/fixtures/leak_guard.py` is one function-scoped autouse fixture that snapshots the cheap-to-compare containers before each test and, after the test's own fixtures are torn down, names the test that changed them. It is a plugin of the repo-root `conftest.py`: [[test-fixtures.ava.okf.md]].

## What it compares

| Kind | Compared | Failure it stands for |
|---|---|---|
| `env` | `os.environ` keys added, removed or changed (values are never printed) | `monkeypatch.delenv(key, raising=False)` on an absent key records nothing, so the key the code under test sets next stays |
| `module-attr` | a first-party module's `__dict__` gained a non-dunder, non-module name | `monkeypatch.setattr` on a name the module serves from `__getattr__` stores the dynamic value for good on undo; code under test marking a module object that outlives the test |
| `cwd` | `os.getcwd()` | a bare `os.chdir` |
| `signal` | `signal.getsignal` of the common signals | a handler that raises, installed and not put back |
| `sys.path` | a note, never a leak: entries added or removed | tests insert on purpose |

First-party means a module's `__file__` is below the rootdir, outside top-level dot-directories (`.venv`, `.git`) and any `tests`, `site-packages` or `node_modules` directory. Dicts are scanned by total length (`sum(map(len, ...))`); only when it moved are the dicts that moved located.

## Cost

Two snapshots a test per xdist worker, under CI's coverage tracer, where the price is the Python frames a test enters (each a trace event), not C work. So the guard works in bulk passes (`map`, `compress`, one `==` per container; only a difference runs the Python that names it), about eight frames a snapshot; `tests/ci/test_leak_guard_cost.py` counts them (`sys.setprofile`; timing flakes). Per test what remains is the module scan twice (about 12 us in cache, several times that after a test; the floor) and `os.getcwd()` twice (0.25 us on Linux, 10-25 us on macOS; only it sees a deleted cwd). Per worker, locating the first-party modules among ~8,000 in `sys.modules` is a one-time cost, as reading a module is what costs: new names come from the key list's tail, and the submodules of a top-level module whose own file is not first-party (an installed package) are skipped unread; a test checks that none of ours hides there. The shard line splits `us/test` into scans, locating (one-time) and the rest; steady cost is `us/test - ms*1000/tests`.

## Modes

`AVA_LEAK_GUARD` is read at configure; any value but these stops the run.

- `warn` (default): observe only. No outcome changes, no state is put back, and a fault of the guard itself (an exception in the snapshot, the comparison, the property write, the controller hooks or the summary) is one report line plus one `leak_guard_fault` property, never a test result.
- `fail`: put back what the strict checks name (so victims stay green), then fail the leaker at its own teardown with the nodeid and a fix hint per kind. A guard that cannot work is loud here: its fault propagates.
- `off`: the fixture is a no-op.

## Why it is second in `pytest_plugins`

pytest tears fixtures down in reverse setup order. As the first function-scoped autouse fixture to set up, the guard is the last to tear down, so it compares after `monkeypatch` has undone everything it recorded and before any module-scoped or session-scoped fixture finalizes (what those set up is already in the "before" snapshot, so a module's first and last test are not special). Registered after another autouse plugin that uses `monkeypatch` it would compare too early and blame a clean test. `env_bootstrap` stays first: the guard imports only the standard library and pytest. `tests/ci/test_leak_guard.py` locks the position statically (the plugin list), dynamically (the closure of a real test) and by experiment (the same suite with the guard first and last).

## Where the findings go

- Each finding is a JUnit property on the leaker's testcase: `leak_guard`, `leak_guard_note` (`sys.path`), `leak_guard_fault`; value `<kind>: <detail>`.
- The terminal prints a `leak guard` section (always in CI: the line proves the guard ran and its us/test).
- `scripts/ci/shard_counts.py shard` carries them into `shard-counts-N.json` (`leaks`, `notes`, `faults`; absent when empty). The `backend test counts (all shards)` job reports the whole run: the job summary, one `::warning title=leak guard (warn)` annotation of at most 40 lines (one line per file, kind and thing leaked) and the `test-leak-report` artifact (the full list). Read it without any shard log: [[../../.github/test-gate.ava.okf.md]].

## What it cannot see

An existing module attribute assigned a new value (a static lint's job; the agent identity is one, see below), a container mutated in place, a module object swapped through `sys.modules` (a per-test `del sys.modules[...]` and re-import), and the disk, sockets, processes and the databases (`_clean_state` owns those). In warn mode only the first leaker of a key is named: a later test that `delenv`s the now-present key is recorded and undone by monkeypatch, so the full list needs a fail-mode run, which restores after every leaker.

## The agent identity is restored, not compared

`ava.agent_identity._agent_id` and its siblings are assigned bare by hundreds of tests (the pattern `env_bootstrap` documents), so reporting them would turn the convention into a defect. The root plugin `identity_restore` (`tests/fixtures/identity_restore.py`, third in `pytest_plugins`) puts them back after every test instead: the five slots of `ava.agent_identity`, `turn_identity._process_agent_id` and the `_TURN_AGENT_ID` contextvar. Its table is the one owner of which slots make up the identity; `tests/ci/test_leak_guard.py` checks that each exists and that every annotated slot of `ava.agent_identity` is in it. `ava.self.AGENT_ID` is not touched: the module `__getattr__` serves it from those slots, and writing a value that was read back would store it for good (PR #3791). A test that stores it is a `module-attr` leak, which the guard names.

## Fixing a finding

Recipes per kind are in the fail-mode message and in [flaky-tests §9](../../conventions/flaky-tests.md): `setenv(name, "")` before `delenv`; `monkeypatch.setitem(vars(module), name, value)` or `mock.patch.object`; `monkeypatch.chdir`; restore the handler in a `finally`; nothing for the agent identity: `identity_restore` puts it back (below).

## Switching `warn` to `fail`

One default changes, and the `AVA_HOME` hook in `tests/fixtures/guards.py` (covered by the `env` check) goes. It waits for: warn running through a full nightly duration refresh with complete 17/17 reports; three consecutive main pushes with no leak; a drain run (the CI workflow dispatched on a scratch branch that forces `AVA_LEAK_GUARD=fail` on all shards, since a draft PR skips CI; it restores after each leaker and so shows the ones warn masks) with zero guard errors; the measured cost on CI at most 0.2 ms a test (`us/test` of the shard line, with the steady figure beside it); and no exemption list.
