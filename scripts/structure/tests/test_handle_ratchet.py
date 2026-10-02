"""The library-layer handle ratchet (scripts/structure/ambient_state/handle_ratchet.py): counts only fall."""

from __future__ import annotations

import ast
import textwrap

from scripts.structure.ambient_state import busrule, dbhandle
from scripts.structure.ambient_state import handle_ratchet as ratchet


def _sites(**where: list[str]) -> ratchet.Sites:
    return {tuple(key.split("__", 1)): lines for key, lines in where.items()}  # type: ignore[misc]


def test_a_dial_is_counted_whichever_package_it_is_in() -> None:
    tree = ast.parse(
        textwrap.dedent(
            """
            from base.db import Database, connect

            def f():
                connect()
                return Database.from_settings()
            """
        )
    )
    names = sorted(hit.name for hit in dbhandle.dials(tree))
    assert names == ["Database.from_settings", "connect"]
    assert [ratchet.kind_of(n) for n in names] == [ratchet.SELF_BUILT, ratchet.SHIM]


def test_a_self_built_bus_is_counted_whichever_package_it_is_in() -> None:
    tree = ast.parse(
        "from base.events.live.bus import EventBus\n\nbus = EventBus.from_settings()\n"
    )
    assert [hit.line for hit in busrule.builds(tree)] == [3]


def test_package_is_the_first_two_components() -> None:
    assert ratchet.package_of("base/agents/x/y.py") == "base/agents"
    assert ratchet.package_of("base/agents/x.py") == "base/agents"
    assert ratchet.package_of("ops/x.py") == "ops"


def test_a_package_above_its_frozen_count_fails_with_its_sites() -> None:
    sites = _sites(**{"base/agents__shim": ["base/agents/a.py:3", "base/agents/b.py:9"]})
    frozen = {"base/agents": {"shim": 1}}
    problems = ratchet.errors(sites, frozen, None)
    assert [p.split(":")[0] for p in problems] == ["base/agents/a.py", "base/agents/b.py"]


def test_a_package_below_its_frozen_count_asks_for_the_baseline_to_be_lowered() -> None:
    problems = ratchet.errors({}, {"base/agents": {"shim": 2}}, None)
    assert len(problems) == 1
    assert "lower the baseline" in problems[0]


def test_an_equal_count_passes() -> None:
    sites = _sites(**{"base/agents__shim": ["base/agents/a.py:3"]})
    assert ratchet.errors(sites, {"base/agents": {"shim": 1}}, {"base/agents": {"shim": 1}}) == []


def test_a_baseline_may_not_rise_above_the_base_revision() -> None:
    sites = _sites(**{"base/agents__shim": ["base/agents/a.py:3", "base/agents/b.py:9"]})
    frozen = {"base/agents": {"shim": 2}}
    problems = ratchet.errors(sites, frozen, {"base/agents": {"shim": 1}})
    assert len(problems) == 1
    assert "only shrinks" in problems[0]


def test_a_turn_settings_read_is_counted_but_not_its_import() -> None:
    tree = ast.parse(
        "from base.config.turn_view import turn_settings\n\nmodel = turn_settings.lm.llm_model\n"
    )
    assert ratchet.turn_reads(tree) == [3]


def test_a_turn_settings_read_above_its_frozen_count_fails_with_the_slice_hint() -> None:
    sites = _sites(**{"agent/graph__turn-settings-read": ["agent/graph/a.py:3"]})
    (problem,) = ratchet.errors(sites, {}, None)
    assert problem.startswith("agent/graph/a.py:3:") and "AgentSlices" in problem


def test_the_revision_introducing_a_kind_may_freeze_its_first_counts() -> None:
    frozen = {"agent/graph": {"turn-settings-read": 2}}
    sites = _sites(**{"agent/graph__turn-settings-read": ["a.py:1", "a.py:2"]})
    assert ratchet.errors(sites, frozen, {"base/agents": {"shim": 1}}) == []
    assert ratchet.errors(sites, frozen, {"agent/graph": {"turn-settings-read": 1}}) != []
