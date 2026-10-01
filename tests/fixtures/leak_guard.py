"""Name the test that leaves process-global state different, at the moment it does.

The suite runs thousands of tests in one worker process. A test that leaves that process
different from how it found it (an environment key, a module attribute that is now stored,
the cwd, a signal handler) changes what every LATER test in the worker sees. The red test is then an innocent victim far from its cause, and only in the runs where
the leaker and the victim share a worker; moving test files changes the order and the shard
composition, so a leak the old order hid starts to hit others. Three shapes seen here:

  * `monkeypatch.setattr(ava.self, "AGENT_ID", ...)` on a name the module `__getattr__`
    serves: monkeypatch records the dynamic value and its undo `setattr`s it back, so the name
    is a stored attribute for good (PR #3791).
  * `monkeypatch.delenv(key, raising=False)` on an absent key records nothing, so the key the
    code under test sets next stays in `os.environ` (PR #3793).
  * code under test assigns an attribute onto a module object that outlives the test
    (`register_namespace` sets `_qualname`) and nothing takes it off.

One function-scoped autouse fixture snapshots the cheap-to-compare containers before the test
and compares after the test's own fixtures are torn down, so the LEAKER is named, not a victim.
It must be the FIRST function-scoped autouse fixture to set up (second plugin in the repo-root
`pytest_plugins`, right after `env_bootstrap`): pytest tears fixtures down in reverse setup order,
so it compares after every function-scoped fixture (monkeypatch included) has restored what it
recorded, and before any module- or session-scoped fixture finalizes. What a wider-scope fixture
set up is already in the "before" snapshot, so a module's first and last test are not special.
`tests/ci/test_leak_guard.py` locks that position and the behavior.

`AVA_LEAK_GUARD` selects the mode (any other value stops the run):
  warn  (default) observe only: never changes a test outcome, never puts any state back, and a
        fault of the guard itself is one report line, never a test result;
  fail  put back what the strict checks name, then fail the leaker at its own teardown, so
        victims stay green and only the leaker is red;
  off   the fixture is a no-op.

Checks (kind: what is compared). Values are never printed.
  env           `os.environ` keys added, removed or changed
  module-attr   a first-party module's `__dict__` gained a non-dunder, non-module name
  cwd           `os.getcwd()`
  signal        `signal.getsignal` of the common signals
  sys.path      a NOTE, never a leak: entries added or removed (73 test sites insert on purpose)

Cost: two snapshots a test, so the guard stays in bulk C-level passes (`map` / `compress` / one `==`
per container) and enters a handful of Python frames; under CI's coverage tracer the frame count,
not the C work, sets the price. `tests/ci/test_leak_guard_cost.py` counts them.

It does not see an existing module attribute re-assigned (a static lint's job: the agent identity
is one, and `identity_restore` puts it back instead), a container mutated in place, a module object
swapped through `sys.modules`, or the disk, sockets and processes. POSIX is the supported platform
(`os.environ._data`).

Reporting: each finding is a JUnit property on the leaker's testcase (`leak_guard`, or
`leak_guard_note` for a note; `leak_guard_fault` for a fault of the guard) that
`scripts/ci/shard_counts.py` collects into the cluster-wide report, and the terminal summary
prints the same list, xdist or not. Design and evidence: `tests/docs/test-leak-guard.ava.okf.md`.
"""

from __future__ import annotations

import contextlib
import importlib
import operator
import os
import re
import signal
import sys
import time
import types
from collections.abc import Callable, Iterator
from itertools import compress, filterfalse, repeat
from typing import Any, cast

import pytest

_Finding = tuple[str, str]  # (kind, detail)
MODES = ("off", "warn", "fail")
LEAK, NOTE, FAULT = "leak_guard", "leak_guard_note", "leak_guard_fault"  # JUnit property names
STRICT_KINDS = ("env", "module-attr", "cwd", "signal")
NOTE_KINDS = ("sys.path",)

_SIGNALS = tuple(
    getattr(signal, name)
    for name in (
        "SIGINT",
        "SIGTERM",
        "SIGHUP",
        "SIGALRM",
        "SIGUSR1",
        "SIGUSR2",
        "SIGPIPE",
        "SIGCHLD",
    )
    if hasattr(signal, name)
)
# `signal.getsignal` wraps this C function to turn 0 and 1 into enum members: three Python frames
# per signal and per read. The raw answer (0, 1, a callable or None) compares just as well.
_getsignal: Callable[[int], Any] = importlib.import_module("_signal").getsignal
_now = time.perf_counter_ns
_IGNORED_ENV = os.environ.encodekey("PYTEST_CURRENT_TEST")  # pytest rewrites it on every phase
# A directory below the rootdir that holds no production module: tests, an installed package.
_EXCLUDED_DIRS = r"(?:tests|site-packages|node_modules)"
_module_dict = operator.attrgetter("__dict__")
_module_file = operator.methodcaller("get", "__file__")
_HINTS = {
    "env": (
        "monkeypatch.delenv(name, raising=False) records nothing for an absent key, so whatever "
        "the code under test sets next stays for every later test in this worker. Call "
        "monkeypatch.setenv(name, '') before the delenv, or put the key in the fixture that "
        "snapshots and restores it. Undo through monkeypatch or patch.dict, not os.environ[...]."
    ),
    "module-attr": (
        "a name now sits in the module's __dict__ that was not there before. If a test did "
        "monkeypatch.setattr on a name the module serves from __getattr__, use "
        "monkeypatch.setitem(vars(module), name, value) (its undo deletes the key). If code "
        "under test assigned it, undo that in a finalizer or pre-register it with "
        "monkeypatch.setattr(module, name, value, raising=False)."
    ),
    "cwd": "use monkeypatch.chdir(path); a bare os.chdir needs a finally that changes back.",
    "signal": "restore the handler in a finally / finalizer: signal.signal(sig, previous).",
}


def _environ_data() -> dict[Any, Any]:
    """The raw mapping behind `os.environ`: a copy through the public API is ~90 times slower."""
    return vars(os.environ)["_data"]


def _show(value: object) -> str:
    return getattr(value, "__qualname__", None) or repr(value)


def _show_handler(handler: object) -> str:
    """`_signal.getsignal` answers SIG_DFL and SIG_IGN as plain ints: name them as `signal` does."""
    return signal.Handlers(handler).name if isinstance(handler, int) else _show(handler)


def _first_party_pattern(root: str) -> re.Pattern[str]:
    """What the file of a production module of this repository looks like.

    A path below `root` (ending in a separator) that is not inside a top-level dot-directory
    (`.venv`, `.git`) and has no tests, site-packages or node_modules directory below it. Test
    modules mutate themselves by design, an installed package is not ours. The directory walk is
    one component at a time, so a deep path costs a few dozen steps, not a backtrack per character.
    """
    return re.compile(re.escape(root) + rf"(?!\.)(?!(?:[^\\/]*[\\/])*?{_EXCLUDED_DIRS}[\\/])")


class _ModuleWatch:
    """Which first-party module dicts gained a name inside one test.

    Locating modules is bulk C-level work, and a test costs a handful of Python calls under a
    coverage tracer. The fast path is one `sum(map(len, dicts))` per snapshot. Only when the total
    moved are the modules located; a dict is insertion-ordered, so the names added are the tail
    past the old length. Modules are located once per name: the first pass judges everything
    already in `sys.modules` (about eight thousand, almost all installed packages, whose
    submodules `_inside` skips unread), later passes only the names an import added. A module
    imported during a test is baselined where first seen, and one a test swapped through
    `sys.modules` under a known name is not re-read.
    """

    def __init__(self, match: Callable[[str], object]) -> None:
        self._match = match  # the file of a first-party module matches, any other file does not
        self._seen = 0  # len(sys.modules) at the last discovery
        self._order: list[str] = []  # the keys of sys.modules the last discovery saw, in order
        self._judged: set[str] = set()
        self._names: list[str] = []
        self._dicts: list[dict[str, Any]] = []
        self._lens: list[int] = []
        self._total = 0
        self.discover_ns = 0  # time spent locating modules, one-time pass included
        self.judged = 0  # modules looked at: the cost of locating, not of watching
        # Locating passes that found names to judge (the first, then one per import), and scans
        # that found the total moved (each costs one more pass over the dicts).
        self.passes = 0
        self.slow = 0
        # (module dict, attr) of the last check(): what fail mode takes back out.
        self.reported: list[tuple[dict[str, Any], str]] = []

    def _unjudged(self) -> list[str]:
        """The names in `sys.modules` that no earlier discovery judged.

        The keys, in insertion order, are one list copy. When the list the last discovery saw is
        still their prefix nothing before it moved or left, so only the tail can hold new names;
        otherwise (a module was removed, or removed and added back) every key is checked.
        """
        current = list(sys.modules)
        seen = len(self._order)
        fresh = current[seen:] if current[:seen] == self._order else current
        self._order = current
        return list(filterfalse(self._judged.__contains__, fresh))

    def _inside(self, names: list[str]) -> list[str]:
        """`names` without the submodules of a top-level module that cannot hold first-party files.

        Almost every module in `sys.modules` belongs to an installed package, and reading a module
        is what costs. A package keeps its submodules below its own directory, so when the file of
        a top-level module is known and not first-party, its submodules are skipped unread. A
        top-level module that is not loaded, or has no file (a namespace package, `tests`), keeps
        all its names. A first-party file loaded under the name of a submodule of an installed
        package is therefore not watched; `tests/ci/test_leak_guard_cost.py` checks that this
        process has none.
        """
        tops = list(map(operator.itemgetter(0), map(str.partition, names, repeat("."))))
        unique = list(set(tops))
        modules = list(map(sys.modules.get, unique))
        is_module = list(map(isinstance, modules, repeat(types.ModuleType)))
        unique = list(compress(unique, is_module))
        files: list[Any] = list(map(_module_file, map(_module_dict, compress(modules, is_module))))
        has_file = list(map(isinstance, files, repeat(str)))
        unique, files = list(compress(unique, has_file)), list(compress(files, has_file))
        outside = set(compress(unique, map(operator.not_, map(self._match, files))))
        return list(compress(names, map(operator.not_, map(outside.__contains__, tops))))

    def _locate(self, names: list[str]) -> tuple[list[str], list[dict[str, Any]]]:
        """The first-party modules among `names`: their names, and their module dicts.

        Every step is one `map`, `compress` or `list` over all the names (no Python call per module),
        and each keeps only what the next one needs: the file of a module is read once, and a
        non-module entry, a module without a string `__file__` or one outside the repository drops
        out before the pattern runs. `__dict__` is read, never `__file__`, so a module's own
        `__getattr__` never runs.
        """
        modules = list(map(sys.modules.get, names))
        is_module = list(map(isinstance, modules, repeat(types.ModuleType)))
        names = list(compress(names, is_module))
        dicts: list[dict[str, Any]] = list(map(_module_dict, compress(modules, is_module)))
        files: list[Any] = list(map(_module_file, dicts))
        has_file = list(map(isinstance, files, repeat(str)))
        names, dicts = list(compress(names, has_file)), list(compress(dicts, has_file))
        ours = list(map(self._match, compress(files, has_file)))
        return list(compress(names, ours)), list(compress(dicts, ours))

    def _discover(self) -> None:
        if len(sys.modules) == self._seen:
            return
        self._seen = len(sys.modules)
        started = _now()
        names = self._unjudged()
        self._judged.update(names)
        self.judged += len(names)
        self.passes += bool(names)
        found, dicts = self._locate(self._inside(names))
        lens = list(map(len, dicts))
        self._names.extend(found)
        self._dicts.extend(dicts)
        self._lens.extend(lens)
        self._total += sum(lens)
        self.discover_ns += _now() - started

    def sync(self) -> None:
        """Rebase on the current sizes: changes between two tests belong to no test."""
        self._discover()
        total = sum(map(len, self._dicts))
        if total != self._total:
            self.slow += 1
            self._lens = [len(d) for d in self._dicts]
            self._total = total

    def check(self) -> list[_Finding]:
        self._discover()
        self.reported = []
        total = sum(map(len, self._dicts))
        if total == self._total:
            return []
        self.slow += 1
        found: list[_Finding] = []
        for i, module_dict in enumerate(self._dicts):
            size = len(module_dict)
            if size > self._lens[i]:
                for attr in list(module_dict)[self._lens[i] :]:
                    value = module_dict[attr]
                    if attr.startswith("__") or isinstance(value, types.ModuleType):
                        continue  # a submodule import binds itself into its parent
                    found.append(
                        (
                            "module-attr",
                            f"{self._names[i]}.{attr} ({type(value).__name__}) was added",
                        )
                    )
                    self.reported.append((module_dict, attr))
            self._lens[i] = size
        self._total = total
        return found

    @property
    def tracked(self) -> int:
        return len(self._dicts)


def _cwd() -> str | None:
    try:
        return os.getcwd()  # noqa: PTH109 - the cheapest read; twice per test
    except FileNotFoundError:
        return None


def _handlers() -> tuple[Any, ...]:
    """The raw handler of every watched signal: one C call, no Python frame per signal."""
    return tuple(map(_getsignal, _SIGNALS))


class _Snapshot:
    __slots__ = ("cwd", "env", "path", "signals")

    def __init__(self) -> None:
        self.env = _environ_data().copy()
        self.cwd = _cwd()
        self.path = sys.path[:]
        self.signals = _handlers()


def _ignore_env_noise(before: dict[Any, Any], env_now: dict[Any, Any]) -> None:
    """Make the key pytest rewrites on every phase equal on both sides, so one `==` decides."""
    if _IGNORED_ENV in env_now:
        before[_IGNORED_ENV] = env_now[_IGNORED_ENV]
    else:
        before.pop(_IGNORED_ENV, None)


def _env_findings(before: dict[Any, Any], env_now: dict[Any, Any]) -> list[_Finding]:
    found: list[_Finding] = []
    for key in sorted(before.keys() | env_now.keys()):
        old, new = before.get(key), env_now.get(key)
        if old != new:
            how = "added" if old is None else "removed" if new is None else "changed"
            found.append(("env", f"{os.fsdecode(key)} was {how}"))
    return found


def _cwd_findings(before: str | None, now: str | None) -> list[_Finding]:
    return [("cwd", f"{before} -> {now or 'a directory that no longer exists'}")]


def _signal_findings(before: tuple[Any, ...], now: tuple[Any, ...]) -> list[_Finding]:
    return [
        (
            "signal",
            f"{signal.Signals(sig).name} handler {_show_handler(old)} -> {_show_handler(new)}",
        )
        for sig, old, new in zip(_SIGNALS, before, now, strict=True)
        if new != old
    ]


def _path_findings(before: list[str]) -> list[_Finding]:
    added = [p for p in sys.path if p not in before]
    removed = [p for p in before if p not in sys.path]
    if not (added or removed):  # a pure reorder (`sys.path.insert(0, p)` of a present entry)
        return []
    return [("sys.path", f"added {added} removed {removed}")]


def _restore_env(before: dict[Any, Any]) -> None:
    env_now = _environ_data()
    for key in [k for k in env_now if k not in before and k != _IGNORED_ENV]:
        del os.environ[os.fsdecode(key)]
    for key, value in before.items():
        if key != _IGNORED_ENV and env_now.get(key) != value:
            os.environ[os.fsdecode(key)] = os.fsdecode(value)


def _restore_signals(before: tuple[Any, ...]) -> None:
    for sig, old_handler in zip(_SIGNALS, before, strict=True):
        if old_handler is not None and _getsignal(sig) != old_handler:
            signal.signal(sig, old_handler)


class _Run:
    """The per-process state: configuration, the module watch, self-timing and the guard's faults."""

    def __init__(self) -> None:
        self.mode = "off"
        # Until configured nothing is first-party.
        self.watch = _ModuleWatch(re.compile(r"(?!)").match)
        self.checked = 0
        self.ns = 0
        self.scan_ns = 0  # time in the module scans, locating included
        self.fault_counts: dict[str, int] = {}
        self.fault_first: dict[str, str] = {}
        # Controller side: (nodeid, property, kind, detail) of every finding the reports carried.
        self.findings: list[tuple[str, str, str, str]] = []
        self.worker_stats: list[dict[str, Any]] = []

    def configure(self, rootpath: str) -> None:
        mode = os.environ.get("AVA_LEAK_GUARD", "warn")
        if mode not in MODES:
            raise pytest.UsageError(f"AVA_LEAK_GUARD={mode!r}: expected one of {', '.join(MODES)}")
        self.mode = mode
        self.watch = _ModuleWatch(_first_party_pattern(rootpath.rstrip(os.sep) + os.sep).match)

    def fault(self, stage: str, exc: Exception) -> bool:
        """Count a fault of the guard itself; True the first time `stage` faults in this process."""
        first = stage not in self.fault_counts
        self.fault_counts[stage] = self.fault_counts.get(stage, 0) + 1
        if first:
            lines = str(exc).splitlines()
            self.fault_first[stage] = f"{type(exc).__name__}: {lines[0][:200] if lines else ''}"
        return first

    def capture(self) -> _Snapshot:
        started = _now()
        self.watch.sync()
        self.scan_ns += _now() - started
        snapshot = _Snapshot()
        self.ns += _now() - started
        return snapshot

    def compare(self, before: _Snapshot) -> list[_Finding]:
        started = _now()
        found = self._diff(before)
        self.checked += 1
        self.ns += _now() - started
        return found

    def _diff(self, before: _Snapshot) -> list[_Finding]:
        """What differs from `before`, in the order the report lists it.

        The clean case is the hot one: each container is decided by one C-level `==`, and only a
        difference pays for the Python that names it.
        """
        found: list[_Finding] = []
        env_now = _environ_data()
        if before.env.get(_IGNORED_ENV) != env_now.get(_IGNORED_ENV):
            _ignore_env_noise(before.env, env_now)
        if before.env != env_now:
            found += _env_findings(before.env, env_now)
        cwd_now = _cwd()
        if cwd_now != before.cwd:
            found += _cwd_findings(before.cwd, cwd_now)
        handlers_now = _handlers()
        if handlers_now != before.signals:
            found += _signal_findings(before.signals, handlers_now)
        scanned = _now()
        found += self.watch.check()
        self.scan_ns += _now() - scanned
        if before.path != sys.path:
            found += _path_findings(before.path)
        return found

    def restore(self, before: _Snapshot, found: list[_Finding]) -> None:
        """Put back what the strict checks name (fail mode): a victim then never sees the leak."""
        kinds = {kind for kind, _ in found}
        if "env" in kinds:
            _restore_env(before.env)
        if "cwd" in kinds and before.cwd is not None:
            os.chdir(before.cwd)
        if "signal" in kinds:
            _restore_signals(before.signals)
        if "module-attr" in kinds:
            for module_dict, attr in self.watch.reported:
                module_dict.pop(attr, None)
            self.watch.sync()

    def stats(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "ns": self.ns,
            "scan_ns": self.scan_ns,
            "tracked": self.watch.tracked,
            "discover_ns": self.watch.discover_ns,
            "judged": self.watch.judged,
            "passes": self.watch.passes,
            "slow": self.watch.slow,
            "faults": {
                stage: [n, self.fault_first[stage]] for stage, n in self.fault_counts.items()
            },
        }


_RUN = _Run()


def _contained[T](
    stage: str, item: pytest.Item | None, call: Callable[..., T], *args: Any
) -> T | None:
    """Warn mode: a fault of the guard itself is one report line (and one JUnit property), never a test result.

    In fail mode a guard that cannot do its job must be loud, so the fault propagates.
    """
    try:
        return call(*args)
    except Exception as exc:
        if _RUN.mode == "fail":
            raise
        if _RUN.fault(stage, exc) and item is not None:
            # The property write may be the very thing that broke: then there is nothing to write it with.
            with contextlib.suppress(Exception):
                item.user_properties.append((FAULT, f"{stage}: {_RUN.fault_first[stage]}"))
        return None


def pytest_configure(config: pytest.Config) -> None:
    _RUN.configure(str(config.rootpath))


def _record(item: pytest.Item, found: list[_Finding]) -> None:
    for kind, detail in found:
        item.user_properties.append((NOTE if kind in NOTE_KINDS else LEAK, f"{kind}: {detail}"))


@pytest.fixture(autouse=True)
def _leak_guard(request: pytest.FixtureRequest) -> Iterator[None]:
    run, item = _RUN, cast("pytest.Item", request.node)
    if run.mode == "off":
        yield
        return
    before = _contained("snapshot", item, run.capture)
    yield
    if before is None:
        return
    found = _contained("compare", item, run.compare, before)
    if not found:
        return
    _contained("report", item, _record, item, found)
    strict = [(kind, detail) for kind, detail in found if kind in STRICT_KINDS]
    if run.mode != "fail" or not strict:
        return
    run.restore(before, strict)
    lines = [f"{item.nodeid} leaked process-global state past its own teardown:"]
    lines += [f"  {kind}: {detail}" for kind, detail in strict]
    lines += [f"  fix ({kind}): {_HINTS[kind]}" for kind in dict.fromkeys(k for k, _ in strict)]
    pytest.fail("\n".join(lines), pytrace=False)


# --------------------------------------------------------------------------- reporting (controller side)
def _collect(report: pytest.TestReport) -> None:
    for name, value in report.user_properties:
        if name in (LEAK, NOTE):
            kind, _, detail = str(value).partition(": ")
            _RUN.findings.append((report.nodeid, name, kind, detail))


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "teardown":
        _contained("collect", None, _collect, report)


def _hand_over(output: dict[str, Any]) -> None:
    output[LEAK] = _RUN.stats()


def pytest_sessionfinish(session: pytest.Session) -> None:
    """A xdist worker hands its self-timing and faults to the controller through `workeroutput`."""
    output = getattr(session.config, "workeroutput", None)
    if output is not None and _RUN.mode != "off":
        _contained("handoff", None, _hand_over, output)


def _take_over(node: Any) -> None:
    stats = getattr(node, "workeroutput", {}).get(LEAK)
    if stats:
        _RUN.worker_stats.append(stats)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: object) -> None:  # xdist only
    del error
    _contained("handoff", None, _take_over, node)


def pytest_terminal_summary(terminalreporter: Any) -> None:
    if _RUN.mode == "off":
        return
    try:
        _write_summary(terminalreporter)
    except Exception as exc:  # the one place a fault cannot be reported through the summary itself
        if _RUN.mode == "fail":
            raise
        terminalreporter.write_line(
            f"leak guard ({_RUN.mode}): the summary failed: {type(exc).__name__}: {exc}"
        )


def _merged_faults(stats: list[dict[str, Any]]) -> dict[str, list[Any]]:
    faults: dict[str, list[Any]] = {}
    for stat in stats:
        for stage, (count, first) in stat["faults"].items():
            faults.setdefault(stage, [0, first])[0] += count
    return faults


def _micros_per_test(stats: list[dict[str, Any]]) -> float:
    checked = sum(s["checked"] for s in stats)
    return sum(s["ns"] for s in stats) / checked / 1000 if checked else 0.0


def _summary_head(stats: list[dict[str, Any]], faults: dict[str, list[Any]]) -> str:
    leaks = [f for f in _RUN.findings if f[1] == LEAK]
    notes = sum(f[1] == NOTE for f in _RUN.findings)
    checked = sum(s["checked"] for s in stats)
    locating_ns = sum(s["discover_ns"] for s in stats)
    scan_us = (sum(s["scan_ns"] for s in stats) - locating_ns) / max(checked, 1) / 1000
    return (
        f"leak guard ({_RUN.mode}): {len(leaks)} leak(s) in {len({f[0] for f in leaks})} test(s), "
        f"{notes} sys.path note(s), {sum(n for n, _ in faults.values())} guard fault(s); "
        f"{checked} test(s) checked, {_micros_per_test(stats):.0f} us/test"
        f" ({scan_us:.0f} us/test of it module scans, {locating_ns / 1e6:.0f} ms locating "
        f"{sum(s['judged'] for s in stats)} module(s) in {sum(s['passes'] for s in stats)} pass(es)), "
        f"{sum(s['slow'] for s in stats)} slow scan(s), "
        f"{max(s['tracked'] for s in stats)} module dicts watched"
    )


def _summary_lines() -> list[str]:
    """The guard's terminal section: a head, the guard's faults, then every finding."""
    stats = [*_RUN.worker_stats, _RUN.stats()]  # under xdist the controller itself checks no test
    faults = _merged_faults(stats)
    if not (_RUN.findings or faults or os.environ.get("GITHUB_ACTIONS")):
        return []  # silent on a clean local run; CI always prints the line (proof the guard ran, and its cost)
    return [
        _summary_head(stats, faults),
        *(f"  FAULT {stage} x{count}: {first}" for stage, (count, first) in faults.items()),
        *(
            f"  {'LEAK' if name == LEAK else 'NOTE'} {nodeid} -- {kind}: {detail}"
            for nodeid, name, kind, detail in _RUN.findings
        ),
    ]


def _write_summary(terminalreporter: Any) -> None:
    lines = _summary_lines()
    if lines:
        terminalreporter.write_sep("=", "leak guard")
        for line in lines:
            terminalreporter.write_line(line)
