"""Name the test that leaves process-global state different, at the moment it does.

The suite runs thousands of tests in one worker process. A test that leaves that process
different from how it found it (an environment key, a module attribute that is now stored,
the cwd, a signal handler, the agent identity) changes what every LATER test in the worker
sees. The red test is then an innocent victim far from its cause, and only in the runs where
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
  identity      the agent-identity singletons in WATCHED_ATTRS / WATCHED_CONTEXTVARS: a key scan
                cannot see an existing name take a new value, and 300+ test sites assign
                `ava.agent_identity._agent_id` bare. Delete the table when a fixture restores them.
  sys.path      a NOTE, never a leak: entries added or removed (73 test sites insert on purpose)

It does not see an existing module attribute re-assigned (a static lint's job), a container
mutated in place, a module object swapped through `sys.modules`, or the disk, sockets and
processes. POSIX is the supported platform (`os.environ._data`).

Reporting: each finding is a JUnit property on the leaker's testcase (`leak_guard`, or
`leak_guard_note` for a note; `leak_guard_fault` for a fault of the guard) that
`scripts/ci/shard_counts.py` collects into the cluster-wide report, and the terminal summary
prints the same list, xdist or not. Design and evidence: `tests/docs/test-leak-guard.ava.okf.md`.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import sys
import time
import types
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest

_Finding = tuple[str, str]  # (kind, detail)
MODES = ("off", "warn", "fail")
LEAK, NOTE, FAULT = "leak_guard", "leak_guard_note", "leak_guard_fault"  # JUnit property names
STRICT_KINDS = ("env", "module-attr", "cwd", "signal", "identity")
NOTE_KINDS = ("sys.path",)
# (module, attrs): module-level singletons whose changed VALUE is a leak. Add one only with a failure story.
WATCHED_ATTRS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "ava.agent_identity",
        ("_agent_id", "_owns_loop", "_actor", "_external_agent_id", "_external_identity"),
    ),
    ("base.native_process.turn_identity", ("_process_agent_id",)),
)
# (module, name): ContextVars, read through `.get()` in the main thread's context.
WATCHED_CONTEXTVARS: tuple[tuple[str, str], ...] = (
    ("base.native_process.turn_identity", "_TURN_AGENT_ID"),
)

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
_IGNORED_ENV = os.environ.encodekey("PYTEST_CURRENT_TEST")  # pytest rewrites it on every phase
# A path below the rootdir that is not a production module: a tests/ directory, an installed package.
_EXCLUDED_DIRS = re.compile(r"(?:^|[\\/])(?:tests|site-packages|node_modules)[\\/]")
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
    "identity": (
        "a test assigned the agent identity (`ava.agent_identity._agent_id = ...`, establish(), "
        "set_process_agent_id()) and did not put the previous value back. Use "
        "monkeypatch.setattr(module, name, value) so teardown restores it."
    ),
    "signal": "restore the handler in a finally / finalizer: signal.signal(sig, previous).",
}


def _environ_data() -> dict[Any, Any]:
    """The raw mapping behind `os.environ`: a copy through the public API is ~90 times slower."""
    return vars(os.environ)["_data"]


def _show(value: object) -> str:
    if isinstance(value, signal.Handlers):
        return value.name
    return getattr(value, "__qualname__", None) or repr(value)


class _ModuleWatch:
    """Which first-party module dicts gained a name inside one test.

    The fast path is one `sum(map(len, dicts))` per snapshot. Only when the total moved are the
    modules located; a dict is insertion-ordered, so the names added are the tail past the old
    length. A module imported during a test is baselined where first seen, and one a test swapped
    through `sys.modules` under a known name is not re-read.
    """

    def __init__(self, first_party: Callable[[types.ModuleType], bool]) -> None:
        self._first_party = first_party
        self._seen = 0  # len(sys.modules) at the last discovery
        self._judged: set[str] = set()
        self._names: list[str] = []
        self._dicts: list[dict[str, Any]] = []
        self._lens: list[int] = []
        self._total = 0
        # (module dict, attr) of the last check(): what fail mode takes back out.
        self.reported: list[tuple[dict[str, Any], str]] = []

    def _discover(self) -> None:
        if len(sys.modules) == self._seen:
            return
        self._seen = len(sys.modules)
        for name in (
            sys.modules.keys() - self._judged
        ):  # a C-level set difference: only the new names are judged
            module = sys.modules.get(name)
            self._judged.add(name)
            if not isinstance(module, types.ModuleType) or not self._first_party(module):
                continue
            self._names.append(name)
            self._dicts.append(module.__dict__)
            self._lens.append(len(module.__dict__))
            self._total += len(module.__dict__)

    def sync(self) -> None:
        """Rebase on the current sizes: changes between two tests belong to no test."""
        self._discover()
        total = sum(map(len, self._dicts))
        if total != self._total:
            self._lens = [len(d) for d in self._dicts]
            self._total = total

    def check(self) -> list[_Finding]:
        self._discover()
        self.reported = []
        total = sum(map(len, self._dicts))
        if total == self._total:
            return []
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


def _read_named() -> dict[tuple[str, str], object]:
    """Current value of every WATCHED_ATTRS / WATCHED_CONTEXTVARS entry whose module is loaded."""
    values: dict[tuple[str, str], object] = {}
    for module_name, attrs in WATCHED_ATTRS:
        module = sys.modules.get(module_name)
        if module is not None:
            for attr in attrs:
                values[(module_name, attr)] = getattr(module, attr)
    for module_name, name in WATCHED_CONTEXTVARS:
        module = sys.modules.get(module_name)
        if module is not None:
            values[(module_name, name)] = getattr(module, name).get()
    return values


def _cwd() -> str | None:
    try:
        return os.getcwd()  # noqa: PTH109 - the cheapest read; twice per test
    except FileNotFoundError:
        return None


class _Snapshot:
    __slots__ = ("cwd", "env", "named", "path", "signals")

    def __init__(self) -> None:
        self.env = dict(_environ_data())
        self.cwd = _cwd()
        self.path = list(sys.path)
        self.signals = tuple(signal.getsignal(sig) for sig in _SIGNALS)
        self.named = _read_named()


def _env_findings(before: dict[Any, Any]) -> list[_Finding]:
    env_now = _environ_data()
    if before.get(_IGNORED_ENV) != env_now.get(_IGNORED_ENV):  # keep the fast equality below true
        if _IGNORED_ENV in env_now:
            before[_IGNORED_ENV] = env_now[_IGNORED_ENV]
        else:
            before.pop(_IGNORED_ENV, None)
    if before == env_now:
        return []
    found: list[_Finding] = []
    for key in sorted(before.keys() | env_now.keys()):
        old, new = before.get(key), env_now.get(key)
        if old != new:
            how = "added" if old is None else "removed" if new is None else "changed"
            found.append(("env", f"{os.fsdecode(key)} was {how}"))
    return found


def _cwd_findings(before: str | None) -> list[_Finding]:
    now = _cwd()
    if now == before:
        return []
    return [("cwd", f"{before} -> {now or 'a directory that no longer exists'}")]


def _signal_findings(before: tuple[Any, ...]) -> list[_Finding]:
    found: list[_Finding] = []
    for sig, old_handler in zip(_SIGNALS, before, strict=True):
        now = signal.getsignal(sig)
        if now != old_handler:
            name = signal.Signals(sig).name
            found.append(("signal", f"{name} handler {_show(old_handler)} -> {_show(now)}"))
    return found


def _identity_findings(before: dict[tuple[str, str], object]) -> list[_Finding]:
    now = _read_named()
    return [
        ("identity", f"{key[0]}.{key[1]}: {_show(old)} -> {_show(now[key])}")
        for key, old in before.items()
        if key in now and now[key] != old
    ]


def _path_findings(before: list[str]) -> list[_Finding]:
    if before == sys.path:
        return []
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
        if old_handler is not None and signal.getsignal(sig) != old_handler:
            signal.signal(sig, old_handler)


def _restore_identity(before: dict[tuple[str, str], object]) -> None:
    for (module_name, attr), old_value in before.items():
        module = sys.modules[module_name]
        if (module_name, attr) in WATCHED_CONTEXTVARS:
            getattr(module, attr).set(old_value)
        else:
            setattr(module, attr, old_value)


class _Run:
    """The per-process state: configuration, the module watch, self-timing and the guard's faults."""

    def __init__(self) -> None:
        self.mode = "off"
        self.root = ""  # the rootdir prefix a first-party module's file starts with
        self.watch = _ModuleWatch(self.is_first_party)
        self.checked = 0
        self.ns = 0
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
        self.root = rootpath.rstrip(os.sep) + os.sep

    def is_first_party(self, module: types.ModuleType) -> bool:
        """A production module of this repository: a file under the rootdir, outside any tests/ directory.

        Test modules mutate themselves by design. Reading `__dict__` rather than `__file__` never
        runs a module's own `__getattr__`.
        """
        file = module.__dict__.get("__file__")
        if not isinstance(file, str) or not file.startswith(self.root):
            return False
        relative = file[len(self.root) :]
        return not (relative.startswith(".") or _EXCLUDED_DIRS.search(relative))

    def fault(self, stage: str, exc: Exception) -> bool:
        """Count a fault of the guard itself; True the first time `stage` faults in this process."""
        first = stage not in self.fault_counts
        self.fault_counts[stage] = self.fault_counts.get(stage, 0) + 1
        if first:
            lines = str(exc).splitlines()
            self.fault_first[stage] = f"{type(exc).__name__}: {lines[0][:200] if lines else ''}"
        return first

    def capture(self) -> _Snapshot:
        started = time.perf_counter_ns()
        self.watch.sync()
        snapshot = _Snapshot()
        self.ns += time.perf_counter_ns() - started
        return snapshot

    def compare(self, before: _Snapshot) -> list[_Finding]:
        started = time.perf_counter_ns()
        found = self._diff(before)
        self.checked += 1
        self.ns += time.perf_counter_ns() - started
        return found

    def _diff(self, before: _Snapshot) -> list[_Finding]:
        return [
            *_env_findings(before.env),
            *_cwd_findings(before.cwd),
            *_signal_findings(before.signals),
            *_identity_findings(before.named),
            *self.watch.check(),
            *_path_findings(before.path),
        ]

    def restore(self, before: _Snapshot, found: list[_Finding]) -> None:
        """Put back what the strict checks name (fail mode): a victim then never sees the leak."""
        kinds = {kind for kind, _ in found}
        if "env" in kinds:
            _restore_env(before.env)
        if "cwd" in kinds and before.cwd is not None:
            os.chdir(before.cwd)
        if "signal" in kinds:
            _restore_signals(before.signals)
        if "identity" in kinds:
            _restore_identity(before.named)
        if "module-attr" in kinds:
            for module_dict, attr in self.watch.reported:
                module_dict.pop(attr, None)
            self.watch.sync()

    def stats(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "ns": self.ns,
            "tracked": self.watch.tracked,
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
    return (
        f"leak guard ({_RUN.mode}): {len(leaks)} leak(s) in {len({f[0] for f in leaks})} test(s), "
        f"{notes} sys.path note(s), {sum(n for n, _ in faults.values())} guard fault(s); "
        f"{sum(s['checked'] for s in stats)} test(s) checked, {_micros_per_test(stats):.0f} us/test, "
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
