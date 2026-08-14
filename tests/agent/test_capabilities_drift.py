"""The `# Capabilities` index is a snapshot; the skill catalog under it is not.

`init_context` renders the index into the SystemMessage once per context window,
while `ava.skills._names()` re-scans the filesystem on every call. These pin the
mechanism that keeps the two from drifting apart without waiting for a
compaction: the membership snapshot recorded at build time, the diff against it,
and the one note that names what appeared.

Skills are faked by pointing `ava.skills._skills_dir` at a tmpdir, same shape as
tests/agent/test_capabilities_index.py.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime

import ava.skills as skills_mod
from agent.graph._capabilities import (
    _NEW_SKILLS_MAX_ENTRIES,
    index_drift,
    indexed_skill_identifiers,
)
from agent.graph._context import AvaContext
from agent.hooks._registry import HOOKS
from agent.hooks.capabilities import _newly_installed_skills, register_capabilities_hooks
from agent.state import AgentState, CapabilitiesState
from shared.config import settings
from shared.message_kwargs import NoteTag

_CONFIG = {"configurable": {"thread_id": "1042"}}


def _runtime(*, container: bool = False) -> Runtime[AvaContext]:
    """The hook reads only `ops_pool` off the runtime: `None` is the
    container/eval signal, anything else is a real agent."""
    return Runtime(
        context=AvaContext(
            ops_pool=None if container else MagicMock(),
            llm=MagicMock(),
            event_publisher=MagicMock(),
        )
    )


@pytest.fixture(autouse=True)
def _fresh_attribution_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_recorded_skill_invocations` is per-agent-RUN state in a module global,
    so it leaks between tests — see tests/agent/test_capabilities_index.py."""
    monkeypatch.setattr(skills_mod, "_recorded_skill_invocations", set())  # pyright: ignore[reportUnknownArgumentType]


@pytest.fixture
def skills_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "skills"
    d.mkdir()
    monkeypatch.setattr(skills_mod, "_skills_dir", lambda: d)
    monkeypatch.setattr(
        skills_mod,
        "enabled_skill_names",
        lambda: {p.name for p in d.iterdir() if p.is_dir()},
    )
    monkeypatch.setattr(settings.agent, "skills_to_inject_into_system_prompt", ["*"])
    return d


def _install(root: Path, name: str, desc: str) -> None:
    """Drop a skill onto disk the way an install does — mid-session, with no
    notification to any running agent."""
    (root / name).mkdir(parents=True)
    (root / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\nBODY\n", encoding="utf-8"
    )


def _state(indexed: set[str] | None) -> AgentState:
    return AgentState(capabilities=CapabilitiesState(indexed=indexed))


async def _run_hook(indexed: set[str] | None) -> dict | None:
    return await _newly_installed_skills(_state(indexed), _runtime(), _CONFIG)  # type: ignore[arg-type]


# ── the diff ──


def test_drift_reports_only_what_appeared_since_the_snapshot(skills_dir: Path) -> None:
    """The whole point: a skill installed after the index was rendered shows up
    as an addition, and one that was already indexed does not show up again."""
    _install(skills_dir, "alpha", "Alpha desc")
    snapshot = indexed_skill_identifiers()
    assert snapshot == {"alpha"}

    _install(skills_dir, "beta", "Beta desc")

    drift = index_drift(snapshot)
    assert [s["name"] for s in drift.added] == ["beta"]
    assert drift.identifiers == {"alpha", "beta"}


def test_drift_is_empty_when_the_catalog_has_not_moved(skills_dir: Path) -> None:
    _install(skills_dir, "alpha", "Alpha desc")
    drift = index_drift(indexed_skill_identifiers())
    assert drift.added == []


def test_uninstall_leaves_the_snapshot_so_a_reinstall_announces_again(
    skills_dir: Path,
) -> None:
    """Only additions are reported, but membership is replaced rather than
    unioned — otherwise a skill removed and put back would stay silent."""
    _install(skills_dir, "alpha", "Alpha desc")
    snapshot = indexed_skill_identifiers()

    shutil.rmtree(skills_dir / "alpha")
    after_removal = index_drift(snapshot)
    assert after_removal.added == []
    assert after_removal.identifiers == set()

    _install(skills_dir, "alpha", "Alpha desc")
    assert [s["name"] for s in index_drift(after_removal.identifiers).added] == ["alpha"]


# ── the note ──


async def test_hook_names_the_new_skill_in_the_index_line_shape(skills_dir: Path) -> None:
    """The note has to read as more of the `# Capabilities` listing, so it uses
    the same `- \\`ava.skills.<path>\\` — description` line the index does."""
    _install(skills_dir, "alpha", "Alpha desc")
    snapshot = indexed_skill_identifiers()
    _install(skills_dir, "beta", "Beta desc")

    update = await _run_hook(snapshot)

    assert update is not None
    (note,) = update["messages"]
    assert note.additional_kwargs["ava_note_tag"] == NoteTag.NEW_SKILLS.value  # pyright: ignore[reportUnknownMemberType]
    assert "- `ava.skills.beta` — Beta desc" in note.content  # pyright: ignore[reportUnknownMemberType]
    assert "alpha" not in note.content  # pyright: ignore[reportUnknownMemberType]


async def test_hook_advances_the_snapshot_so_one_install_is_named_once(
    skills_dir: Path,
) -> None:
    _install(skills_dir, "alpha", "Alpha desc")
    snapshot = indexed_skill_identifiers()
    _install(skills_dir, "beta", "Beta desc")

    update = await _run_hook(snapshot)
    assert update is not None
    advanced = update["capabilities"].indexed  # pyright: ignore[reportUnknownMemberType]
    assert advanced == {"alpha", "beta"}

    assert await _run_hook(advanced) is None  # pyright: ignore[reportUnknownArgumentType]


async def test_hook_is_silent_when_nothing_was_installed(skills_dir: Path) -> None:
    _install(skills_dir, "alpha", "Alpha desc")
    assert await _run_hook(indexed_skill_identifiers()) is None


async def test_no_snapshot_adopts_the_live_catalog_without_announcing_it(
    skills_dir: Path,
) -> None:
    """A checkpoint written before the snapshot field existed. What that agent's
    standing SystemMessage lists is unknowable here, so the whole catalog must
    not be announced as newly installed."""
    _install(skills_dir, "alpha", "Alpha desc")
    _install(skills_dir, "beta", "Beta desc")

    update = await _run_hook(None)

    assert update is not None
    assert "messages" not in update
    assert update["capabilities"].indexed == {"alpha", "beta"}  # pyright: ignore[reportUnknownMemberType]


async def test_a_bulk_install_is_capped_with_a_counted_tail(skills_dir: Path) -> None:
    """One drift event can carry dozens — a first converge on a fresh box, a
    plugin sync landing a whole pack. The note must not turn into a second full
    index, so it lists a bounded prefix and points at the catalog for the rest."""
    extra = 5
    for i in range(_NEW_SKILLS_MAX_ENTRIES + extra):
        _install(skills_dir, f"skill-{i:02d}", f"Desc {i}")

    update = await _run_hook(set())

    assert update is not None
    (note,) = update["messages"]
    lines = [ln for ln in note.content.splitlines() if ln.startswith("- ")]  # pyright: ignore[reportUnknownMemberType]
    assert (
        len(lines) == _NEW_SKILLS_MAX_ENTRIES + 1  # pyright: ignore[reportUnknownArgumentType]
    )  # the cap, plus the tail  # pyright: ignore[reportUnknownArgumentType]
    assert lines[-1].startswith(f"- … and {extra} more")  # pyright: ignore[reportUnknownMemberType]
    assert "ava.help(ava.skills)" in lines[-1]
    # The snapshot still covers every one of them, so the ones the note left out
    # are never named later — the tail's pointer is what covers them.
    assert len(update["capabilities"].indexed) == _NEW_SKILLS_MAX_ENTRIES + extra  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


# ── the two turns that write nothing ──


def _pin_compact_ceiling(monkeypatch: pytest.MonkeyPatch, *, hard_tokens: int) -> None:
    """Pin the force-compact ceiling regardless of model. These messages carry no
    usage_metadata, so occupancy is the chars/4 fallback and `hard_tokens` is the
    absolute threshold `auto_compact_will_fire` compares against."""
    from shared.lm.context_budget import ContextBudget

    monkeypatch.setattr(
        "agent.hooks.compact.resolve_context_budget",
        lambda _model: ContextBudget(  # pyright: ignore[reportUnknownArgumentType]
            max_context_tokens=1_000_000,
            soft_compact_tokens=hard_tokens,
            hard_compact_tokens=hard_tokens,
        ),
    )


async def test_hook_defers_when_a_compaction_will_replace_the_window(
    skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real drift, but a compaction fires the same before_llm pass — so nothing
    may be written at all.

    `add_messages` applies compaction's `REMOVE_ALL` and THEN the append, so a
    note written here is the sole survivor of the wipe, and `init_context` (whose
    only trigger is an empty `messages`) would read that one note as an intact
    history and drop the parked summary. tests/agent/test_init_context.py drives
    that composition end to end; this pins the predicate the defer is gated on.
    """
    _install(skills_dir, "alpha", "Alpha desc")
    snapshot = indexed_skill_identifiers()
    _install(skills_dir, "beta", "Beta desc")
    over_the_ceiling: list[AnyMessage] = [
        SystemMessage(content="<sys>"),
        *(HumanMessage(content="x" * 1000, id=f"h{i}") for i in range(5)),
    ]
    state = AgentState(messages=over_the_ceiling, capabilities=CapabilitiesState(indexed=snapshot))

    _pin_compact_ceiling(monkeypatch, hard_tokens=1)
    assert await _newly_installed_skills(state, _runtime(), _CONFIG) is None  # type: ignore[arg-type]

    # Deferring is not silencing: the same state with no compaction predicted
    # still names beta, so the guard is the only thing suppressing it.
    _pin_compact_ceiling(monkeypatch, hard_tokens=10_000_000)
    update = await _newly_installed_skills(state, _runtime(), _CONFIG)  # type: ignore[arg-type]
    assert update is not None
    assert "ava.skills.beta" in update["messages"][0].content  # pyright: ignore[reportUnknownMemberType]


async def test_container_mode_writes_nothing(skills_dir: Path) -> None:
    """The eval harness has no ops_pool — the same signal `init_context` reduces
    its head by. An eval's context is deterministic by construction, and this
    note's trigger is whatever the host filesystem gained mid-run."""
    _install(skills_dir, "alpha", "Alpha desc")
    snapshot = indexed_skill_identifiers()
    _install(skills_dir, "beta", "Beta desc")

    state = _state(snapshot)
    assert await _newly_installed_skills(state, _runtime(container=True), _CONFIG) is None  # type: ignore[arg-type]


# ── narrowed agents ──


async def test_narrowing_holds_and_a_configured_name_that_arrives_late_drifts_in(
    skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent narrowed to a specific list gets the same diff with no special
    case: a name that resolved to nothing at build time and resolves now is
    drift, and a skill outside the list never becomes drift."""
    monkeypatch.setattr(settings.agent, "skills_to_inject_into_system_prompt", ["beta"])
    _install(skills_dir, "alpha", "Alpha desc")
    snapshot = indexed_skill_identifiers()
    assert snapshot == set()  # `beta` is configured but not installed yet

    _install(skills_dir, "beta", "Beta desc")
    _install(skills_dir, "gamma", "Gamma desc")

    update = await _run_hook(snapshot)

    assert update is not None
    (note,) = update["messages"]
    assert "ava.skills.beta" in note.content  # pyright: ignore[reportUnknownMemberType]
    assert "gamma" not in note.content  # pyright: ignore[reportUnknownMemberType]
    assert update["capabilities"].indexed == {"beta"}  # pyright: ignore[reportUnknownMemberType]


async def test_sdk_disabled_skills_never_drift(
    skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`AVA_SDK_DISABLE=skills` removed the surface on purpose; an install must
    not put it back through the note."""
    monkeypatch.setattr(settings.agent, "sdk_disable", ["skills"])
    _install(skills_dir, "alpha", "Alpha desc")
    assert await _run_hook(set()) is None


async def test_a_stale_config_name_warns_once_not_every_turn(
    skills_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Resolution stopped being a per-window event when the drift check started
    re-resolving before every LLM call. An unresolved configured name is a fact
    about static config, so repeating its warning for the agent's whole life is
    noise, not information."""
    monkeypatch.setattr(settings.agent, "skills_to_inject_into_system_prompt", ["does-not-exist"])
    _install(skills_dir, "alpha", "Alpha desc")

    with caplog.at_level("WARNING"):
        for _ in range(3):
            await _run_hook(set())

    assert caplog.text.count("does-not-exist") == 1
    assert "skills_to_inject_into_system_prompt" in caplog.text


# ── registration ──


def test_register_capabilities_hooks_registers_before_llm() -> None:
    """Framework-owned and unconditional — the index under-reporting is a
    correctness problem, not a plugin's optional layer."""
    before = list(HOOKS["before_llm"])
    try:
        register_capabilities_hooks()
        assert HOOKS["before_llm"][-1] is _newly_installed_skills
    finally:
        HOOKS["before_llm"][:] = before
