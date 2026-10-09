"""The chunk size is a ratio of the agent model's soft compaction threshold, one definition for all callers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage

from agent.hooks import understanding_chunks as uc
from agent.state_channels import CompactState
from base.agents.history.hierarchy.chunk_plan import plan_replay
from base.agents.history.hierarchy.chunks import chunk_threshold
from base.config import settings
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.context_budget import UnknownModelWindowError, resolve_context_budget

_FLASH = "deepseek-flash"
_HAIKU = "claude-haiku-4-5-20251001"


def test_the_threshold_is_the_ratio_of_each_models_soft_threshold(
    *, model_catalog: ModelCatalog
) -> None:
    for model in (_FLASH, _HAIKU):
        soft = resolve_context_budget(
            model, ModelOverrides.from_pins({}), catalog=model_catalog
        ).soft_compact_tokens
        assert chunk_threshold(
            model, ModelOverrides.from_pins({}), 0.5, catalog=model_catalog
        ) == round(0.5 * soft)
        assert (
            chunk_threshold(model, ModelOverrides.from_pins({}), 1.0, catalog=model_catalog) == soft
        )  # the ratio's upper bound: one soft threshold
    assert chunk_threshold(
        _FLASH, ModelOverrides.from_pins({}), 0.5, catalog=model_catalog
    ) != chunk_threshold(_HAIKU, ModelOverrides.from_pins({}), 0.5, catalog=model_catalog)
    assert (
        chunk_threshold(_FLASH, ModelOverrides.from_pins({}), 0.5, catalog=model_catalog) == 187_000
    )  # the aligned figure for DeepSeek


def test_the_agents_own_soft_threshold_override_wins(*, model_catalog: ModelCatalog) -> None:
    overrides = ModelOverrides.from_pins({"compact_reminder_fraction": 0.2})
    assert (
        chunk_threshold(_FLASH, overrides, 1.0, catalog=model_catalog)
        == resolve_context_budget(_FLASH, overrides, catalog=model_catalog).soft_compact_tokens
    )
    assert chunk_threshold(_FLASH, overrides, 1.0, catalog=model_catalog) < chunk_threshold(
        _FLASH, ModelOverrides.from_pins({}), 1.0, catalog=model_catalog
    )


def test_a_tiny_ratio_never_gives_a_zero_threshold(*, model_catalog: ModelCatalog) -> None:
    assert chunk_threshold(_FLASH, ModelOverrides.from_pins({}), 1e-12, catalog=model_catalog) == 1


def test_an_unknown_model_fails_loudly(*, model_catalog: ModelCatalog) -> None:
    with pytest.raises(UnknownModelWindowError):
        chunk_threshold("no-such-model", ModelOverrides.from_pins({}), 0.5, catalog=model_catalog)


@pytest.mark.parametrize("ratio", [0, -0.1, 1.01])
def test_the_ratio_setting_is_bounded_to_zero_exclusive_one_inclusive(ratio: float) -> None:
    field = type(settings.agent).model_fields[
        "AVA_UNDERSTANDING_CHUNK_RATIO" and "understanding_chunk_ratio"
    ]
    assert field.default == 0.5
    with pytest.raises(ValueError):
        type(settings.agent).model_validate({"AVA_UNDERSTANDING_CHUNK_RATIO": ratio})
    type(settings.agent).model_validate({"AVA_UNDERSTANDING_CHUNK_RATIO": 1})


async def test_the_hook_and_the_build_cut_the_same_chunks(
    monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    """The hook's turn-by-turn cuts equal `plan_replay` at the same `chunk_threshold`."""
    monkeypatch.setattr(settings.agent, "understanding_enabled", True)
    ratio = 0.002  # a real model, a small ratio: the threshold is about 750 tokens
    monkeypatch.setattr(settings.agent, "understanding_chunk_ratio", ratio)
    threshold = chunk_threshold(_FLASH, ModelOverrides.from_pins({}), ratio, catalog=model_catalog)
    msgs: list[AnyMessage] = [SystemMessage(content="head", id="h")]
    for i in range(40):
        msgs.append(HumanMessage(content=f"u{i}", id=f"u{i}"))
        tokens = 2000 + 130 * i
        msgs.append(
            AIMessage(
                content="x",
                id=f"a{i}",
                usage_metadata={"input_tokens": tokens, "output_tokens": 1, "total_tokens": tokens},
            )
        )
    enqueued: list[tuple[int, int]] = []

    async def fake_enqueue(_pool: object, _agent: int, **kwargs: object) -> bool:
        enqueued.append((kwargs["chunk"].start_index, kwargs["chunk"].end_index))  # type: ignore[attr-defined]
        return True

    monkeypatch.setattr(uc, "enqueue_chunk", fake_enqueue)
    compact = CompactState()
    for i, msg in enumerate(msgs):
        if isinstance(msg, AIMessage):
            update = await uc.due_chunk_update(
                compact,
                msgs[:i],
                msg,
                pool=MagicMock(),
                agent_id=1,
                model=_FLASH,
                overrides=ModelOverrides.from_pins({}),
                catalog=model_catalog,
            )
            compact = update.get("compact", compact)
    live = [
        (p.chunk.start_index, p.chunk.end_index)
        for p in plan_replay(msgs, threshold=threshold)
        if not p.closing
    ]
    assert len(live) > 3 and live == enqueued
