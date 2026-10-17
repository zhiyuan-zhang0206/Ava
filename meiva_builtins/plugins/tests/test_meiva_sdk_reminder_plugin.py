"""SDK reminder contracts: per-call code hints and persistence NameErrors,
with per-compaction cadence; agent-reply hints on agent-sourced inbound.
"""

import inspect
from collections.abc import Iterator
from typing import Any

import pytest
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph.hooks import compact
from agent.messages import inbound_message, tail_has_agent_inbound
from agent.state import CompactState, build_agent_state
from base.agents.context import MeivaContext
from base.db.code_version_gate import ProcessDbGate
from base.packages.plugins.extensions import ExtensionRegistry
from meiva.sdk_surface import install
from meiva_builtins.plugins.meiva_sdk_reminder._state import (
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
    from base.lm.context_budget import ContextBudget

    budget = ContextBudget(
        max_context_tokens=1_000_000,
        soft_compact_tokens=soft_tokens,
        hard_compact_tokens=hard_tokens,
    )
    monkeypatch.setattr(compact, "resolve_context_budget", lambda *_, **_kw: budget)  # pyright: ignore[reportUnknownArgumentType]


@pytest.fixture
def reminder_plugin_loaded() -> Any:
    """The meiva_sdk_reminder agent-runtime face.
    Compact is now a core capability (Issue #1284) — its state fields live
    directly on BaseAgentState (nested compact/memory, etc.) and its config comes
    from base.config.settings. No separate plugin module to load.
    """
    from meiva_builtins.plugins.meiva_sdk_reminder import agent_runtime as _plugin

    return _plugin


def _state(messages: list[AnyMessage], **fields: Any):
    """AgentState carrying the loaded face's declared state (`reminder_plugin_loaded` has run)."""
    from meiva_builtins.plugins.meiva_sdk_reminder import agent_runtime

    extensions = ExtensionRegistry((("meiva_sdk_reminder", agent_runtime.contribute()),))
    return build_agent_state(extensions)(messages=messages, **fields)


def _runtime(*, database_gate: ProcessDbGate) -> Runtime[MeivaContext]:
    # The hooks ignore runtime/config; a placeholder context satisfies the
    # MeivaContext invariant.
    from agent.hooks.tests.fakes import placeholder_runtime

    return placeholder_runtime(database_gate=database_gate)


def _runtime_for_runner(*, database_gate: ProcessDbGate) -> Runtime[MeivaContext]:
    """Runtime for tests that drive a real make_hook_runner — its node_lifecycle
    wrapper publishes a timeline snapshot through ops_pool, so a DB-shaped fake
    pool (not a bare MagicMock) is needed."""
    from agent.hooks.tests.fakes import make_fake_ops_pool, placeholder_runtime

    return placeholder_runtime(make_fake_ops_pool(), database_gate=database_gate)


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
        'Traceback (most recent call last):\n  File "<meiva-exec>", line 1, in <module>\n'
        f"NameError: name '{name}' is not defined"
    )


def _agent_inbound(content: str = "ping", source: str = "agent:7") -> AnyMessage:
    return inbound_message(content=content, source=source, inbound_id=1, body_start=0)


# ── the pure matcher ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "code,expected",
    [
        ("import subprocess; subprocess.run(['ls'])", ["shell"]),
        ("os.system('ls')", ["shell"]),
        ("os.popen('ls')", ["shell"]),
        ("time.sleep(5)", ["wait"]),
        ("sleep(5)", ["wait"]),
        ("meiva.shell.run('ls')", []),  # SDK call -> no hint
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
        # the user-reported false trigger: stdlib lists names, meiva.files reads
        # content -> no hint (2026-08-26 ruling).
        ("import glob\nfor p in glob.glob('*.md'):\n    meiva.files.read(p)", []),
        ("os.listdir('d')\nmeiva.files.read('f')", []),
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
        ("meiva.files.read('f')", []),  # `.read(` is not files-triggered (needs read_text)
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
    """Every category has a hint that names its `meiva` primitive. wait/files/http
    point at a self-serve help() entry; shell inlines the `meiva.shell.run`
    contract instead (drift-guarded below)."""
    for cat in CATEGORIES:
        h = hint_for(cat)
        assert "meiva" in h
        if cat != "shell":
            assert "help(" in h


@pytest.fixture
def load_meiva_code_plugin() -> Iterator[None]:
    """Install plugins.meiva_code's SDK surface so `meiva.shell.run` carries the same
    wrap the agent runtime installs — the drift guard below reads the agent-facing
    signature + docstring off that live object. Teardown uninstalls (wraps
    included) so nothing leaks into the next test."""
    from meiva_builtins.plugins.meiva_code import plugin

    install.install(ExtensionRegistry((("meiva_code", plugin.contribute()),)))

    yield

    install.uninstall()


def test_shell_hint_embeds_live_shell_run_contract(load_meiva_code_plugin: None):
    """The shell hint's inlined signature + docstring track the live
    `meiva.shell.run` (the meiva_code wrap is the agent-facing contract): when the
    wrapper's signature or docstring drifts, this goes red and the hint follows
    (user ruling 2026-09-12)."""
    import meiva as meiva_sdk
    from meiva.sdk_surface.help import format_signature

    hint = hint_for("shell")

    # Signature line: rendered by the same helper as meiva.help()'s stub, so a
    # changed parameter set or annotation fails here.
    assert f"def run{format_signature(meiva_sdk.shell.run)}:" in hint

    # Docstring block: verbatim modulo line wrapping.
    doc = inspect.getdoc(meiva_sdk.shell.run)
    assert doc is not None
    assert " ".join(doc.split()) in " ".join(hint.split())


@pytest.mark.parametrize(
    "code,expected",
    [
        ("time.sleep(1)", False),
        ("meiva.watcher.at('5m', name='test')", True),
        ("# Watcher script\nsleep(2)", True),  # case-insensitive substring
        ("x = 1 + 1", False),
    ],
)
def test_mentions_watcher(code: str, expected: bool):
    assert mentions_watcher(code) is expected


# ── the after_exec hook (code categories) ──────────────────────────────────


async def test_first_hit_injects_note_and_marks(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    state = _state(_cell("subprocess.run(['ls'])"))
    result = await hook(state, _runtime(database_gate=database_gate), _config())

    assert result is not None
    # hint injected as its own system-note; the exec-output message is untouched.
    [note] = result["messages"]
    assert note.additional_kwargs["meiva_msg_type"] == "system_note"
    assert note.additional_kwargs["meiva_note_tag"] == "sdk_hint"
    assert note.additional_kwargs["meiva_created_at"]  # stamped with the inject time
    assert "meiva.shell.run" in note.content
    # category recorded.
    assert result["meiva_sdk_reminder__reminded"] == {"shell"}
    assert result["meiva_sdk_reminder__last_seen_compact"] == 0


async def test_second_hit_same_category_no_append(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    state = _state(_cell("subprocess.run(['ls'])"), meiva_sdk_reminder__reminded={"shell"})
    result = await hook(state, _runtime(database_gate=database_gate), _config())
    assert result is None


async def test_code_every_time_cadence_hints_two_consecutive_matching_cells(
    reminder_plugin_loaded: Any, monkeypatch: pytest.MonkeyPatch, *, database_gate: ProcessDbGate
):
    """`every_time` bypasses the reminded gate for code categories, so two
    consecutive shell cells each receive the shell hint."""
    from base.config import settings

    monkeypatch.setattr(settings.agent, "sdk_code_reminder_cadence", "every_time")
    hook = reminder_plugin_loaded.contribute().after_exec[0]

    first = await hook(
        _state(_cell("subprocess.run(['first'])")), _runtime(database_gate=database_gate), _config()
    )
    assert first is not None
    second_state = _state(
        _cell("subprocess.run(['second'])"),
        meiva_sdk_reminder__reminded=first["meiva_sdk_reminder__reminded"],
        meiva_sdk_reminder__last_seen_compact=first["meiva_sdk_reminder__last_seen_compact"],
    )
    second = await hook(second_state, _runtime(database_gate=database_gate), _config())

    assert second is not None
    for result in (first, second):
        [note] = result["messages"]
        assert "meiva.shell.run" in note.content


async def test_code_once_cadence_hints_only_first_consecutive_matching_cell(
    reminder_plugin_loaded: Any, monkeypatch: pytest.MonkeyPatch, *, database_gate: ProcessDbGate
):
    """`once_per_compaction` (the default) preserves the existing behavior:
    the first shell cell hints and the next shell cell in the window no-ops."""
    from base.config import settings

    monkeypatch.setattr(settings.agent, "sdk_code_reminder_cadence", "once_per_compaction")
    hook = reminder_plugin_loaded.contribute().after_exec[0]

    first = await hook(
        _state(_cell("subprocess.run(['first'])")), _runtime(database_gate=database_gate), _config()
    )
    assert first is not None
    second_state = _state(
        _cell("subprocess.run(['second'])"),
        meiva_sdk_reminder__reminded=first["meiva_sdk_reminder__reminded"],
        meiva_sdk_reminder__last_seen_compact=first["meiva_sdk_reminder__last_seen_compact"],
    )

    assert await hook(second_state, _runtime(database_gate=database_gate), _config()) is None


async def test_different_categories_each_fire_once(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    # shell already reminded; this cell hits shell + wait -> only wait is fresh.
    state = _state(
        _cell("subprocess.run(['ls'])\ntime.sleep(2)"),
        meiva_sdk_reminder__reminded={"shell"},
    )
    result = await hook(state, _runtime(database_gate=database_gate), _config())

    assert result is not None
    [note] = result["messages"]
    assert note.additional_kwargs["meiva_msg_type"] == "system_note"
    assert "meiva.watcher" in note.content
    assert "meiva.shell.run" not in note.content  # shell already hinted
    assert result["meiva_sdk_reminder__reminded"] == {"shell", "wait"}


async def test_wait_with_watcher_marked_silently_no_hint(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    """A cell that sleeps while already naming `watcher` is the agent working
    with the watcher primitive itself — the wait hint is suppressed but the
    category is marked seen (so it fires neither now nor later this window)."""
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    state = _state(_cell("meiva.watcher.at('5m', name='test')\ntime.sleep(1)"))
    result = await hook(state, _runtime(database_gate=database_gate), _config())

    assert result is not None
    # marked seen, but no hint emitted -> no output-message replacement.
    assert "messages" not in result
    assert result["meiva_sdk_reminder__reminded"] == {"wait"}
    assert result["meiva_sdk_reminder__last_seen_compact"] == 0


async def test_wait_with_watcher_suppressed_other_category_still_hints(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    """When a watcher-naming sleep cell also trips another category, the wait
    hint is suppressed (but marked) while the other category still hints."""
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    state = _state(_cell("subprocess.run(['ls'])\nmeiva.watcher\ntime.sleep(1)"))
    result = await hook(state, _runtime(database_gate=database_gate), _config())

    assert result is not None
    [note] = result["messages"]
    assert "meiva.shell.run" in note.content
    assert "meiva.watcher" not in note.content  # wait hint suppressed
    assert result["meiva_sdk_reminder__reminded"] == {"shell", "wait"}


async def test_wait_with_watcher_already_marked_is_noop(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    """A second watcher-naming sleep cell, wait already marked -> no-op (the
    silent suppression does not re-fire or re-persist)."""
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    state = _state(
        _cell("meiva.watcher.at('5m', name='test')\ntime.sleep(1)"),
        meiva_sdk_reminder__reminded={"wait"},
    )
    result = await hook(state, _runtime(database_gate=database_gate), _config())
    assert result is None


async def test_compaction_rearms_silent_watcher_path(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    """The silent-suppress path is the only one that advances the bookmark
    WITHOUT emitting a message — pin that a re-arm still persists the bookmark
    advance and re-marks wait, with no hint message. Guards a refactor that
    moved the bookmark write under the hint-emit branch from stranding it."""
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    state = _state(
        _cell("meiva.watcher.at('5m', name='test')\ntime.sleep(1)"),
        meiva_sdk_reminder__reminded={"wait"},
        meiva_sdk_reminder__last_seen_compact=0,
        compact=CompactState(version=1),
    )
    result = await hook(state, _runtime(database_gate=database_gate), _config())

    assert result is not None
    assert "messages" not in result  # silent path emits no hint
    assert result["meiva_sdk_reminder__last_seen_compact"] == 1  # bookmark persisted
    assert result["meiva_sdk_reminder__reminded"] == {"wait"}  # re-marked after re-arm


async def test_wait_suppressed_with_stale_other_category_marks_only(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    """A watcher+sleep cell that also trips an already-reminded category: nothing
    hints (the other category is stale) but wait is still newly marked — the
    `silent - reminded` branch where `hinted` is empty yet `newly_seen` is not."""
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    state = _state(
        _cell("subprocess.run(['ls'])\nmeiva.watcher\ntime.sleep(1)"),
        meiva_sdk_reminder__reminded={"shell"},
    )
    result = await hook(state, _runtime(database_gate=database_gate), _config())

    assert result is not None
    assert "messages" not in result  # shell stale, wait suppressed -> no hint
    assert result["meiva_sdk_reminder__reminded"] == {"shell", "wait"}


async def test_watcher_named_without_sleep_does_not_suppress(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    """Naming `watcher` only suppresses when the cell also trips the wait
    trigger. A watcher mention beside a non-wait idiom still hints that idiom
    and does not mark wait (guards the `"wait" in matched` conjunct)."""
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    state = _state(_cell("meiva.watcher\nsubprocess.run(['ls'])"))
    result = await hook(state, _runtime(database_gate=database_gate), _config())

    assert result is not None
    [note] = result["messages"]
    assert "meiva.shell.run" in note.content
    assert result["meiva_sdk_reminder__reminded"] == {"shell"}  # wait NOT marked


async def test_multi_category_cell_lists_all_in_order(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    code = "subprocess.run(['ls'])\ntime.sleep(1)\nopen('f')\nrequests.get('u')"
    state = _state(_cell(code))
    result = await hook(state, _runtime(database_gate=database_gate), _config())

    assert result is not None
    [note] = result["messages"]
    content = note.content
    # all four primitive names present, in CATEGORIES order.
    order = [
        content.index(name)
        for name in ("meiva.shell.run", "meiva.watcher", "meiva.files", "meiva.web")
    ]
    assert order == sorted(order)
    assert result["meiva_sdk_reminder__reminded"] == {"shell", "wait", "files", "http"}


async def test_compaction_rearms_category(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    # shell was reminded last context window (bookmark 0); a compaction advanced
    # the version to 1 -> the set re-arms and shell hints again.
    state = _state(
        _cell("subprocess.run(['ls'])"),
        meiva_sdk_reminder__reminded={"shell"},
        meiva_sdk_reminder__last_seen_compact=0,
        compact=CompactState(version=1),
    )
    result = await hook(state, _runtime(database_gate=database_gate), _config())

    assert result is not None
    [note] = result["messages"]
    assert "meiva.shell.run" in note.content
    # bookmark advanced to the new version; reminded now only carries shell again.
    assert result["meiva_sdk_reminder__last_seen_compact"] == 1
    assert result["meiva_sdk_reminder__reminded"] == {"shell"}


async def test_compaction_not_advanced_keeps_dedup(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    # version == bookmark -> no re-arm; shell already reminded -> no-op.
    state = _state(
        _cell("subprocess.run(['ls'])"),
        meiva_sdk_reminder__reminded={"shell"},
        meiva_sdk_reminder__last_seen_compact=1,
        compact=CompactState(version=1),
    )
    result = await hook(state, _runtime(database_gate=database_gate), _config())
    assert result is None


async def test_no_tool_calls_is_noop(reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate):
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    # An assistant message with no tool_calls (the model spoke + paused) +
    # no trailing ToolMessage -> the tail shape does not match -> no-op.
    state = _state(
        [HumanMessage(content="hi", id="h1"), AIMessage(content="just talking", id="a1")]
    )
    result = await hook(state, _runtime(database_gate=database_gate), _config())
    assert result is None


async def test_assistant_without_toolcall_before_output_is_noop(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    """messages[-2] is an AIMessage but it carries no tool_calls (defensive on
    the [-2] shape) -> no-op rather than indexing tool_calls[0]."""
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    ai = AIMessage(content="spoke", id="a1")  # no tool_calls
    out = ToolMessage(content="x", tool_call_id="c1", id="o1")
    state = _state([ai, out])
    result = await hook(state, _runtime(database_gate=database_gate), _config())
    assert result is None


async def test_code_matches_nothing_is_noop(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    state = _state(_cell("total = sum(range(10))\nprint(total)"))
    result = await hook(state, _runtime(database_gate=database_gate), _config())
    assert result is None


async def test_files_hint_not_fired_when_listing_via_stdlib_content_via_sdk(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    """The user-reported false trigger (2026-08-26): a cell lists file names
    with stdlib glob while reading content through meiva.files — the files hint
    must not fire, and nothing is marked."""
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    code = "import glob\nfor p in glob.glob('*.md'):\n    meiva.files.read(p)"
    result = await hook(_state(_cell(code)), _runtime(database_gate=database_gate), _config())
    assert result is None


async def test_files_hint_fires_for_direct_open_read(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    """A cell that genuinely bypasses meiva.files for content (open()) still
    receives the files hint."""
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    result = await hook(
        _state(_cell("data = open('f.txt').read()")),
        _runtime(database_gate=database_gate),
        _config(),
    )
    assert result is not None
    [note] = result["messages"]
    assert "meiva.files" in note.content
    assert result["meiva_sdk_reminder__reminded"] == {"files"}


async def test_hint_not_fired_for_trigger_words_in_string_literal(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    """A cell whose only 'open(' occurrence sits inside a string literal (e.g.
    a grep pattern or printed example) gets no files hint (literal masking)."""
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    code = "for line in meiva.shell.run(\"grep -rn 'open(' .\").splitlines():\n    print(line)"
    result = await hook(_state(_cell(code)), _runtime(database_gate=database_gate), _config())
    assert result is None


async def test_short_history_is_noop(reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate):
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    state = _state([ToolMessage(content="x", tool_call_id="c1", id="o1")])
    result = await hook(state, _runtime(database_gate=database_gate), _config())
    assert result is None


# ── assumed-persistence NameError hint ─────────────────────────────────────


async def test_nameerror_for_name_used_in_earlier_cell_hints(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    messages = _cell("cache = {'ready': True}", id_suffix="1") + _cell(
        "print(cache)", _nameerror_output("cache"), id_suffix="2"
    )
    result = await hook(_state(messages), _runtime(database_gate=database_gate), _config())

    assert result is not None
    [note] = result["messages"]
    assert note.content == (
        "[system] NameError: 'cache' appeared in an earlier execute_code call, "
        "but each call runs in a fresh interpreter — variables do not persist "
        "between calls. Re-define it here, or carry state via files or shell sessions."
    )
    assert result["meiva_sdk_reminder__reminded"] == {"nameerror:cache"}


async def test_nameerror_with_python_suggestion_suffix_hints(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    """Python may append a `Did you mean` clause to the NameError line; the
    stable `name 'X' is not defined` prefix still identifies the failure."""
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    output = _nameerror_output("listt") + ". Did you mean: 'list'?"
    messages = _cell("listt = [1]", id_suffix="1") + _cell("print(listt)", output, id_suffix="2")

    assert (
        await hook(_state(messages), _runtime(database_gate=database_gate), _config()) is not None
    )


async def test_nameerror_without_prior_whole_name_is_noop(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    """A substring in an earlier cell does not count, and the current cell is
    excluded from the search even though it necessarily contains the name."""
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    messages = _cell("cached_value = 1", id_suffix="1") + _cell(
        "print(cache)", _nameerror_output("cache"), id_suffix="2"
    )

    assert await hook(_state(messages), _runtime(database_gate=database_gate), _config()) is None


async def test_nameerror_hint_disabled_is_noop(
    reminder_plugin_loaded: Any, monkeypatch: pytest.MonkeyPatch, *, database_gate: ProcessDbGate
):
    from base.config import settings

    monkeypatch.setattr(settings.agent, "sdk_nameerror_hint_enabled", False)
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    messages = _cell("cache = 1", id_suffix="1") + _cell(
        "print(cache)", _nameerror_output("cache"), id_suffix="2"
    )

    assert await hook(_state(messages), _runtime(database_gate=database_gate), _config()) is None


async def test_repeated_nameerror_for_same_name_hints_once_per_window(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    first_messages = _cell("cache = 1", id_suffix="1") + _cell(
        "print(cache)", _nameerror_output("cache"), id_suffix="2"
    )
    first = await hook(_state(first_messages), _runtime(database_gate=database_gate), _config())
    assert first is not None

    second_messages = first_messages + _cell(
        "print(cache)", _nameerror_output("cache"), id_suffix="3"
    )
    second_state = _state(
        second_messages,
        meiva_sdk_reminder__reminded=first["meiva_sdk_reminder__reminded"],
        meiva_sdk_reminder__last_seen_compact=first["meiva_sdk_reminder__last_seen_compact"],
    )

    assert await hook(second_state, _runtime(database_gate=database_gate), _config()) is None


@pytest.mark.parametrize("name", ["len", "for"])
async def test_nameerror_hint_skips_builtins_and_keywords(
    reminder_plugin_loaded: Any, name: str, *, database_gate: ProcessDbGate
):
    hook = reminder_plugin_loaded.contribute().after_exec[0]
    messages = _cell(f"{name} = 1", id_suffix="1") + _cell(
        f"print({name})", _nameerror_output(name), id_suffix="2"
    )

    assert await hook(_state(messages), _runtime(database_gate=database_gate), _config()) is None


# ── the agent_reply matcher (inbound tail scan) ─────────────────────────────


def test_tail_has_agent_inbound_true():
    msgs: list[AnyMessage] = [AIMessage(content="prev", id="a0"), _agent_inbound(source="agent:9")]
    assert tail_has_agent_inbound(msgs) is True


def test_tail_has_agent_inbound_user_only_false():
    msgs: list[AnyMessage] = [
        AIMessage(content="prev", id="a0"),
        inbound_message(content="hi", source="user", inbound_id=2, body_start=0),
    ]
    assert tail_has_agent_inbound(msgs) is False


def test_tail_has_agent_inbound_stops_at_prior_ai():
    # An agent inbound BEFORE the most recent AIMessage is part of a prior turn
    # (already answered) -> not in the current incoming batch.
    msgs: list[AnyMessage] = [
        _agent_inbound(source="agent:3"),
        AIMessage(content="already answered", id="a1"),
        inbound_message(content="hi", source="user", inbound_id=2, body_start=0),
    ]
    assert tail_has_agent_inbound(msgs) is False


def test_tail_has_agent_inbound_ui_source_false():
    msgs: list[AnyMessage] = [
        AIMessage(content="prev", id="a0"),
        inbound_message(content="x", source="user", inbound_id=3, body_start=0),
    ]
    assert tail_has_agent_inbound(msgs) is False


# ── the before_llm hook (agent_reply) ───────────────────────────────────────


async def test_agent_reply_first_hit_injects_note(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    hook = reminder_plugin_loaded.sdk_reminder_agent_reply_before_llm
    state = _state([AIMessage(content="prev", id="a0"), _agent_inbound(source="agent:9")])
    result = await hook(state, _runtime(database_gate=database_gate), _config())

    assert result is not None
    [note] = result["messages"]
    assert note.additional_kwargs["meiva_msg_type"] == "system_note"
    assert note.additional_kwargs["meiva_note_tag"] == "agent_reply"
    assert note.additional_kwargs["meiva_created_at"]  # stamped with the inject time
    assert "meiva.agents.send_message" in note.content
    assert result["meiva_sdk_reminder__reminded"] == {AGENT_REPLY_CATEGORY}


async def test_agent_reply_second_hit_no_inject(
    reminder_plugin_loaded: Any, *, database_gate: ProcessDbGate
):
    hook = reminder_plugin_loaded.sdk_reminder_agent_reply_before_llm
    state = _state(
        [AIMessage(content="prev", id="a0"), _agent_inbound(source="agent:9")],
        meiva_sdk_reminder__reminded={AGENT_REPLY_CATEGORY},
    )
    result = await hook(state, _runtime(database_gate=database_gate), _config())
    assert result is None


# ── the agent_reply_reminder_cadence config ────────────────────────────────────


# ── defer-predicate parity with the real auto-compact gate ──────────────────


# ── real two-hook runner integration (both orderings) ───────────────────────


# ── exec-output left untouched (the hint is a separate note) ────────────────


# ── tail_has_agent_inbound shape coverage ───────────────────────────────────
