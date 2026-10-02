"""What keeps the root leak guard cheap: no Python call per module, a handful per test, work only on a change.

The guard runs twice around every test of the suite, in every xdist worker, under a coverage tracer
on CI. Every Python frame it enters then costs a trace event, so its cost is set by how many frames a
test enters, not by how long the C-level work between them takes. Timing is no assertion here (it
flakes); the property that keeps the cost down is: bulk C-level passes, and Python only for a
difference. These tests count the Python calls the guard makes (`sys.setprofile`), which is exact and
the same on every machine.

`tests/ci/test_leak_guard.py` locks what the guard names and when; this file locks how it gets there.
"""

from __future__ import annotations

import gc
import importlib.machinery
import os
import re
import signal
import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.fixtures import leak_guard

# What one test may spend: the fixture's own frames, the guard's phases, and the few helpers that read
# the cwd, the signals, the identity slots and the environment. The previous implementation entered
# about forty frames per phase (a Python `getsignal`, `_int_to_enum` and enum construction per signal).
_FRAMES_PER_PHASE = 15
# Locating modules is a fixed number of passes, however many modules there are (the previous
# implementation entered one frame per name in `sys.modules`).
_FRAMES_TO_LOCATE = 15
_FAKE_MODULES = 400


class _ImportingSpec(importlib.machinery.ModuleSpec):
    """`ModuleSpec` as the import system leaves it: `_initializing` is set while a body runs."""

    _initializing: bool = False


def _python_calls(fn: Callable[[], object]) -> int:
    """How many Python-level function calls `fn` makes (C functions are not counted).

    Garbage is collected before and not during: a finalizer of an earlier test's object (a redis
    connection's `__del__`, seen in a cold run) would otherwise be counted as the guard's own frames.
    """
    calls = 0
    gc.collect()

    def profile(_frame: types.FrameType, event: str, _arg: object) -> None:
        nonlocal calls
        calls += event == "call"

    previous = sys.getprofile()
    was_enabled = gc.isenabled()
    gc.disable()
    sys.setprofile(profile)
    try:
        fn()
    finally:
        sys.setprofile(previous)
        if was_enabled:
            gc.enable()
    return calls


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    return tmp_path / "repo"


@pytest.fixture
def modules(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> list[types.ModuleType]:
    """Stand-in first-party modules registered in `sys.modules`, next to the real thousands."""
    made: list[types.ModuleType] = []
    for i in range(_FAKE_MODULES):
        module = types.ModuleType(f"leak_cost_fake_{i}")
        module.__file__ = str(repo_root / "pkg" / f"m{i}.py")
        monkeypatch.setitem(sys.modules, module.__name__, module)
        made.append(module)
    return made


@pytest.fixture
def run(repo_root: Path, modules: list[types.ModuleType]) -> leak_guard._Run:
    del modules  # registered before the guard looks
    guard = leak_guard._Run()
    guard.configure(str(repo_root))
    return guard


# -- locating modules ----------------------------------------------------------------------------


def test_locating_the_modules_enters_no_python_frame_per_module(run: leak_guard._Run) -> None:
    """The first pass judges everything already imported (thousands of installed modules)."""
    assert len(sys.modules) > _FAKE_MODULES
    assert _python_calls(run.watch.sync) <= _FRAMES_TO_LOCATE
    assert run.watch.tracked == _FAKE_MODULES


def test_a_module_is_judged_once_and_only_a_new_name_is_judged_again(
    run: leak_guard._Run, repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run.watch.sync()
    judged = run.watch.judged
    assert judged >= _FAKE_MODULES
    assert _python_calls(run.watch.sync) <= 3  # sync -> _discover: the length did not move
    assert run.watch.judged == judged

    late = types.ModuleType("leak_cost_fake_late")
    late.__file__ = str(repo_root / "pkg" / "late.py")
    monkeypatch.setitem(sys.modules, late.__name__, late)
    run.watch.sync()
    assert run.watch.judged == judged + 1
    assert run.watch.tracked == _FAKE_MODULES + 1


def test_a_module_another_thread_is_still_importing_is_watched_once_it_is_done(
    run: leak_guard._Run, repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A background thread's lazy import registers the module before its body ran. The names the body
    then defines are that import's, not a leak of the test that happens to be running."""
    spec = _ImportingSpec("leak_cost_fake_importing", None)
    spec._initializing = True  # what the import system sets until the body is done
    importing = types.ModuleType(spec.name)
    importing.__spec__ = spec
    importing.__file__ = str(repo_root / "pkg" / "importing.py")
    monkeypatch.setitem(sys.modules, importing.__name__, importing)

    before = run.capture()
    assert run.watch.tracked == _FAKE_MODULES  # half built: not watched yet
    importing.__dict__.update(defined_by_the_body=1)
    spec._initializing = False
    assert run.compare(before) == []
    assert run.watch.tracked == _FAKE_MODULES + 1  # done: watched from here on

    before = run.capture()
    importing.__dict__["added_by_a_test"] = 1
    assert [kind for kind, _ in run.compare(before)] == ["module-attr"]


def test_an_unchanged_state_names_nothing_and_makes_no_second_look(
    run: leak_guard._Run, modules: list[types.ModuleType]
) -> None:
    before = run.capture()
    assert run.compare(before) == []
    before = run.capture()
    modules[7].__dict__["added_by_a_test"] = 1
    assert [kind for kind, _ in run.compare(before)] == ["module-attr"]
    # The new size is the new baseline: the same state is not reported again, by this or the next test.
    assert run.compare(before) == []
    assert run.compare(run.capture()) == []


def test_the_slow_path_names_exactly_the_dicts_that_grew(
    run: leak_guard._Run, modules: list[types.ModuleType]
) -> None:
    before = run.capture()
    modules[3].__dict__.update(leaked=1, __dunder__=2, sub=types.ModuleType("sub"))
    modules[9].__dict__["also_leaked"] = "x"
    found = run.compare(before)
    assert sorted(found) == [
        ("module-attr", "leak_cost_fake_3.leaked (int) was added"),
        ("module-attr", "leak_cost_fake_9.also_leaked (str) was added"),
    ]
    assert {attr for _, attr in run.watch.reported} == {
        "leaked",
        "also_leaked",
    }  # what fail mode removes


def test_a_dict_that_shrank_is_a_new_baseline_not_a_finding(
    run: leak_guard._Run, modules: list[types.ModuleType]
) -> None:
    modules[1].__dict__["temporary"] = 1
    before = run.capture()  # a change between two tests belongs to no test
    del modules[1].__dict__["temporary"]
    modules[2].__dict__.update(one=1, two=2)
    assert sorted(run.compare(before)) == [
        ("module-attr", "leak_cost_fake_2.one (int) was added"),
        ("module-attr", "leak_cost_fake_2.two (int) was added"),
    ]
    assert run.compare(before) == []


def test_a_change_between_two_tests_belongs_to_no_test(
    run: leak_guard._Run, modules: list[types.ModuleType]
) -> None:
    modules[4].__dict__["set_up_by_a_wider_fixture"] = 1
    before = run.capture()
    assert run.compare(before) == []


# -- the steady state: a handful of frames per phase ----------------------------------------------


def test_a_test_enters_a_handful_of_python_frames(run: leak_guard._Run) -> None:
    run.watch.sync()  # the one-time pass is paid by the first test of a worker, not by every test
    before = run.capture()
    assert _python_calls(run.capture) <= _FRAMES_PER_PHASE
    assert _python_calls(lambda: run.compare(before)) <= _FRAMES_PER_PHASE


def test_the_signals_are_read_without_the_python_wrapper(
    run: leak_guard._Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`signal.getsignal` is three frames and an enum construction per signal: the raw answer is enough."""

    def forbidden(_signum: int) -> object:
        raise AssertionError("the guard went through signal.getsignal")

    monkeypatch.setattr(signal, "getsignal", forbidden)
    before = run.capture()
    assert run.compare(before) == []


def _handler(_signum: int, _frame: object) -> None:  # pragma: no cover - never called
    raise KeyboardInterrupt


def test_a_changed_signal_is_named_as_the_wrapper_names_it() -> None:
    """`_signal.getsignal` answers 0 and 1 as plain ints: they are named SIG_DFL and SIG_IGN again."""
    rest = (0,) * (len(leak_guard._SIGNALS) - 4)
    found = leak_guard._signal_findings(
        (0, 1, None, _handler, *rest), (1, 0, _handler, None, *rest)
    )
    first = [signal.Signals(sig).name for sig in leak_guard._SIGNALS[:4]]
    assert found == [
        ("signal", f"{first[0]} handler SIG_DFL -> SIG_IGN"),
        ("signal", f"{first[1]} handler SIG_IGN -> SIG_DFL"),
        ("signal", f"{first[2]} handler None -> _handler"),
        ("signal", f"{first[3]} handler _handler -> None"),
    ]


# -- what counts as first-party: the same rule as before ------------------------------------------

ROOT = "/work/repo/"
_EXCLUDED_DIRS = re.compile(r"(?:^|[\\/])(?:tests|site-packages|node_modules)[\\/]")


def _the_rule(file: str, root: str = ROOT) -> bool:
    """The rule the guard applied one module at a time before it located modules in bulk, verbatim."""
    if not file.startswith(root):
        return False
    relative = file[len(root) :]
    return not (relative.startswith(".") or _EXCLUDED_DIRS.search(relative))


FILES = [
    "/work/repo/ava/self.py",
    "/work/repo/ava/sub/deep/er/module.py",
    "/work/repo/conftest.py",
    "/work/repo/tests/fixtures/leak_guard.py",
    "/work/repo/base/telemetry/tests/test_x.py",
    "/work/repo/pkg/tests/helpers/y.py",
    "/work/repo/pkg/tests_extra/m.py",
    "/work/repo/pkg/mytests/m.py",
    "/work/repo/pkg/tests.py",
    "/work/repo/pkg/tests",
    "/work/repo/.venv/lib/python3.12/site-packages/pytest/__init__.py",
    "/work/repo/.git/hooks.py",
    "/work/repo/ui/web/node_modules/a/b.py",
    "/work/repo/pkg/site-packages/m.py",
    "/work/repo/pkg/node_modules.py",
    "/work/repo-other/pkg/m.py",
    "/work/repo",
    "/work/repo/",
    "/usr/lib/python3.12/json/__init__.py",
    "/work/repo/pkg\\tests\\m.py",
    "/work/repo/pkg\\mytests\\m.py",
    "/work/repo/pk\ng/m.py",
    "/work/repo/pk\ng/tests/m.py",
    "",
]


@pytest.mark.parametrize("file", FILES)
def test_the_first_party_pattern_applies_the_same_rule(file: str) -> None:
    assert bool(leak_guard._first_party_pattern(ROOT).match(file)) is _the_rule(file)


def test_every_first_party_module_of_this_process_is_located(
    request: pytest.FixtureRequest,
) -> None:
    """The guard skips the submodules of an installed top-level module unread; none may be ours.

    The per-module rule over everything this process has imported is the oracle. This is the check
    that the import system's rule (a package keeps its submodules below its own directory) holds
    for the real repository: a first-party file loaded under an installed package's name fails here.
    """
    root = str(request.config.rootpath).rstrip(os.sep) + os.sep  # the root the guard itself uses
    watch = leak_guard._ModuleWatch(leak_guard._first_party_pattern(root).match)
    watch.sync()
    expected = {
        name
        for name, module in list(sys.modules.items())
        if isinstance(module, types.ModuleType)
        and isinstance(file := vars(module).get("__file__"), str)
        and _the_rule(file, root)
    }
    assert len(expected) > 100  # the repository's own modules are imported by the root plugins
    located = set(watch._names)
    assert sorted(expected - located) == [], "first-party modules the guard does not watch"
    assert sorted(located - expected) == [], "modules the guard watches that the rule rejects"


def test_a_first_party_file_loaded_inside_an_installed_package_is_not_looked_at(
    run: leak_guard._Run, repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented edge of skipping by top-level module: its own file decides, not the submodule's."""
    for name, file in (
        ("leak_cost_installed", "/site-packages-elsewhere/leak_cost_installed/__init__.py"),
        ("leak_cost_installed.hidden", str(repo_root / "pkg" / "hidden.py")),
        ("leak_cost_namespace", None),  # a namespace package has no file: its names are all judged
        ("leak_cost_namespace.ours", str(repo_root / "pkg" / "ours.py")),
    ):
        module = _with_file(file)
        monkeypatch.setitem(sys.modules, name, module)
    run.watch.sync()
    names = set(run.watch._names)
    assert "leak_cost_installed.hidden" not in names
    assert "leak_cost_namespace.ours" in names


def test_a_module_removed_and_added_back_is_not_judged_twice_and_a_new_one_is_found(
    run: leak_guard._Run, repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The names the last discovery saw are a prefix of the keys until a module leaves."""
    run.watch.sync()
    judged = run.watch.judged
    saved = sys.modules.pop("leak_cost_fake_3")
    late = types.ModuleType("leak_cost_fake_late")
    late.__file__ = str(repo_root / "pkg" / "late.py")
    monkeypatch.setitem(sys.modules, late.__name__, late)
    sys.modules["leak_cost_fake_3"] = saved  # back at the end of the dict, behind the new module
    run.watch.sync()
    assert run.watch.judged == judged + 1  # the new name only: the one that came back was judged
    assert "leak_cost_fake_late" in set(run.watch._names)


def test_a_module_without_a_string_file_is_never_first_party(
    run: leak_guard._Run, repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A namespace package has `__file__ = None`, a built-in has none, `sys.modules` may hold a non-module."""
    odd = {
        "leak_cost_odd_none": _with_file(None),
        "leak_cost_odd_int": _with_file(5),
        "leak_cost_odd_path": _with_file(repo_root / "pkg" / "p.py"),  # a Path, not a str
        "leak_cost_odd_missing": types.ModuleType("leak_cost_odd_missing"),
    }
    for name, module in odd.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setitem(sys.modules, "leak_cost_odd_object", object())
    monkeypatch.setitem(sys.modules, "leak_cost_odd_none_entry", None)
    run.watch.sync()
    assert run.watch.tracked == _FAKE_MODULES


def _with_file(file: object) -> types.ModuleType:
    module = types.ModuleType("odd")
    module.__file__ = file  # type: ignore[assignment]
    return module
