"""Tests for plugins/ava_syntax_fix/plugin.py.

Coverage:
- _fix_chinese_punctuation: convert Chinese punctuation to ASCII
- _detect_missing_imports: detect missing imports
- _insert_imports: insert import statements at the correct position
- _ruff_fix: ruff check --fix (mock subprocess)
- syntax_fix_before_exec: full pipeline (Chinese punctuation → import → ruff → compile)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.state import AgentState
from ava_builtins.plugins.ava_syntax_fix.agent_runtime import (
    syntax_fix_before_exec,
)
from base.agents.context import AvaContext
from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices

# --- _fix_chinese_punctuation ---


# --- _translate_outside_strings crash guard (gh-149183) ---


# --- _detect_missing_imports ---


# --- _insert_imports ---


# --- _ruff_fix ---


# --- _ruff_format ---


# --- ruff give-up logging (issue #159) ---
# A ruff pass that gives up must be visible: a timeout / OS error logs a
# warning with the budget, input size, and errno; a missing ruff logs once per
# process. The pass-through behavior itself is unchanged.


# --- _ruff_executable ---


# ============================================================================
# _fix_invalid_escapes
# ============================================================================


# ============================================================================
# syntax_fix_before_exec integration
# ============================================================================


# ============================================================================
# _is_stdlib_module
# ============================================================================


# LLM repair helpers
# ============================================================================


# ============================================================================
# Deterministic fixes tests (syntax-fix v3)
# ============================================================================


def _is_broken(code: str) -> bool:
    """True when `code` fails to compile -- used to assert a fixture is a
    genuine syntax error before a fixer is run against it."""
    try:
        compile(code, "<t>", "exec")
    except SyntaxError:
        return True
    return False


# ============================================================================
# trace-v2 syntax_fix events (task #792 group B)
# ============================================================================


class TestSyntaxFixEvents:
    """The before/after retention events: rough fix stores before only (after
    is replayable), lm fix stores before AND after (LLM rewrites are not
    replayable). Both go out as event="syntax_fix" loguru records whose extra
    lands in the event's `attributes`."""

    @staticmethod
    def _runtime(database_gate: ProcessDbGate):
        ctx = AvaContext(
            ops_pool=AsyncMock(),
            llm=MagicMock(),
            agent=AgentSlices.resolve(
                default_reader=lambda domain, field: getattr(getattr(settings, domain), field),
            ),
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(),
        )
        return Runtime(context=ctx)

    @staticmethod
    def _config() -> RunnableConfig:
        return {"configurable": {"thread_id": "7"}}

    async def test_rough_fix_emits_before_only(
        self, monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
    ):
        """A deterministic (rough) fix records fix_type=rough with the original
        source as `before` and no `after` — the after state is replayable via
        _apply_fix_pipeline(before)."""
        from ava_builtins.plugins.ava_syntax_fix import agent_runtime as _plugin

        events: list[dict] = []
        monkeypatch.setattr(_plugin, "_emit_syntax_fix_event", lambda **kw: events.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        # Chinese comma -> deterministic fix fires
        original = "print(1\uff0c2)"
        state = AgentState(
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute_code",
                            "args": {"code": original},
                            "id": "1",
                        }
                    ],
                )
            ]
        )
        result = await syntax_fix_before_exec(
            state, self._runtime(database_gate=database_gate), self._config()
        )
        assert result is not None
        assert len(events) == 1  # pyright: ignore[reportUnknownArgumentType]
        ev = events[0]
        assert ev["fix_type"] == "rough"
        assert ev["before"] == original
        assert ev["after"] is None
        assert ev["fixes"], "deterministic fixes must be listed"
        assert "chinese_punct" in ",".join(ev["fixes"])  # pyright: ignore[reportUnknownArgumentType]

    async def test_rough_fix_event_logger_payload_shape(self, monkeypatch: pytest.MonkeyPatch):
        """The real emit path: a loguru record with event='syntax_fix' whose
        extra carries fix_type / before / fixes — the shape the event's
        `attributes` stores."""
        from ava_builtins.plugins.ava_syntax_fix import agent_runtime as _plugin

        captured: dict = {}
        monkeypatch.setattr(_plugin.logger, "info", lambda _msg, **kw: captured.update(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        _plugin._emit_syntax_fix_event(
            fix_type="rough",
            before="print(1\uff0c2)",
            after=None,
            fixes=["chinese_punct(1)"],
            note="deterministic fixes applied",
        )
        assert captured["event"] == "syntax_fix"
        assert captured["fix_type"] == "rough"
        assert captured["before"] == "print(1\uff0c2)"
        assert captured["fixes"] == ["chinese_punct(1)"]
        assert "after" not in captured

    async def test_lm_fix_emits_before_and_after(
        self, monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
    ):
        """An LLM repair records fix_type=lm with both before (the
        deterministic-fixed source the LLM saw) and after (the repair)."""
        from ava_builtins.plugins.ava_syntax_fix import agent_runtime as _plugin

        events: list[dict] = []
        monkeypatch.setattr(_plugin, "_emit_syntax_fix_event", lambda **kw: events.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        broken = "x = 'unterminated\nprint(x)"
        repaired = "x = 'fixed'\nprint(x)"
        with patch(
            "ava_builtins.plugins.ava_syntax_fix.agent_runtime._llm_repair_syntax",
            new=AsyncMock(return_value=repaired),
        ):
            result = await syntax_fix_before_exec(
                TestSyntaxFixEvents._broken_state(broken),
                self._runtime(database_gate=database_gate),
                self._config(),
            )
        assert result is not None
        assert len(events) == 1  # pyright: ignore[reportUnknownArgumentType]
        ev = events[0]
        assert ev["fix_type"] == "lm"
        assert ev["before"] == broken  # no deterministic fix fired -> LLM saw the original
        assert ev["after"] == repaired

    @staticmethod
    def _broken_state(code: str) -> AgentState:
        return AgentState(
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[{"name": "execute_code", "args": {"code": code}, "id": "1"}],
                )
            ]
        )

    async def test_no_event_when_nothing_changed(
        self, monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
    ):
        """Clean code that compiles as-is emits no syntax_fix event — the event
        stream records mutations only."""
        from ava_builtins.plugins.ava_syntax_fix import agent_runtime as _plugin

        events: list[dict] = []
        monkeypatch.setattr(_plugin, "_emit_syntax_fix_event", lambda **kw: events.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        # Force the pipeline to be a no-op (nothing to fix, nothing formatted)
        # so the event decision is what is under test — not ruff's formatting.
        monkeypatch.setattr(_plugin, "_apply_fix_pipeline", lambda code, **_kw: (code, []))  # pyright: ignore[reportUnknownArgumentType]
        state = AgentState(
            messages=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute_code",
                            "args": {"code": "x = 1\nprint(x)"},
                            "id": "1",
                        }
                    ],
                )
            ]
        )
        await syntax_fix_before_exec(
            state, self._runtime(database_gate=database_gate), self._config()
        )
        assert events == []
