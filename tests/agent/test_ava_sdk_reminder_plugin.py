"""`plugins.ava_sdk_reminder` tests — the two hooks that surface a one-time hint
when the agent reaches for a native-Python equivalent of an SDK primitive.

Both hooks are graph-edge nodes: they read the message tail + their own plugin
fields straight off the `state` arg and return a delta dict (no exec-turn state
plumbing). So these tests build a dynamic AgentState instance with the plugin
fields and call the hook functions directly, mirroring the
`_auto_compact_with_version_bump` tests in test_compact.py.

Covered:
- after_exec (code categories shell/wait/files/http): first hit injects the
  hint as a separate system-note (leaving the exec-output message untouched) +
  marks; second hit no-ops; multi-category cell lists all in CATEGORIES order;
  a compaction re-arms; tail-shape / empty-code no-ops; the pure
  detect_categories matcher — literal masking (string/comment/f-string spans
  never trigger) and the content-only files trigger (listing/managing via
  stdlib + content via ava.files never triggers). A sleep cell that already names `watcher` marks
  the wait category seen WITHOUT emitting (the agent is using the watcher
  primitive itself) while any other matched category still hints; the pure
  mentions_watcher matcher.
- after_exec (assumed-persistence NameError): an undefined identifier hints
  only when its whole name appeared in an earlier execute_code cell; the
  current cell, builtins, keywords, disabled config, and repeated same-name
  failures stay silent.
- before_llm (agent_reply): first inbound from another agent injects a
  system-note + marks; second no-ops; a compaction re-arms; user/ui inbound
  no-ops; the note defers when auto-compact would fire the same turn (it would
  clobber / be clobbered by compaction's message replacement) and fires
  normally; the pure tail_has_agent_inbound
  matcher (incl. stop-at-prior-AIMessage boundary).
"""

import sys
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph._context import AvaContext
from agent.messages import inbound_message, tail_has_agent_inbound
from agent.state import CompactState, build_agent_state, clear_plugin_registrations
from ava_builtins.plugins.ava_sdk_reminder._state import (
    AGENT_REPLY_CATEGORY,
    CATEGORIES,
    detect_categories,
    hint_for,
    mentions_watcher,
)


def _pin_compact_budget(
    monkeypatch: pytest.MonkeyPatch, *, hard_tokens: int, soft_tokens: int = 600_000
) -> None:
    """Pin the auto-compact thresholds regardless of model, by replacing
    `resolve_context_budget` in the compact module. These tests use synthetic
    messages with no usage_metadata, so occupancy is the chars/4 fallback and
    `hard_tokens` is the absolute force-compact threshold the gate compares."""
    from shared.lm.context_budget import ContextBudget

    budget = ContextBudget(
        max_context_tokens=1_000_000,
        soft_compact_tokens=soft_tokens,
        hard_compact_tokens=hard_tokens,
    )
    monkeypatch.setattr("agent.hooks.compact.resolve_context_budget", lambda _model: budget)  # pyright: ignore[reportUnknownArgumentType]


@pytest.fixture
def _loaded() -> Iterator[Any]:
    """Load plugins.ava_sdk_reminder via the real plugin-registration path.
    Compact is now a core capability (Issue #1284) — its state fields live
    directly on BaseAgentState (nested compact/memory, etc.) and its config comes
    from shared.config.settings. No separate plugin module to load.
    Teardown clears registrations + unloads the module so the hooks do not
    leak into other tests.
    """
    from shared.plugin_config_registry import bind_from_disk
    from shared.plugin_context import PluginContext

    clear_plugin_registrations()
    for name in list(sys.modules):
        if name.startswith("ava_builtins.plugins.ava_sdk_reminder"):
            del sys.modules[name]

    # Register built-in compact hooks (mirrors build_graph).
    from agent.hooks.compact import register_compact_hooks

    register_compact_hooks()

    with PluginContext("ava_sdk_reminder"):
        from ava_builtins.plugins.ava_sdk_reminder import plugin as _plugin

    bind_from_disk()

    yield _plugin

    clear_plugin_registrations()
    for name in list(sys.modules):
        if name.startswith("ava_builtins.plugins.ava_sdk_reminder"):
            del sys.modules[name]


def _state(messages: list[AnyMessage], **fields: Any):
    return build_agent_state()(messages=messages, **fields)


def _runtime() -> Runtime[AvaContext]:
    # The hooks ignore runtime/config; a placeholder context satisfies the
    # AvaContext invariant.
    ctx = AvaContext(ops_pool=MagicMock(), llm=MagicMock(), event_publisher=MagicMock())
    return Runtime(context=ctx)


def _runtime_for_runner() -> Runtime[AvaContext]:
    """Runtime for tests that drive a real make_hook_runner — its node_lifecycle
    wrapper publishes a timeline snapshot through ops_pool, so a DB-shaped fake
    pool (not a bare MagicMock) is needed."""
    from tests.agent._fakes import make_fake_ops_pool

    ctx = AvaContext(ops_pool=make_fake_ops_pool(), llm=MagicMock(), event_publisher=MagicMock())
    return Runtime(context=ctx)


def _config() -> RunnableConfig:
    return {"configurable": {"thread_id": "1"}}


def _cell(code: str, output: str = "stdout text", *, id_suffix: str = "1") -> list[AnyMessage]:
    """A minimal post-exec message tail: an assistant execute_code call
    followed by its execution-output message."""
    tool_call_id = f"c{id_suffix}"
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "execute_code", "args": {"code": code}, "id": tool_call_id}],
    )
    out = ToolMessage(content=output, tool_call_id=tool_call_id, id=f"out-{id_suffix}")
    return [HumanMessage(content="do it", id=f"h{id_suffix}"), ai, out]


def _nameerror_output(name: str) -> str:
    return (
        'Traceback (most recent call last):\n  File "<ava-exec>", line 1, in <module>\n'
        f"NameError: name '{name}' is not defined"
    )


def _agent_inbound(content: str = "ping", source: str = "agent:7") -> AnyMessage:
    return inbound_message(content=content, source=source, inbound_id=1)


# ── the pure matcher ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "code,expected",
    [
        ("import subprocess; subprocess.run(['ls'])", ["shell"]),
        ("os.system('ls')", ["shell"]),
        ("os.popen('ls')", ["shell"]),
        ("time.sleep(5)", ["wait"]),
        ("sleep(5)", ["wait"]),
        ("ava.shell.run('ls')", []),  # SDK call -> no hint
        ("open('f.txt')", ["files"]),
        ("p.read_text()", ["files"]),
        ("path.write_bytes(b'x')", ["files"]),
        ("os.makedirs('d')", []),  # managing dirs is not a content bypass
        ("shutil.copy('a', 'b')", ["files"]),
        ("shutil.rmtree('d')", ["files"]),
        ("shutil.which('git')", []),  # not a content op
        ("glob.glob('*.py')", []),  # listing only
        ("os.listdir('d')", []),
        ("os.remove('f')", []),
        ("os.unlink('f')", []),
        # the user-reported false trigger: stdlib lists names, ava.files reads
        # content -> no hint (2026-08-26 ruling).
        ("import glob\nfor p in glob.glob('*.md'):\n    ava.files.read(p)", []),
        ("os.listdir('d')\nava.files.read('f')", []),
        # trigger words inside string/comment/f-string literals are masked
        # (they grep/print examples, they do not touch files):
        ("print(\"open('f')\")", []),
        ("# open('f')", []),
        ("s = 'glob.glob(\"*.py\")'", []),
        ("import re\nre.compile(r'open\\(')", []),
        ('f"open({x})"', []),
        ("s = 'subprocess.run([\"ls\"])'", []),
        ("s = 'time.sleep(1)'", []),
        ("s = 'requests.get(\"u\")'", []),
        # a real call beside a literal still fires; broken code falls back to
        # the raw scan:
        ("print('note')\nopen('f')", ["files"]),
        ("open('f'", ["files"]),
        ("requests.get('http://x')", ["http"]),
        ("httpx.get('http://x')", ["http"]),
        ("urllib.request.urlopen('http://x')", ["http"]),
        ("ava.files.read('f')", []),  # `.read(` is not files-triggered (needs read_text)
        ("x = 1 + 1", []),
    ],
)
def test_detect_categories(code: str, expected: list[str]):
    assert detect_categories(code) == expected


def test_detect_categories_multi_in_order():
    """A cell hitting multiple categories returns them in CATEGORIES order."""
    code = "import requests\nrequests.get('u')\nsubprocess.run(['ls'])\ntime.sleep(1)\nopen('f')"
    assert detect_categories(code) == ["shell", "wait", "files", "http"]


def test_categories_cover_all_hints():
    """Every category has a hint that points at an `ava` primitive via help()."""
    for cat in CATEGORIES:
        h = hint_for(cat)
        assert "ava" in h
        assert "help(" in h


@pytest.mark.parametrize(
    "code,expected",
    [
        ("time.sleep(1)", False),
        ("ava.watcher.at('5m', name='test')", True),
        ("# Watcher script\nsleep(2)", True),  # case-insensitive substring
        ("x = 1 + 1", False),
    ],
)
def test_mentions_watcher(code: str, expected: bool):
    assert mentions_watcher(code) is expected


# ── the after_exec hook (code categories) ──────────────────────────────────


async def test_first_hit_injects_note_and_marks(_loaded: Any):
    hook = _loaded.sdk_reminder_after_exec
    state = _state(_cell("subprocess.run(['ls'])"))
    result = await hook(state, _runtime(), _config())

    assert result is not None
    # hint injected as its own system-note; the exec-output message is untouched.
    [note] = result["messages"]
    assert note.additional_kwargs["ava_msg_type"] == "system_note"
    assert note.additional_kwargs["ava_note_tag"] == "sdk_hint"
    assert note.additional_kwargs["ava_created_at"]  # stamped with the inject time
    assert "ava.shell.run" in note.content
    # category recorded.
    assert result["ava_sdk_reminder__reminded"] == {"shell"}
    assert result["ava_sdk_reminder__last_seen_compact"] == 0


async def test_second_hit_same_category_no_append(_loaded: Any):
    hook = _loaded.sdk_reminder_after_exec
    state = _state(_cell("subprocess.run(['ls'])"), ava_sdk_reminder__reminded={"shell"})
    result = await hook(state, _runtime(), _config())
    assert result is None


async def test_code_every_time_cadence_hints_two_consecutive_matching_cells(
    _loaded: Any, monkeypatch: pytest.MonkeyPatch
):
    """`every_time` bypasses the reminded gate for code categories, so two
    consecutive shell cells each receive the shell hint."""
    from shared.config import settings

    monkeypatch.setattr(settings.agent, "sdk_code_reminder_cadence", "every_time")
    hook = _loaded.sdk_reminder_after_exec

    first = await hook(_state(_cell("subprocess.run(['first'])")), _runtime(), _config())
    assert first is not None
    second_state = _state(
        _cell("subprocess.run(['second'])"),
        ava_sdk_reminder__reminded=first["ava_sdk_reminder__reminded"],
        ava_sdk_reminder__last_seen_compact=first["ava_sdk_reminder__last_seen_compact"],
    )
    second = await hook(second_state, _runtime(), _config())

    assert second is not None
    for result in (first, second):
        [note] = result["messages"]
        assert "ava.shell.run" in note.content


async def test_code_once_cadence_hints_only_first_consecutive_matching_cell(
    _loaded: Any, monkeypatch: pytest.MonkeyPatch
):
    """`once_per_compaction` (the default) preserves the existing behavior:
    the first shell cell hints and the next shell cell in the window no-ops."""
    from shared.config import settings

    monkeypatch.setattr(settings.agent, "sdk_code_reminder_cadence", "once_per_compaction")
    hook = _loaded.sdk_reminder_after_exec

    first = await hook(_state(_cell("subprocess.run(['first'])")), _runtime(), _config())
    assert first is not None
    second_state = _state(
        _cell("subprocess.run(['second'])"),
        ava_sdk_reminder__reminded=first["ava_sdk_reminder__reminded"],
        ava_sdk_reminder__last_seen_compact=first["ava_sdk_reminder__last_seen_compact"],
    )

    assert await hook(second_state, _runtime(), _config()) is None


async def test_different_categories_each_fire_once(_loaded: Any):
    hook = _loaded.sdk_reminder_after_exec
    # shell already reminded; this cell hits shell + wait -> only wait is fresh.
    state = _state(
        _cell("subprocess.run(['ls'])\ntime.sleep(2)"),
        ava_sdk_reminder__reminded={"shell"},
    )
    result = await hook(state, _runtime(), _config())

    assert result is not None
    [note] = result["messages"]
    assert note.additional_kwargs["ava_msg_type"] == "system_note"
    assert "ava.watcher" in note.content
    assert "ava.shell.run" not in note.content  # shell already hinted
    assert result["ava_sdk_reminder__reminded"] == {"shell", "wait"}


async def test_wait_with_watcher_marked_silently_no_hint(_loaded: Any):
    """A cell that sleeps while already naming `watcher` is the agent working
    with the watcher primitive itself — the wait hint is suppressed but the
    category is marked seen (so it fires neither now nor later this window)."""
    hook = _loaded.sdk_reminder_after_exec
    state = _state(_cell("ava.watcher.at('5m', name='test')\ntime.sleep(1)"))
    result = await hook(state, _runtime(), _config())

    assert result is not None
    # marked seen, but no hint emitted -> no output-message replacement.
    assert "messages" not in result
    assert result["ava_sdk_reminder__reminded"] == {"wait"}
    assert result["ava_sdk_reminder__last_seen_compact"] == 0


async def test_wait_with_watcher_suppressed_other_category_still_hints(_loaded: Any):
    """When a watcher-naming sleep cell also trips another category, the wait
    hint is suppressed (but marked) while the other category still hints."""
    hook = _loaded.sdk_reminder_after_exec
    state = _state(_cell("subprocess.run(['ls'])\nava.watcher\ntime.sleep(1)"))
    result = await hook(state, _runtime(), _config())

    assert result is not None
    [note] = result["messages"]
    assert "ava.shell.run" in note.content
    assert "ava.watcher" not in note.content  # wait hint suppressed
    assert result["ava_sdk_reminder__reminded"] == {"shell", "wait"}


async def test_wait_with_watcher_already_marked_is_noop(_loaded: Any):
    """A second watcher-naming sleep cell, wait already marked -> no-op (the
    silent suppression does not re-fire or re-persist)."""
    hook = _loaded.sdk_reminder_after_exec
    state = _state(
        _cell("ava.watcher.at('5m', name='test')\ntime.sleep(1)"),
        ava_sdk_reminder__reminded={"wait"},
    )
    result = await hook(state, _runtime(), _config())
    assert result is None


async def test_compaction_rearms_silent_watcher_path(_loaded: Any):
    """The silent-suppress path is the only one that advances the bookmark
    WITHOUT emitting a message — pin that a re-arm still persists the bookmark
    advance and re-marks wait, with no hint message. Guards a refactor that
    moved the bookmark write under the hint-emit branch from stranding it."""
    hook = _loaded.sdk_reminder_after_exec
    state = _state(
        _cell("ava.watcher.at('5m', name='test')\ntime.sleep(1)"),
        ava_sdk_reminder__reminded={"wait"},
        ava_sdk_reminder__last_seen_compact=0,
        compact=CompactState(version=1),
    )
    result = await hook(state, _runtime(), _config())

    assert result is not None
    assert "messages" not in result  # silent path emits no hint
    assert result["ava_sdk_reminder__last_seen_compact"] == 1  # bookmark persisted
    assert result["ava_sdk_reminder__reminded"] == {"wait"}  # re-marked after re-arm


async def test_wait_suppressed_with_stale_other_category_marks_only(_loaded: Any):
    """A watcher+sleep cell that also trips an already-reminded category: nothing
    hints (the other category is stale) but wait is still newly marked — the
    `silent - reminded` branch where `hinted` is empty yet `newly_seen` is not."""
    hook = _loaded.sdk_reminder_after_exec
    state = _state(
        _cell("subprocess.run(['ls'])\nava.watcher\ntime.sleep(1)"),
        ava_sdk_reminder__reminded={"shell"},
    )
    result = await hook(state, _runtime(), _config())

    assert result is not None
    assert "messages" not in result  # shell stale, wait suppressed -> no hint
    assert result["ava_sdk_reminder__reminded"] == {"shell", "wait"}


async def test_watcher_named_without_sleep_does_not_suppress(_loaded: Any):
    """Naming `watcher` only suppresses when the cell also trips the wait
    trigger. A watcher mention beside a non-wait idiom still hints that idiom
    and does not mark wait (guards the `"wait" in matched` conjunct)."""
    hook = _loaded.sdk_reminder_after_exec
    state = _state(_cell("ava.watcher\nsubprocess.run(['ls'])"))
    result = await hook(state, _runtime(), _config())

    assert result is not None
    [note] = result["messages"]
    assert "ava.shell.run" in note.content
    assert result["ava_sdk_reminder__reminded"] == {"shell"}  # wait NOT marked


async def test_multi_category_cell_lists_all_in_order(_loaded: Any):
    hook = _loaded.sdk_reminder_after_exec
    code = "subprocess.run(['ls'])\ntime.sleep(1)\nopen('f')\nrequests.get('u')"
    state = _state(_cell(code))
    result = await hook(state, _runtime(), _config())

    assert result is not None
    [note] = result["messages"]
    content = note.content
    # all four primitive names present, in CATEGORIES order.
    order = [
        content.index(name) for name in ("ava.shell.run", "ava.watcher", "ava.files", "ava.web")
    ]
    assert order == sorted(order)
    assert result["ava_sdk_reminder__reminded"] == {"shell", "wait", "files", "http"}


async def test_compaction_rearms_category(_loaded: Any):
    hook = _loaded.sdk_reminder_after_exec
    # shell was reminded last context window (bookmark 0); a compaction advanced
    # the version to 1 -> the set re-arms and shell hints again.
    state = _state(
        _cell("subprocess.run(['ls'])"),
        ava_sdk_reminder__reminded={"shell"},
        ava_sdk_reminder__last_seen_compact=0,
        compact=CompactState(version=1),
    )
    result = await hook(state, _runtime(), _config())

    assert result is not None
    [note] = result["messages"]
    assert "ava.shell.run" in note.content
    # bookmark advanced to the new version; reminded now only carries shell again.
    assert result["ava_sdk_reminder__last_seen_compact"] == 1
    assert result["ava_sdk_reminder__reminded"] == {"shell"}


async def test_compaction_not_advanced_keeps_dedup(_loaded: Any):
    hook = _loaded.sdk_reminder_after_exec
    # version == bookmark -> no re-arm; shell already reminded -> no-op.
    state = _state(
        _cell("subprocess.run(['ls'])"),
        ava_sdk_reminder__reminded={"shell"},
        ava_sdk_reminder__last_seen_compact=1,
        compact=CompactState(version=1),
    )
    result = await hook(state, _runtime(), _config())
    assert result is None


async def test_no_tool_calls_is_noop(_loaded: Any):
    hook = _loaded.sdk_reminder_after_exec
    # An assistant message with no tool_calls (the model spoke + paused) +
    # no trailing ToolMessage -> the tail shape does not match -> no-op.
    state = _state(
        [HumanMessage(content="hi", id="h1"), AIMessage(content="just talking", id="a1")]
    )
    result = await hook(state, _runtime(), _config())
    assert result is None


async def test_assistant_without_toolcall_before_output_is_noop(_loaded: Any):
    """messages[-2] is an AIMessage but it carries no tool_calls (defensive on
    the [-2] shape) -> no-op rather than indexing tool_calls[0]."""
    hook = _loaded.sdk_reminder_after_exec
    ai = AIMessage(content="spoke", id="a1")  # no tool_calls
    out = ToolMessage(content="x", tool_call_id="c1", id="o1")
    state = _state([ai, out])
    result = await hook(state, _runtime(), _config())
    assert result is None


async def test_code_matches_nothing_is_noop(_loaded: Any):
    hook = _loaded.sdk_reminder_after_exec
    state = _state(_cell("total = sum(range(10))\nprint(total)"))
    result = await hook(state, _runtime(), _config())
    assert result is None


async def test_files_hint_not_fired_when_listing_via_stdlib_content_via_sdk(_loaded: Any):
    """The user-reported false trigger (2026-08-26): a cell lists file names
    with stdlib glob while reading content through ava.files — the files hint
    must not fire, and nothing is marked."""
    hook = _loaded.sdk_reminder_after_exec
    code = "import glob\nfor p in glob.glob('*.md'):\n    ava.files.read(p)"
    result = await hook(_state(_cell(code)), _runtime(), _config())
    assert result is None


async def test_files_hint_fires_for_direct_open_read(_loaded: Any):
    """A cell that genuinely bypasses ava.files for content (open()) still
    receives the files hint."""
    hook = _loaded.sdk_reminder_after_exec
    result = await hook(_state(_cell("data = open('f.txt').read()")), _runtime(), _config())
    assert result is not None
    [note] = result["messages"]
    assert "ava.files" in note.content
    assert result["ava_sdk_reminder__reminded"] == {"files"}


async def test_hint_not_fired_for_trigger_words_in_string_literal(_loaded: Any):
    """A cell whose only 'open(' occurrence sits inside a string literal (e.g.
    a grep pattern or printed example) gets no files hint (literal masking)."""
    hook = _loaded.sdk_reminder_after_exec
    code = "for line in ava.shell.run(\"grep -rn 'open(' .\").splitlines():\n    print(line)"
    result = await hook(_state(_cell(code)), _runtime(), _config())
    assert result is None


async def test_short_history_is_noop(_loaded: Any):
    hook = _loaded.sdk_reminder_after_exec
    state = _state([ToolMessage(content="x", tool_call_id="c1", id="o1")])
    result = await hook(state, _runtime(), _config())
    assert result is None


# ── assumed-persistence NameError hint ─────────────────────────────────────


async def test_nameerror_for_name_used_in_earlier_cell_hints(_loaded: Any):
    hook = _loaded.sdk_reminder_after_exec
    messages = _cell("cache = {'ready': True}", id_suffix="1") + _cell(
        "print(cache)", _nameerror_output("cache"), id_suffix="2"
    )
    result = await hook(_state(messages), _runtime(), _config())

    assert result is not None
    [note] = result["messages"]
    assert note.content == (
        "[system] NameError: 'cache' appeared in an earlier execute_code call, "
        "but each call runs in a fresh interpreter — variables do not persist "
        "between calls. Re-define it here, or carry state via files or shell sessions."
    )
    assert result["ava_sdk_reminder__reminded"] == {"nameerror:cache"}


async def test_nameerror_with_python_suggestion_suffix_hints(_loaded: Any):
    """Python may append a `Did you mean` clause to the NameError line; the
    stable `name 'X' is not defined` prefix still identifies the failure."""
    hook = _loaded.sdk_reminder_after_exec
    output = _nameerror_output("listt") + ". Did you mean: 'list'?"
    messages = _cell("listt = [1]", id_suffix="1") + _cell("print(listt)", output, id_suffix="2")

    assert await hook(_state(messages), _runtime(), _config()) is not None


async def test_nameerror_without_prior_whole_name_is_noop(_loaded: Any):
    """A substring in an earlier cell does not count, and the current cell is
    excluded from the search even though it necessarily contains the name."""
    hook = _loaded.sdk_reminder_after_exec
    messages = _cell("cached_value = 1", id_suffix="1") + _cell(
        "print(cache)", _nameerror_output("cache"), id_suffix="2"
    )

    assert await hook(_state(messages), _runtime(), _config()) is None


async def test_nameerror_hint_disabled_is_noop(_loaded: Any, monkeypatch: pytest.MonkeyPatch):
    from shared.config import settings

    monkeypatch.setattr(settings.agent, "sdk_nameerror_hint_enabled", False)
    hook = _loaded.sdk_reminder_after_exec
    messages = _cell("cache = 1", id_suffix="1") + _cell(
        "print(cache)", _nameerror_output("cache"), id_suffix="2"
    )

    assert await hook(_state(messages), _runtime(), _config()) is None


async def test_repeated_nameerror_for_same_name_hints_once_per_window(_loaded: Any):
    hook = _loaded.sdk_reminder_after_exec
    first_messages = _cell("cache = 1", id_suffix="1") + _cell(
        "print(cache)", _nameerror_output("cache"), id_suffix="2"
    )
    first = await hook(_state(first_messages), _runtime(), _config())
    assert first is not None

    second_messages = first_messages + _cell(
        "print(cache)", _nameerror_output("cache"), id_suffix="3"
    )
    second_state = _state(
        second_messages,
        ava_sdk_reminder__reminded=first["ava_sdk_reminder__reminded"],
        ava_sdk_reminder__last_seen_compact=first["ava_sdk_reminder__last_seen_compact"],
    )

    assert await hook(second_state, _runtime(), _config()) is None


@pytest.mark.parametrize("name", ["len", "for"])
async def test_nameerror_hint_skips_builtins_and_keywords(_loaded: Any, name: str):
    hook = _loaded.sdk_reminder_after_exec
    messages = _cell(f"{name} = 1", id_suffix="1") + _cell(
        f"print({name})", _nameerror_output(name), id_suffix="2"
    )

    assert await hook(_state(messages), _runtime(), _config()) is None


# ── the agent_reply matcher (inbound tail scan) ─────────────────────────────


def test_tail_has_agent_inbound_true():
    msgs: list[AnyMessage] = [AIMessage(content="prev", id="a0"), _agent_inbound(source="agent:9")]
    assert tail_has_agent_inbound(msgs) is True


def test_tail_has_agent_inbound_user_only_false():
    msgs: list[AnyMessage] = [
        AIMessage(content="prev", id="a0"),
        inbound_message(content="hi", source="user", inbound_id=2),
    ]
    assert tail_has_agent_inbound(msgs) is False


def test_tail_has_agent_inbound_stops_at_prior_ai():
    # An agent inbound BEFORE the most recent AIMessage is part of a prior turn
    # (already answered) -> not in the current incoming batch.
    msgs: list[AnyMessage] = [
        _agent_inbound(source="agent:3"),
        AIMessage(content="already answered", id="a1"),
        inbound_message(content="hi", source="user", inbound_id=2),
    ]
    assert tail_has_agent_inbound(msgs) is False


def test_tail_has_agent_inbound_ui_source_false():
    msgs: list[AnyMessage] = [
        AIMessage(content="prev", id="a0"),
        inbound_message(content="x", source="user", inbound_id=3),
    ]
    assert tail_has_agent_inbound(msgs) is False


# ── the before_llm hook (agent_reply) ───────────────────────────────────────


async def test_agent_reply_first_hit_injects_note(_loaded: Any):
    hook = _loaded.sdk_reminder_agent_reply_before_llm
    state = _state([AIMessage(content="prev", id="a0"), _agent_inbound(source="agent:9")])
    result = await hook(state, _runtime(), _config())

    assert result is not None
    [note] = result["messages"]
    assert note.additional_kwargs["ava_msg_type"] == "system_note"
    assert note.additional_kwargs["ava_note_tag"] == "agent_reply"
    assert note.additional_kwargs["ava_created_at"]  # stamped with the inject time
    assert "ava.agents.send_message" in note.content
    assert result["ava_sdk_reminder__reminded"] == {AGENT_REPLY_CATEGORY}


async def test_agent_reply_second_hit_no_inject(_loaded: Any):
    hook = _loaded.sdk_reminder_agent_reply_before_llm
    state = _state(
        [AIMessage(content="prev", id="a0"), _agent_inbound(source="agent:9")],
        ava_sdk_reminder__reminded={AGENT_REPLY_CATEGORY},
    )
    result = await hook(state, _runtime(), _config())
    assert result is None


async def test_agent_reply_rearms_after_compaction(_loaded: Any):
    hook = _loaded.sdk_reminder_agent_reply_before_llm
    # agent_reply reminded last window (bookmark 0); compaction advanced to 1 ->
    # the set re-arms and the note fires again.
    state = _state(
        [AIMessage(content="prev", id="a0"), _agent_inbound(source="agent:9")],
        ava_sdk_reminder__reminded={AGENT_REPLY_CATEGORY},
        ava_sdk_reminder__last_seen_compact=0,
        compact=CompactState(version=1),
    )
    result = await hook(state, _runtime(), _config())

    assert result is not None
    [note] = result["messages"]
    assert "ava.agents.send_message" in note.content
    assert result["ava_sdk_reminder__last_seen_compact"] == 1
    assert result["ava_sdk_reminder__reminded"] == {AGENT_REPLY_CATEGORY}


async def test_agent_reply_user_inbound_is_noop(_loaded: Any):
    hook = _loaded.sdk_reminder_agent_reply_before_llm
    state = _state(
        [
            AIMessage(content="prev", id="a0"),
            inbound_message(content="hi", source="user", inbound_id=2),
        ]
    )
    result = await hook(state, _runtime(), _config())
    assert result is None


async def test_agent_reply_defers_when_compaction_fires(
    _loaded: Any, monkeypatch: pytest.MonkeyPatch
):
    """When auto-compact would fire this same before_llm node run, the note is
    deferred (no inject, not marked) so it does not clobber / get clobbered by
    compaction's full-history message replacement."""
    # Force the compact threshold to 1 token so any history triggers it.
    _pin_compact_budget(monkeypatch, hard_tokens=1)

    hook = _loaded.sdk_reminder_agent_reply_before_llm
    # A non-empty conversation over the (forced-to-1) token threshold -> compaction fires.
    msgs: list[AnyMessage] = [
        AIMessage(content="prev", id="a0"),
        *(HumanMessage(content="x" * 200, id=f"h{i}") for i in range(5)),
        _agent_inbound(source="agent:9"),
    ]
    state = _state(msgs)
    result = await hook(state, _runtime(), _config())
    assert result is None  # deferred; agent_reply not marked


# ── the agent_reply_reminder_cadence config ────────────────────────────────────


async def test_agent_reply_once_cadence_dedups_and_rearms(
    _loaded: Any, monkeypatch: pytest.MonkeyPatch
):
    """`once_per_compaction` (the default, set explicitly here): a second inbound
    in the same window no-ops, and a compaction re-arms so the note fires again.
    Pins the behavior to the config value rather than the field default."""
    from shared.config import settings

    monkeypatch.setattr(settings.agent, "agent_reply_reminder_cadence", "once_per_compaction")
    hook = _loaded.sdk_reminder_agent_reply_before_llm

    # Same window, already reminded -> no repeat.
    same_window = _state(
        [AIMessage(content="prev", id="a0"), _agent_inbound(source="agent:9")],
        ava_sdk_reminder__reminded={AGENT_REPLY_CATEGORY},
    )
    assert await hook(same_window, _runtime(), _config()) is None

    # Compaction advanced the version past the bookmark -> re-armed, fires again.
    after_compact = _state(
        [AIMessage(content="prev", id="a0"), _agent_inbound(source="agent:9")],
        ava_sdk_reminder__reminded={AGENT_REPLY_CATEGORY},
        ava_sdk_reminder__last_seen_compact=0,
        compact=CompactState(version=1),
    )
    result = await hook(after_compact, _runtime(), _config())
    assert result is not None
    assert result["ava_sdk_reminder__last_seen_compact"] == 1
    assert result["ava_sdk_reminder__reminded"] == {AGENT_REPLY_CATEGORY}


async def test_agent_reply_every_time_fires_even_when_already_reminded(
    _loaded: Any, monkeypatch: pytest.MonkeyPatch
):
    """`every_time`: the note fires on every agent inbound, even one already
    marked in the shared `reminded` set — and it does not touch that set (the
    after_exec hook owns the code-category re-arm)."""
    from shared.config import settings

    monkeypatch.setattr(settings.agent, "agent_reply_reminder_cadence", "every_time")
    hook = _loaded.sdk_reminder_agent_reply_before_llm

    state = _state(
        [AIMessage(content="prev", id="a0"), _agent_inbound(source="agent:9")],
        ava_sdk_reminder__reminded={AGENT_REPLY_CATEGORY, "shell"},
    )
    result = await hook(state, _runtime(), _config())

    assert result is not None
    [note] = result["messages"]
    assert note.additional_kwargs["ava_note_tag"] == "agent_reply"
    assert "ava.agents.send_message" in note.content
    # every_time does not participate in the once-per-window bookkeeping.
    assert "ava_sdk_reminder__reminded" not in result
    assert "ava_sdk_reminder__last_seen_compact" not in result


async def test_agent_reply_every_time_user_inbound_is_noop(
    _loaded: Any, monkeypatch: pytest.MonkeyPatch
):
    """`every_time` still gates on an agent-sourced inbound — a user inbound
    never triggers the reminder."""
    from shared.config import settings

    monkeypatch.setattr(settings.agent, "agent_reply_reminder_cadence", "every_time")
    hook = _loaded.sdk_reminder_agent_reply_before_llm

    state = _state(
        [
            AIMessage(content="prev", id="a0"),
            inbound_message(content="hi", source="user", inbound_id=2),
        ]
    )
    assert await hook(state, _runtime(), _config()) is None


async def test_agent_reply_every_time_still_defers_on_compaction(
    _loaded: Any, monkeypatch: pytest.MonkeyPatch
):
    """`every_time` defers exactly like `once_per_compaction` when auto-compact
    fires the same turn — the note would be clobbered by the message replacement."""
    from shared.config import settings

    monkeypatch.setattr(settings.agent, "agent_reply_reminder_cadence", "every_time")
    _pin_compact_budget(monkeypatch, hard_tokens=1)
    hook = _loaded.sdk_reminder_agent_reply_before_llm

    msgs: list[AnyMessage] = [
        AIMessage(content="prev", id="a0"),
        *(HumanMessage(content="x" * 200, id=f"h{i}") for i in range(5)),
        _agent_inbound(source="agent:9"),
    ]
    assert await hook(_state(msgs), _runtime(), _config()) is None


# ── defer-predicate parity with the real auto-compact gate ──────────────────


@pytest.mark.parametrize(
    "history_len_kind,auto_compact_tokens,expect_fire",
    [
        # under the token threshold -> no fire
        ("long", 10_000_000, False),
        # exactly at the threshold (est_tokens == threshold) -> no fire (gate is >)
        ("at_threshold", None, False),
        # over the threshold with a non-empty conversation -> fire
        ("long", 1, True),
        # over the threshold but no conversation to compress (system prompt only) -> no fire
        ("no_conversation", 1, False),
    ],
)
async def test_defer_predicate_matches_real_gate(
    _loaded: Any,
    monkeypatch: pytest.MonkeyPatch,
    history_len_kind,
    auto_compact_tokens,
    expect_fire,
):
    """`auto_compact_will_fire(state)` is the single shared gate the reminder plugins
    call; this pins it to the real `auto_compact_before_llm` firing across the
    threshold boundary. For each parametrized state, the predicate must equal
    "does the real auto_compact_before_llm actually return a replacement"
    (generate_summary stubbed so a fire produces a non-None result without a
    live LLM).
    """
    from agent.hooks import compact as compact_mod
    from agent.hooks.compact import auto_compact_will_fire

    if history_len_kind == "no_conversation":
        # only the system prompt -> conversation_messages empty -> no fire
        msgs: list[AnyMessage] = [SystemMessage(content="x" * 400)]
    elif history_len_kind == "at_threshold":
        # Build messages whose estimated tokens (total_chars // 4) exactly equal
        # the configured threshold, then set the threshold to that value so the
        # `occupancy <= threshold` gate is exercised right at the boundary.
        msgs = [HumanMessage(content="y" * 400, id=f"h{i}") for i in range(8)]
        total_chars = sum(len(m.content) for m in msgs)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        auto_compact_tokens = total_chars // 4
    else:  # "long"
        msgs = [HumanMessage(content="z" * 400, id=f"h{i}") for i in range(8)]

    _pin_compact_budget(monkeypatch, hard_tokens=auto_compact_tokens)  # pyright: ignore[reportUnknownArgumentType]

    # Stub generate_summary so a "would fire" path produces a real replacement
    # dict without invoking a live Compaction LLM. Long enough to clear the
    # auto-compact retry floor on the first attempt.
    async def _fake_generate_summary(messages, llm):
        return "stub summary " * 100

    monkeypatch.setattr(compact_mod, "generate_summary", _fake_generate_summary)  # pyright: ignore[reportUnknownArgumentType]

    state = _state(msgs)
    predicate = auto_compact_will_fire(state)
    real = await compact_mod.auto_compact_before_llm(state, _runtime(), _config())  # pyright: ignore[reportUnknownMemberType]
    assert predicate is expect_fire
    assert predicate == (real is not None)


# ── real two-hook runner integration (both orderings) ───────────────────────


@pytest.mark.parametrize("reminder_first", [True, False])
async def test_real_runner_compaction_wins_no_note(
    _loaded: Any, monkeypatch: pytest.MonkeyPatch, reminder_first
):
    """Register the real ava_compact wrapper + the sdk_reminder agent_reply hook
    into a real make_hook_runner('before_llm', ...) and run it in BOTH
    orderings. With the force ceiling pinned to 1 token + a long history + a tail
    agent inbound, auto-compact fires; the reminder must defer (no system_note in the
    final messages, agent_reply unmarked) and compact.version bumps exactly +1.
    After A2 the runner also raises if the two hooks ever co-write a key — a
    passing run proves the defer prevents a same-key (messages) collision.
    """
    from langgraph.graph.message import add_messages

    from agent.hooks import HOOKS, make_hook_runner
    from agent.hooks import compact as compact_mod

    # Force auto-compact to fire on any history.
    _pin_compact_budget(monkeypatch, hard_tokens=1)

    # The compact hook wrapper lives directly in agent.hooks.compact.
    from agent.hooks.compact import _auto_compact_with_version_bump

    # Long enough to clear the auto-compact retry floor on the first attempt.
    long_summary = "compacted summary " * 100

    async def _fake_generate_summary(messages, llm):
        return long_summary

    monkeypatch.setattr(compact_mod, "generate_summary", _fake_generate_summary)  # pyright: ignore[reportUnknownArgumentType]

    compact_hook = _auto_compact_with_version_bump
    reminder_hook = _loaded.sdk_reminder_agent_reply_before_llm

    # Install exactly these two hooks (in the chosen order) on before_llm,
    # snapshotting + restoring the live list so other tests are unaffected.
    saved = list(HOOKS["before_llm"])
    HOOKS["before_llm"][:] = (
        [reminder_hook, compact_hook] if reminder_first else [compact_hook, reminder_hook]
    )
    try:
        runner = make_hook_runner("before_llm", default_next="llm")
        sys_msg = SystemMessage(content="<sys>")
        msgs: list[AnyMessage] = [
            sys_msg,
            *(HumanMessage(content="x" * 1000, id=f"h{i}") for i in range(5)),
            _agent_inbound(source="agent:9"),
        ]
        state = _state(msgs, compact=CompactState(version=0))
        cmd = await runner(state, _runtime_for_runner(), _config())
    finally:
        HOOKS["before_llm"][:] = saved

    update = cmd.update
    assert isinstance(update, dict)
    # Apply the real add_messages reducer to get the committed messages.
    final = add_messages(list(state.messages), update["messages"])  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(final, list)

    # Compaction cleared the window outright — the standing head is rebuilt by
    # the init_context node, not by this hook — so nothing survives the reducer,
    # and in particular no reminder note slipped in alongside the compaction.
    assert final == []
    # The summary rides in the parked tail the compaction handed to that node.
    assert [m.content for m in update["context_reset"].tail] == [  # pyright: ignore[reportUnknownMemberType]
        compact_mod.compose_summary_message(long_summary)
    ]
    assert cmd.goto == "init_context"
    # compact.version bumped exactly +1; agent_reply not marked (deferred).
    assert update["compact"].version == 1  # pyright: ignore[reportUnknownMemberType]
    assert AGENT_REPLY_CATEGORY not in update.get("ava_sdk_reminder__reminded", set())  # pyright: ignore[reportUnknownMemberType]


# ── exec-output left untouched (the hint is a separate note) ────────────────


async def test_after_exec_leaves_exec_output_untouched(_loaded: Any):
    """The hint is a fresh system-note, not a rewrite of the exec-output
    message: the injected message is a new id-less HumanMessage (so the reducer
    appends it after the output rather than replacing it), and the real
    exec_output message keeps its content + every additional_kwargs field."""
    from agent.messages import exec_output_message

    hook = _loaded.sdk_reminder_after_exec
    out = exec_output_message(
        content="original stdout",
        tool_call_id="c1",
        exit_code=42,
        cancelled=True,
        exec_ms=1300,
    )
    out.id = "out-1"
    out_kwargs_before = dict(out.additional_kwargs)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    ai = AIMessage(
        content="",
        tool_calls=[
            {"name": "execute_code", "args": {"code": "subprocess.run(['ls'])"}, "id": "c1"}
        ],
    )
    state = _state([HumanMessage(content="do it", id="h1"), ai, out])

    result = await hook(state, _runtime(), _config())
    assert result is not None
    [note] = result["messages"]
    # a fresh system-note (id-less -> the reducer appends), not the output message
    assert isinstance(note, HumanMessage)
    assert note.additional_kwargs["ava_msg_type"] == "system_note"  # pyright: ignore[reportUnknownMemberType]
    assert note.id is None
    assert "ava.shell.run" in note.content  # pyright: ignore[reportUnknownMemberType]
    # the real exec-output message is untouched: content + metadata intact
    assert out.content == "original stdout"  # pyright: ignore[reportUnknownMemberType]
    assert out.additional_kwargs == out_kwargs_before  # pyright: ignore[reportUnknownMemberType]


# ── tail_has_agent_inbound shape coverage ───────────────────────────────────


def test_tail_has_agent_inbound_exec_tail_false():
    """A normal exec tail [..., AIMessage(tool_call), ToolMessage] — the scan
    stops at the AIMessage, the trailing ToolMessage is not a HumanMessage, so
    no agent inbound -> False."""
    msgs: list[AnyMessage] = [
        AIMessage(
            content="",
            tool_calls=[{"name": "execute_code", "args": {"code": "x"}, "id": "c1"}],
            id="a1",
        ),
        ToolMessage(content="out", tool_call_id="c1", id="o1"),
    ]
    assert tail_has_agent_inbound(msgs) is False


def test_tail_has_agent_inbound_first_turn_agent_only_true():
    """First turn, no prior AIMessage at all, a single agent inbound -> True
    (the scan walks the whole list without hitting an AIMessage boundary)."""
    msgs: list[AnyMessage] = [_agent_inbound(source="agent:7")]
    assert tail_has_agent_inbound(msgs) is True


def test_tail_has_agent_inbound_user_only_no_ai_false():
    """Only a user inbound, no AIMessage -> False."""
    msgs: list[AnyMessage] = [inbound_message(content="hi", source="user", inbound_id=2)]
    assert tail_has_agent_inbound(msgs) is False
