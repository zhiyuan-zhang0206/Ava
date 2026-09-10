"""Inherited memory — the `inheritable` block parser and the chain-read note.

The chain is faked at the client seam (`ava._gateway_client.get_born_chain`)
and entry files are written into the test home's workspaces, so the note
builder runs end to end minus the network and the DB. What the real claim node
does with the note (strip on fork + regraft) is pinned in
`tests/agent/test_fork_notes.py`.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from ava import _gateway_client
from ava_builtins.plugins.ava_memory import inherit
from shared.agents import GatewayUnavailable
from shared.config import settings
from shared.machine import machine_name
from shared.paths import ava_home

OPEN = inherit.INHERITABLE_OPEN
CLOSE = inherit.INHERITABLE_CLOSE


def _wrap(*blocks: str) -> str:
    return "\n\n".join(f"{OPEN}\n{block}\n{CLOSE}" for block in blocks)


def _note_text(note: HumanMessage) -> str:
    """The note's text — a system note is always plain text."""
    content = note.content  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(content, str)
    return content


def _note_tag(note: HumanMessage) -> object:
    return note.additional_kwargs.get("ava_note_tag")  # pyright: ignore[reportUnknownMemberType]


def _write_entry(agent_id: int, slug: str, body: str) -> Path:
    mem = ava_home() / "workspaces" / str(agent_id) / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    path = mem / f"{slug}.md"
    path.write_text(body, encoding="utf-8")
    return path


def _local_row(agent_id: int, label: str | None = None, depth: int = 1) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "label": label,
        "status": "idling",
        "machine": machine_name(),
        "depth": depth,
    }


def _remote_row(agent_id: int, depth: int = 1) -> dict[str, Any]:
    row = _local_row(agent_id, depth=depth)
    row["machine"] = "test-elsewhere"
    return row


class _FakeChain:
    """The born-chain read seam: rows to return, calls recorded."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.calls: list[int] = []
        self.error: Exception | None = None

    def __call__(self, agent_id: int) -> list[dict[str, Any]]:
        self.calls.append(agent_id)
        if self.error is not None:
            raise self.error
        return [dict(row) for row in self.rows]


@pytest.fixture(autouse=True)
def _fresh_chain_cache() -> Iterator[None]:
    """The builder caches the immutable chain per process; each test starts
    empty, so call counts reflect that test's calls only."""
    inherit._CHAIN_CACHE.clear()
    yield
    inherit._CHAIN_CACHE.clear()


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch) -> _FakeChain:
    fake = _FakeChain()
    monkeypatch.setattr(_gateway_client, "get_born_chain", fake)
    return fake


@pytest.fixture
def memory_plugin() -> Iterator[Any]:
    """Load ava_memory through the real plugin-registration path (mirrors
    tests/agent/test_fork_notes.py) so `fork_notes` runs against the
    registered note set."""
    from agent.state import clear_plugin_registrations
    from shared.plugin_config_registry import bind_from_disk
    from shared.plugin_context import PluginContext

    clear_plugin_registrations()
    for name in list(sys.modules):
        if name.startswith("ava_builtins.plugins.ava_memory"):
            del sys.modules[name]

    with PluginContext("ava_memory"):
        from ava_builtins.plugins.ava_memory import plugin as _plugin

    bind_from_disk()
    yield _plugin

    clear_plugin_registrations()
    for name in list(sys.modules):
        if name.startswith("ava_builtins.plugins.ava_memory"):
            del sys.modules[name]


# ── the block parser ───────────────────────────────────────────────────


def test_parser_reads_a_single_block() -> None:
    text = f"intro\n{OPEN}\nthe rule\n{CLOSE}\noutro"
    assert inherit.parse_inheritable_blocks(text) == ["the rule"]


def test_parser_reads_multiple_blocks_in_order() -> None:
    assert inherit.parse_inheritable_blocks(_wrap("A", "B")) == ["A", "B"]


def test_parser_skips_frontmatter_and_takes_only_the_body() -> None:
    """A marker inside frontmatter never opens a block (#2128 coherence: the
    writer's own frontmatter split decides where the body starts)."""
    text = f"---\nname: x\ndescription: {OPEN}\nnot a block\n---\n\n{OPEN}\nreal\n{CLOSE}"
    assert inherit.parse_inheritable_blocks(text) == ["real"]


def test_parser_normalizes_crlf() -> None:
    """A Windows-written entry (text-mode CRLF) parses like an LF one —
    frontmatter included."""
    text = f"---\r\nname: x\r\n---\r\n\r\n{OPEN}\r\ncrlf block\r\n{CLOSE}"
    assert inherit.parse_inheritable_blocks(text) == ["crlf block"]


def test_parser_fails_soft_on_malformed_fences() -> None:
    """Malformed fences drop content with a warning — never a silent
    over-share and never a sweep to EOF."""
    assert inherit.parse_inheritable_blocks(f"{OPEN}\nsecret\nstill open") == []
    assert inherit.parse_inheritable_blocks(f"body\n{CLOSE}\nmore") == []
    # A nested open is ignored; its content stays inside the outer block.
    assert inherit.parse_inheritable_blocks(f"{OPEN}\nouter\n{OPEN}\ninner\n{CLOSE}") == [
        "outer\ninner"
    ]
    # Blank blocks declare nothing.
    assert inherit.parse_inheritable_blocks(f"{OPEN}\n{CLOSE}") == []


# ── the note builder ───────────────────────────────────────────────────


def test_disabled_and_empty_chain_yield_no_note(
    chain: _FakeChain, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.agent, "memory_inherit_depth", 0)
    assert inherit.inherited_memory_note() is None
    monkeypatch.setattr(settings.agent, "memory_inherit_depth", 1)
    monkeypatch.setattr(settings.agent, "eval_isolation", True)
    assert inherit.inherited_memory_note() is None
    # Nothing read while the layer is off.
    assert chain.calls == []

    monkeypatch.setattr(settings.agent, "eval_isolation", False)
    assert inherit.inherited_memory_note() is None  # empty chain
    chain.rows = [_local_row(600301)]
    assert inherit.inherited_memory_note() is None  # local ancestor, no store


def test_direct_parent_blocks_are_injected(chain: _FakeChain) -> None:
    _write_entry(600311, "family-rules", f"private\n\n{_wrap('Report in Chinese.')}\n")
    chain.rows = [_local_row(600311, label="Parent")]
    note = inherit.inherited_memory_note()
    assert note is not None
    assert _note_tag(note) == "inherited_memory"
    content = _note_text(note)
    assert content.startswith("[system] Inherited memory")
    assert "## ancestor #600311 (Parent) — memory/family-rules.md" in content
    assert "Report in Chinese." in content
    # Private content outside the fence never leaks.
    assert "private" not in content


def test_blocks_render_nearest_first_with_sorted_entries(
    chain: _FakeChain, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_entry(600321, "b-entry", _wrap("b block"))
    _write_entry(600321, "a-entry", _wrap("a block one", "a block two"))
    _write_entry(600322, "grandparent", _wrap("grandparent block"))
    chain.rows = [_local_row(600321, label="Parent"), _local_row(600322, depth=2)]
    monkeypatch.setattr(settings.agent, "memory_inherit_depth", 2)
    note = inherit.inherited_memory_note()
    assert note is not None
    content = _note_text(note)
    # Nearest ancestor first; within one ancestor, sorted file names; both
    # fences of one file joined into one section.
    assert content.index("#600321") < content.index("#600322")
    assert content.index("memory/a-entry.md") < content.index("memory/b-entry.md")
    assert "a block one\n\na block two" in content
    assert "grandparent block" in content


def test_depth_slices_the_chain(chain: _FakeChain, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_entry(600331, "parent", _wrap("parent block"))
    _write_entry(600332, "grandparent", _wrap("grandparent block"))
    chain.rows = [_local_row(600331), _local_row(600332, depth=2)]
    monkeypatch.setattr(settings.agent, "memory_inherit_depth", 1)
    note = inherit.inherited_memory_note()
    assert note is not None
    content = _note_text(note)
    assert "(depth 1," in content
    assert "parent block" in content and "grandparent block" not in content

    monkeypatch.setattr(settings.agent, "memory_inherit_depth", 2)
    note = inherit.inherited_memory_note()
    assert note is not None
    content = _note_text(note)
    assert "parent block" in content and "grandparent block" in content


def test_remote_ancestors_are_skipped_and_footered(
    chain: _FakeChain, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_entry(600341, "local", _wrap("local block"))
    chain.rows = [_remote_row(600342), _local_row(600341, depth=2)]
    monkeypatch.setattr(settings.agent, "memory_inherit_depth", 2)
    note = inherit.inherited_memory_note()
    assert note is not None
    content = _note_text(note)
    assert "local block" in content
    assert (
        "Ancestors on other machines (not readable from here): #600342 (test-elsewhere)." in content
    )

    # Remote-only chain: no content, no note (the footer appears only when
    # there is something to carry it).
    inherit._CHAIN_CACHE.clear()
    chain.rows = [_remote_row(600343)]
    monkeypatch.setattr(settings.agent, "memory_inherit_depth", 1)
    assert inherit.inherited_memory_note() is None


def test_chain_read_failure_degrades_and_is_not_cached(chain: _FakeChain) -> None:
    chain.error = GatewayUnavailable("gateway down")
    assert inherit.inherited_memory_note() is None
    assert inherit.inherited_memory_note() is None
    # A failed read is not a negative cache — the next establishment retries.
    assert len(chain.calls) == 2


def test_chain_read_is_cached_per_process(chain: _FakeChain) -> None:
    _write_entry(600351, "rules", _wrap("cached block"))
    chain.rows = [_local_row(600351)]
    assert inherit.inherited_memory_note() is not None
    assert inherit.inherited_memory_note() is not None
    assert chain.calls == [1]  # the boot identity in tests; read once


def test_content_is_deterministic(chain: _FakeChain) -> None:
    """Unchanged state renders byte-identical — the property the fork's
    prefix-cache stability relies on (no timestamps in the content)."""
    _write_entry(600361, "rules", _wrap("stable block"))
    chain.rows = [_local_row(600361)]
    first = inherit.inherited_memory_note()
    inherit._CHAIN_CACHE.clear()
    second = inherit.inherited_memory_note()
    assert first is not None and second is not None
    assert _note_text(first) == _note_text(second)


def test_block_guardrail_truncates_with_a_visible_marker(
    chain: _FakeChain, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _write_entry(600371, "big", _wrap("x" * 200))
    chain.rows = [_local_row(600371)]
    monkeypatch.setattr(settings.agent, "memory_inherit_max_block_chars", 50)
    note = inherit.inherited_memory_note()
    assert note is not None
    content = _note_text(note)
    assert "[truncated at 50 chars — full entry: " in content
    assert str(entry) in content
    assert "x" * 51 not in content  # clipped at the cap


def test_total_guardrail_clips_and_omits(
    chain: _FakeChain, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_entry(600381, "one", _wrap("a" * 40))
    _write_entry(600381, "two", _wrap("b" * 40))
    chain.rows = [_local_row(600381)]
    monkeypatch.setattr(settings.agent, "memory_inherit_max_total_chars", 60)
    note = inherit.inherited_memory_note()
    assert note is not None
    content = _note_text(note)
    assert "a" * 40 in content  # first section enters whole
    assert "b" * 20 in content  # second clipped to the remaining budget
    assert "b" * 21 not in content
    assert "[truncated: the 60-char inherited-memory budget is reached" in content
    assert "further blocks omitted" in content

    # Exact exhaustion: nothing of the crossing block fits, so it is omitted
    # whole behind the same marker (no dangling section header).
    inherit._CHAIN_CACHE.clear()
    monkeypatch.setattr(settings.agent, "memory_inherit_max_total_chars", 40)
    monkeypatch.setattr(settings.agent, "memory_inherit_max_block_chars", 0)
    note = inherit.inherited_memory_note()
    assert note is not None
    content = _note_text(note)
    assert "a" * 40 in content
    assert "b" * 10 not in content  # block two omitted whole
    assert "memory/two.md" not in content  # no dangling section for it
    assert content.count("## ancestor") == 1
    assert "[truncated: the 40-char inherited-memory budget is reached" in content


def test_guardrails_can_be_disabled(chain: _FakeChain, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_entry(600391, "big", _wrap("y" * 200))
    chain.rows = [_local_row(600391)]
    monkeypatch.setattr(settings.agent, "memory_inherit_max_block_chars", 0)
    monkeypatch.setattr(settings.agent, "memory_inherit_max_total_chars", 0)
    note = inherit.inherited_memory_note()
    assert note is not None
    content = _note_text(note)
    assert "y" * 200 in content
    assert "truncated" not in content


def test_fork_notes_graft_the_new_agent_s_own_chain(chain: _FakeChain, memory_plugin: Any) -> None:
    """`fork_notes()` builds the on_fork notes in the NEW agent's process — the
    inherited note therefore carries the new agent's chain, not the source's
    (the claim node strips the source's copy; see test_fork_notes.py)."""
    _write_entry(600401, "rules", _wrap("fork chain block"))
    chain.rows = [_local_row(600401, label="Fork parent")]
    from agent.graph._context_notes import fork_notes

    notes = fork_notes()
    inherited = [n for n in notes if _note_tag(n) == "inherited_memory"]
    assert len(inherited) == 1
    assert "fork chain block" in _note_text(inherited[0])
