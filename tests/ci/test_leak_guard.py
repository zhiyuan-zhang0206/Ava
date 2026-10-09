"""The root leak guard (`tests/fixtures/leak_guard.py`): it names the test that leaks, and nothing else.

A synthetic suite (`leak_guard_suite.py`) goes through real pytest in a scratch rootdir, once per
mode and runner, and the guard is asserted from its observable outputs: the terminal section and
the JUnit properties. The suite holds the leak classes seen in this repository, each with a victim
that only fails because of it, and clean write-ups of the idioms the repository uses, which must
stay silent.

Also locked here: `fail` is the default mode, the guard is the first function-scoped autouse
fixture of the real root stack (the design rests on teardown running in reverse), a fault of the
guard never changes a test result (the one exception is a fault of the snapshot or the comparison
in fail mode, which is loud), and the JUnit properties are exactly what `scripts/ci/shard_counts.py`
reads.
"""

from __future__ import annotations

import ast
import importlib
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

import ava
from ava.sdk_surface import agent_identity
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from scripts.ci import shard_counts
from scripts.ci.tests import leak_guard_suite

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GUARD = "tests.fixtures.leak_guard"
_RESTORE = "tests.fixtures.identity_restore"
# The plugins of the repo-root conftest that matter here, in its order.
_ROOT_STACK = (_GUARD, _RESTORE)
_TESTS = "leakdemo/tests"
_CLEAN_FILE = f"{_TESTS}/test_clean_patterns.py"
_LEAKS_FILE = f"{_TESTS}/test_known_leaks.py"

LEAKERS = {
    "test_leaker_setattr_getattr_served_name": "module-attr",
    "test_leaker_delenv_raising_false": "env",
    "test_leaker_install_namespace": "module-attr",
    "test_leaker_cwd": "cwd",
    "test_leaker_signal_handler": "signal",
}
NOTES = {"test_note_syspath_leak": "sys.path"}
# Not named by the guard: `identity_restore` undoes it, and without it this test's victim is red.
IDENTITY_LEAKER = "test_leaker_bare_identity_assignment"
IDENTITY_VICTIM = "test_victim_stale_identity"
VICTIMS = {
    "test_victim_identity_read",
    "test_victim_expects_unset_port",
    "test_victim_plain_namespace_name",
    IDENTITY_VICTIM,
}


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    junit: Path
    properties: dict[str, list[tuple[str, str]]]  # testcase name -> its (property, value) pairs
    outcomes: dict[str, str]  # testcase name -> passed | failed | error | skipped

    @property
    def named(self) -> dict[str, str]:
        """Test -> kind, for every LEAK / NOTE line of the guard's terminal section."""
        pattern = r"^\s+(?:LEAK|NOTE) \S+::(\S+) -- ([\w.\-]+):"
        return {m.group(1): m.group(2) for m in re.finditer(pattern, self.out, re.M)}

    def bad(self) -> set[str]:
        return {name for name, status in self.outcomes.items() if status in ("failed", "error")}

    def fault_properties(self) -> list[str]:
        return [
            v for pairs in self.properties.values() for p, v in pairs if p == "leak_guard_fault"
        ]


class Suite:
    """Real pytest runs over one synthetic suite, each run done once and remembered."""

    def __init__(self, root: Path) -> None:
        self.root = root
        leak_guard_suite.write_suite(root)
        self._runs: dict[tuple[object, ...], Run] = {}

    def run(
        self,
        *,
        mode: str | None = None,
        xdist: bool = False,
        fault: str = "",
        select: str = _TESTS,
        plugins: tuple[str, ...] = (_GUARD,),
    ) -> Run:
        key = (mode, xdist, fault, select, plugins)
        if key not in self._runs:
            self._runs[key] = _pytest(
                self.root, select, mode, xdist, fault, len(self._runs), plugins=plugins
            )
        return self._runs[key]


def _child_env(mode: str | None, fault: str) -> dict[str, str]:
    # The child must not inherit this run's pytest/coverage plumbing, nor an opinion about the guard.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(
            ("PYTEST_", "COV_CORE_", "COVERAGE_", "AVA_LEAK_GUARD", "GITHUB_ACTIONS")
        )
    }
    env["PYTHONPATH"] = str(
        _REPO_ROOT
    )  # the guard is a module of this repository, imported by name
    # Autoloading every installed pytest plugin costs ~1.8 s a run; the suite needs only these two.
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    if mode is not None:
        env["AVA_LEAK_GUARD"] = mode
    env["LEAK_SUITE_FAULT"] = fault
    return env


def _read_junit(junit: Path) -> tuple[dict[str, list[tuple[str, str]]], dict[str, str]]:
    """Per testcase name: its (property, value) pairs, and its outcome."""
    properties: dict[str, list[tuple[str, str]]] = {}
    outcomes: dict[str, str] = {}
    if not junit.exists():
        return properties, outcomes
    for case in ET.parse(junit).getroot().iter("testcase"):  # noqa: S314 - pytest's own report
        name = case.get("name") or ""
        props = case.findall("properties/property")
        if props:
            properties[name] = [(p.get("name") or "", p.get("value") or "") for p in props]
        status = {"error": "error", "failure": "failed", "skipped": "skipped"}
        outcomes[name] = next(
            (out for tag, out in status.items() if case.find(tag) is not None), "passed"
        )
    return properties, outcomes


def _pytest(
    root: Path,
    select: str,
    mode: str | None,
    xdist: bool,
    fault: str,
    n: int,
    *,
    plugins: Sequence[str] = (_GUARD,),
) -> Run:
    junit = root / f"junit-{n}.xml"
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    for plugin in plugins:  # preloaded = registered before everything else, in this order
        command += ["-p", plugin]
    command += ["-p", "pytest_asyncio.plugin", "-c", "pytest.ini"]
    command += [f"--junitxml={junit}", "-o", "junit_family=xunit1"]
    command += ["-p", "xdist.plugin", "-n", "2"] if xdist else []
    done = subprocess.run(  # noqa: S603 - our own interpreter, a synthetic suite
        [*command, select],
        cwd=root,
        env=_child_env(mode, fault),
        capture_output=True,
        text=True,
        check=False,
    )
    properties, outcomes = _read_junit(junit)
    return Run(done.returncode, done.stdout + done.stderr, junit, properties, outcomes)


@pytest.fixture(scope="module")
def suite(tmp_path_factory: pytest.TempPathFactory) -> Suite:
    return Suite(tmp_path_factory.mktemp("leak_suite"))


def _kinds(run: Run, name: str) -> list[str]:
    return [
        v.partition(": ")[0] for p, v in run.properties.get(name, []) if p != "leak_guard_fault"
    ]


# -- the root stack ------------------------------------------------------------------------------


def test_the_guard_is_the_second_root_plugin_right_after_env_bootstrap() -> None:
    tree = ast.parse((_REPO_ROOT / "conftest.py").read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", "") == "pytest_plugins" for t in node.targets)
    )
    plugins = ast.literal_eval(assignment.value)
    assert plugins[:2] == ["tests.fixtures.env_bootstrap", _GUARD]
    assert _RESTORE in plugins


def test_the_guard_sets_up_before_every_other_function_scoped_autouse_fixture(
    request: pytest.FixtureRequest,
) -> None:
    """Teardown runs in reverse: the first fixture set up compares last, after monkeypatch's undo."""
    names = request.fixturenames
    root_autouse = ("_clean_state", "_restore_plugin_registrations")
    others = [n for n in names if n in root_autouse or n.startswith("_guard_")]
    assert "_leak_guard" in names
    assert len(others) > len(root_autouse), f"the root autouse fixtures are missing: {others}"
    assert all(names.index("_leak_guard") < names.index(other) for other in others)


def test_agent_identity_holds_no_process_global_slot() -> None:
    """The identity is the bound context's, restored through its context variable. A slot added
    to the module would leak again, unnamed: the module annotates each process-global slot it
    holds, and it holds none."""
    assert importlib.import_module("ava.sdk_surface.agent_identity").__annotations__ == {}


def test_native_admission_does_not_rebind_the_sdk_identity() -> None:
    context = ava.context
    expected = agent_identity.agent_id()
    hosted = AvaContext(identity=AgentIdentity(43 if expected == 42 else 42, True))
    assert hosted.require_identity().agent_id != expected
    assert ava.context is context
    assert agent_identity.agent_id() == expected
    assert ava.context is context
    assert agent_identity.agent_id() == expected


# -- what it names -------------------------------------------------------------------------------


def test_without_the_guard_the_victims_are_the_red_tests(suite: Suite) -> None:
    off = suite.run(mode="off")
    assert off.bad() == VICTIMS  # the pain: the red test is not the guilty one
    assert not off.named
    assert not off.properties.keys() & (LEAKERS.keys() | NOTES.keys())


def test_fail_is_the_default(suite: Suite) -> None:
    default, fail = suite.run(), suite.run(mode="fail")  # AVA_LEAK_GUARD unset, and set to fail
    assert default.outcomes == fail.outcomes
    assert default.code == fail.code != 0
    assert "leak guard (fail):" in default.out


def test_a_warn_run_names_exactly_the_leakers(suite: Suite) -> None:
    run = suite.run(mode="warn")
    assert run.named == {**LEAKERS, **NOTES}
    assert set(run.properties) == set(run.named), (
        "every finding is a property of that very testcase"
    )
    for name, kind in {**LEAKERS, **NOTES}.items():
        assert _kinds(run, name) == [kind]
    properties = {p for pairs in run.properties.values() for p, _ in pairs}
    assert properties == {"leak_guard", "leak_guard_note"}
    assert "FAULT" not in run.out


def test_the_clean_idioms_produce_no_finding(suite: Suite) -> None:
    run = suite.run()
    clean = {
        n for n in run.outcomes if not n.startswith(("test_leaker", "test_victim", "test_note"))
    }
    assert len(clean) >= 30
    assert not clean & (set(run.named) | set(run.properties))


def test_the_summary_says_what_locating_the_modules_cost(suite: Suite) -> None:
    """The one-time pass over `sys.modules` is a share of the cost a run reports: say how big it was."""
    head = (
        r"\d+ test\(s\) checked, \d+ us/test \(\d+ us/test of it module scans, \d+ ms locating "
        r"\d+ module\(s\) in \d+ pass\(es\)\), \d+ slow scan\(s\), \d+ module dicts watched"
    )
    assert re.search(head, suite.run().out)


def test_warn_changes_no_test_outcome(suite: Suite) -> None:
    """Warn neither fails a leaker nor puts state back: the victims stay red, as without the guard."""
    off, warn = suite.run(mode="off"), suite.run(mode="warn")
    assert warn.outcomes == off.outcomes
    assert warn.code == off.code
    assert warn.bad() == VICTIMS


def test_the_guard_never_prints_a_value(suite: Suite) -> None:
    run = suite.run()
    assert "8123" not in run.out  # the env value the leaker stored
    assert "8123" not in run.junit.read_text(encoding="utf-8")
    assert "DEMO_HEALTH_PORT was added" in run.out


def test_under_xdist_the_controller_names_the_same_leakers(suite: Suite) -> None:
    run = suite.run(mode="warn", xdist=True)
    assert run.named == {**LEAKERS, **NOTES}
    assert set(run.properties) == set(run.named)
    assert run.bad() <= VICTIMS  # a victim is red only when it shares a worker with its leaker


def test_an_unknown_mode_stops_the_run(suite: Suite) -> None:
    run = suite.run(mode="loud", select=_CLEAN_FILE)
    assert run.code != 0
    assert "AVA_LEAK_GUARD='loud': expected one of off, warn, fail" in run.out


# -- the identity restore ------------------------------------------------------------------------


def test_the_real_root_stack_runs_the_identity_restore(request: pytest.FixtureRequest) -> None:
    assert "_restore_agent_identity" in request.fixturenames


def test_the_guard_does_not_compare_the_agent_identity(suite: Suite) -> None:
    """Bare identity assignment is the convention: it is undone, not reported."""
    run = suite.run()
    assert IDENTITY_LEAKER in run.outcomes
    assert IDENTITY_LEAKER not in run.named
    assert IDENTITY_LEAKER not in run.properties


def test_the_identity_restore_puts_the_slot_back_for_the_next_test(suite: Suite) -> None:
    """Red without it (`test_without_the_guard_the_victims_are_the_red_tests`), green with it."""
    run = suite.run(mode="off", plugins=(_RESTORE,))
    assert run.bad() == VICTIMS - {IDENTITY_VICTIM}


# -- fail mode: the later switch -----------------------------------------------------------------


@pytest.mark.parametrize("xdist", [False, True])
def test_fail_mode_reds_only_the_leakers_at_their_own_teardown(suite: Suite, xdist: bool) -> None:
    run = suite.run(mode="fail", xdist=xdist, plugins=_ROOT_STACK)
    assert run.bad() == set(LEAKERS)
    assert {run.outcomes[name] for name in LEAKERS} == {"error"}
    assert not run.bad() & VICTIMS, "the state was put back before the victims ran"
    assert "leaked process-global state past its own teardown" in run.out
    assert "fix (env): monkeypatch.delenv(name, raising=False) records nothing" in run.out


@pytest.mark.parametrize("fault", ["snapshot", "compare"])
def test_fail_mode_is_loud_about_a_guard_that_cannot_take_a_verdict(
    suite: Suite, fault: str
) -> None:
    """A snapshot or a comparison that raises, which warn contains, stops every test of a fail-mode run:
    without either there is no verdict, and the gate would pass silently."""
    run = suite.run(mode="fail", fault=fault, select=_CLEAN_FILE)
    assert run.code != 0
    assert "passed" not in set(run.outcomes.values())
    assert "RuntimeError" in run.out


# -- the order is load-bearing -------------------------------------------------------------------


def test_registered_first_the_guard_blames_the_leaker_and_only_the_leaker(tmp_path: Path) -> None:
    leak_guard_suite.write_order_suite(tmp_path, [_GUARD, "order_other"])
    run = _pytest(tmp_path, _TESTS, None, False, "", 0, plugins=())
    assert run.named == {"test_leaker_setattr_getattr_served_name": "module-attr"}
    assert (
        "leakdemo.selfmod.AGENT_ID (NoneType) was added" in run.out
    )  # what monkeypatch's undo stored


def test_registered_after_an_autouse_monkeypatch_plugin_the_guard_compares_too_early(
    tmp_path: Path,
) -> None:
    """Registered last, it compares before the other plugin's undo: a clean test is blamed."""
    leak_guard_suite.write_order_suite(tmp_path, ["order_other", _GUARD])
    run = _pytest(tmp_path, _TESTS, None, False, "", 0, plugins=())
    assert run.named["test_clean_monkeypatch_setenv_in_the_body"] == "env"  # the loud symptom
    assert "leakdemo.selfmod.AGENT_ID (int) was added" in run.out  # still patched, not yet undone


# -- a fault of the guard never changes a result (fail mode: but for the snapshot and the comparison) --


@pytest.mark.parametrize(
    ("fault", "error", "properties"),
    [
        ("snapshot", "RuntimeError", ["snapshot"]),
        ("compare", "RuntimeError", ["compare"]),
        ("report", "RuntimeError", []),  # the property write itself fails: nothing to write it with
        (
            "collect",
            "RuntimeError",
            [],
        ),  # the controller cannot record a finding: no test is at hand
    ],
)
def test_in_warn_mode_a_fault_of_the_guard_is_one_report_line_and_changes_no_result(
    suite: Suite, fault: str, error: str, properties: list[str]
) -> None:
    off, broken = suite.run(mode="off"), suite.run(mode="warn", fault=fault)
    assert broken.outcomes == off.outcomes
    assert broken.code == off.code
    assert "INTERNALERROR" not in broken.out
    assert re.search(rf"^  FAULT {fault} x\d+: {error}", broken.out, re.M)
    assert [v.partition(":")[0] for v in broken.fault_properties()] == properties


def test_in_warn_mode_a_fault_of_the_summary_is_one_line_and_changes_no_result(
    suite: Suite,
) -> None:
    off, broken = suite.run(mode="off"), suite.run(mode="warn", fault="summary")
    assert broken.outcomes == off.outcomes
    assert broken.code == off.code
    assert "INTERNALERROR" not in broken.out
    assert "leak guard (warn): the summary failed: RuntimeError: injected fault" in broken.out


@pytest.mark.parametrize("fault", ["report", "collect", "summary"])
def test_in_fail_mode_a_fault_of_the_reporting_is_not_a_result(suite: Suite, fault: str) -> None:
    """Writing the property, handing findings to the controller and the summary cannot hide a leak from
    the gate (the teardown error is the gate), so a fault there never fails a test or crashes the run."""
    clean, broken = suite.run(mode="fail"), suite.run(mode="fail", fault=fault)
    assert broken.outcomes == clean.outcomes
    assert broken.code == clean.code
    assert "INTERNALERROR" not in broken.out
    if fault == "summary":
        assert "leak guard (fail): the summary failed: RuntimeError: injected fault" in broken.out
    else:
        assert re.search(rf"^  FAULT {fault} x\d+: RuntimeError", broken.out, re.M)


def test_a_clean_session_with_a_broken_guard_still_passes_in_warn_mode(suite: Suite) -> None:
    run = suite.run(mode="warn", fault="snapshot", select=_CLEAN_FILE)
    assert run.code == 0
    assert run.bad() == set()
    assert "FAULT snapshot" in run.out


# -- the report the aggregator reads ---------------------------------------------------------------


def test_the_properties_the_guard_writes_are_what_shard_counts_reads(suite: Suite) -> None:
    counted = shard_counts.count_junit(suite.run().junit)
    assert {x["test"].rpartition("::")[2]: x["kind"] for x in counted["leaks"]} == LEAKERS
    assert [x["kind"] for x in counted["notes"]] == ["sys.path"]
    assert shard_counts.bucket_of(counted["leaks"][0]["test"].partition("::")[0]) == _TESTS
    assert "faults" not in counted
    broken = shard_counts.count_junit(
        suite.run(mode="warn", fault="snapshot", select=_LEAKS_FILE).junit
    )
    assert [x["kind"] for x in broken["faults"]] == ["snapshot"]
