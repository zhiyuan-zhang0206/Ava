from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.messages import NoteTag, read_meiva_kwargs
from agent.state import CompactState, build_agent_state
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.agents.to_user.models import DOCUMENT_FILENAME
from base.config import settings
from base.host.env.agent_slices import AgentSlices
from base.log.tests.log_records import LogRecords
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths import workspace_dir
from meiva.tests.fixtures.pin_agent import pin_agent
from meiva_builtins.plugins.meiva_to_user import agent_runtime
from meiva_builtins.plugins.meiva_to_user._state import OVER_LIMIT

_AGENT = 17
_WRITTEN = "meiva_to_user__panel_written"
_REMINDED = "meiva_to_user__reminded"
_BOOKMARK = "meiva_to_user__last_seen_compact"
_OVER = "one two three four five six"


def _default_reader(domain: str, field: str) -> Any:
    return getattr(getattr(settings, domain), field)


def _state(
    *,
    panel_written: bool = True,
    reminded: set[str] | None = None,
    bookmark: int = 0,
    compact_version: int = 0,
) -> Any:
    registry = ExtensionRegistry((("meiva_to_user", agent_runtime.contribute()),))
    recorded: dict[str, Any] = {
        _WRITTEN: panel_written,
        _REMINDED: set(reminded or ()),
        _BOOKMARK: bookmark,
    }
    return build_agent_state(registry)(
        messages=[],
        **recorded,
        compact=CompactState(version=compact_version),
    )


def _runtime(pins: dict[str, Any] | None = None) -> Runtime[MeivaContext]:
    context = MeivaContext(
        identity=AgentIdentity(agent_id=_AGENT, owns_loop=True),
        agent=AgentSlices.resolve(pins=pins, default_reader=_default_reader),
    )
    return Runtime(context=context)


def _write_panel(text: str) -> None:
    workspace = workspace_dir(_AGENT)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / DOCUMENT_FILENAME).write_text(text, encoding="utf-8")


@pytest.fixture
def agent(unit_home: Path) -> int:
    pin_agent(_AGENT)
    return _AGENT


@pytest.fixture
def hook() -> Any:
    return agent_runtime.to_user_limit_after_exec


def _note(result: dict[str, Any] | None) -> Any:
    assert result is not None
    [note] = result.get("messages", [])
    return note


async def test_no_recorded_write_is_a_noop(hook: Any, agent: int) -> None:
    assert await hook(_state(panel_written=False), _runtime(), RunnableConfig()) is None


async def test_a_panel_within_the_limit_adds_no_reminder(hook: Any, agent: int) -> None:
    _write_panel("short and scannable")

    result = await hook(_state(), _runtime(), RunnableConfig())

    assert result is not None
    assert "messages" not in result
    assert result[_WRITTEN] is False


async def test_an_over_limit_panel_gets_one_reminder_with_both_figures(
    hook: Any, agent: int
) -> None:
    _write_panel(_OVER)

    result = await hook(_state(), _runtime({"to_user_soft_limit_words": 5}), RunnableConfig())

    note = _note(result)
    assert read_meiva_kwargs(note).get("meiva_note_tag") == NoteTag.TO_USER_SOFT_LIMIT.value
    text = str(note.content)
    assert "6 words" in text
    assert "soft limit is 5" in text
    assert "Links and images" in text
    assert result[_WRITTEN] is False
    assert result[_REMINDED] == {OVER_LIMIT}


async def test_the_default_limit_covers_a_small_panel(hook: Any, agent: int) -> None:
    _write_panel(_OVER)

    result = await hook(_state(), _runtime(), RunnableConfig())

    assert result is not None
    assert "messages" not in result


async def test_once_per_compaction_stays_silent_in_the_same_window(hook: Any, agent: int) -> None:
    _write_panel(_OVER)

    result = await hook(
        _state(reminded={OVER_LIMIT}), _runtime({"to_user_soft_limit_words": 5}), RunnableConfig()
    )

    assert result is not None
    assert "messages" not in result
    assert result[_REMINDED] == {OVER_LIMIT}
    assert result[_BOOKMARK] == 0


async def test_once_per_compaction_rearms_after_a_compaction(hook: Any, agent: int) -> None:
    _write_panel(_OVER)

    result = await hook(
        _state(reminded={OVER_LIMIT}, bookmark=0, compact_version=1),
        _runtime({"to_user_soft_limit_words": 5}),
        RunnableConfig(),
    )

    note = _note(result)
    assert "6 words" in str(note.content)
    assert result[_BOOKMARK] == 1
    assert result[_REMINDED] == {OVER_LIMIT}


async def test_every_time_reminds_on_every_over_limit_write(hook: Any, agent: int) -> None:
    _write_panel(_OVER)

    result = await hook(
        _state(reminded={OVER_LIMIT}),
        _runtime({"to_user_soft_limit_words": 5, "to_user_limit_reminder_cadence": "every_time"}),
        RunnableConfig(),
    )

    assert _note(result) is not None
    assert result[_REMINDED] == {OVER_LIMIT}


async def test_coming_back_within_the_limit_rearms_the_reminder(hook: Any, agent: int) -> None:
    _write_panel("short again")

    result = await hook(
        _state(reminded={OVER_LIMIT}), _runtime({"to_user_soft_limit_words": 5}), RunnableConfig()
    )

    assert result is not None
    assert "messages" not in result
    assert result[_REMINDED] == set()


async def test_eval_isolation_skips_the_check(hook: Any, agent: int) -> None:
    _write_panel(_OVER)

    result = await hook(
        _state(),
        _runtime({"to_user_soft_limit_words": 5, "eval_isolation": True}),
        RunnableConfig(),
    )

    assert result is not None
    assert "messages" not in result
    assert result[_WRITTEN] is False


async def test_an_unreadable_panel_logs_and_stays_silent(
    hook: Any, agent: int, tmp_path: Path, loguru_records: LogRecords
) -> None:
    workspace = workspace_dir(_AGENT)
    workspace.rmdir()
    workspace.symlink_to(tmp_path / "elsewhere")

    result = await hook(_state(), _runtime({"to_user_soft_limit_words": 5}), RunnableConfig())

    assert result is not None
    assert "messages" not in result
    assert (
        "to_user soft limit: the panel could not be read; no reminder"
        in loguru_records.messages("WARNING")
    )


async def test_the_reminder_count_is_the_shared_word_metric(hook: Any, agent: int) -> None:
    from base.packages.text_budget import count_words

    panel = "\u4eca\u5929\u5b8c\u6210\u4e86 3 \u9879\u4efb\u52a1, see report.md for details. " * 40
    _write_panel(panel)

    result = await hook(_state(), _runtime({"to_user_soft_limit_words": 5}), RunnableConfig())

    note = _note(result)
    assert f"panel is at {count_words(panel)} words" in str(note.content)
