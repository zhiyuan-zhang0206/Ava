"""Per-model context budget — the model's context-window ceiling plus the soft
(reminder) and hard (force-compact) thresholds derived from it.

The hard threshold is ``min(fraction * window, ceiling)``. All three factors are
per-model: the window is the model's registry fact (``ModelSpec.context_window``),
and the fraction and ceiling resolve through the per-model config layering
(``resolve_setting``: code shared default 0.4 / 0.3 / no-ceiling < the model's
registry tuning default < an explicit ``AVA_AUTO_COMPACT_FRACTION`` /
``AVA_COMPACT_REMINDER_FRACTION`` / ``AVA_AUTO_COMPACT_CEILING_TOKENS`` env value
< the per-agent overlay). No per-model threshold table for operators to maintain:
a model added to the registry gets correct thresholds for free, and one env value
still pins the whole cluster when the user says so.

The roster runs one flat rule — force-compact at 40% of the model's own window,
remind at 30% — so no registry entry carries a compact fraction or ceiling of its
own and every model's thresholds are those two percentages of its own
``context_window`` (``decisions/2026-07-31-flat-compact-thresholds.md``; the
per-model evidence the earlier tiers were built from is kept in
``decisions/2026-07-25-per-model-tuning-values.md``). The two deepseek
entries are the exception: a user decision (task #581) pins them at soft 374k /
hard 512k (0.374 / 0.512 of their 1M window — see
``decisions/2026-08-01-deepseek-compact-thresholds.md``).

**Why the ceiling knob still exists.** A fraction tracks the ADVERTISED window,
and advertised windows grew roughly 8x (128K -> 1M) while measured effective
context did not move with them, so one fraction means a different absolute
budget on a 200K model than on a 1M one, and every vendor window inflation
loosens it again. The ceiling is the escape hatch for pinning an absolute
trigger — per model in the registry, or cluster-wide via
``AVA_AUTO_COMPACT_CEILING_TOKENS``. When it binds it scales the soft threshold
by the same factor it scales the hard one, so the reminder keeps its
proportional lead instead of collapsing onto the forced ceiling. Ceiling 0 = no
cap, which is what the whole roster runs today.

(The design before fractions compared a chars/4 estimate against a single
absolute token count — ``AVA_AUTO_COMPACT_TOKENS`` = 800K — which was silently
unreachable on a 200K-window model, so compaction never fired and the model
overflowed. The ceiling here is per-model and paired with the fraction, so a
small-window model can never lose its threshold to it.)

``latest_input_tokens`` reads context occupancy as the provider truth (the most
recent LLM call's ``input_tokens``), the unit the frontend gauge already shows —
so the compact trigger, the gauge, and the reported thresholds are all one unit
(the ``Option Y`` caliber unification: this shifts when compaction fires from a
chars/4 estimate of the *current* context to the real token count measured on the
*previous* call — see the compact hook docstring).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage

from shared.lm.registry import MODELS, resolve_setting


class UnknownModelWindowError(ValueError):
    """The model has no registry ``context_window`` fact, so its compaction
    thresholds cannot be derived. Every spawnable model has one (registry
    import-time invariant), so this only fires on a registry gap — a developer
    added a model without a window. The agent compact hook lets it surface (an
    agent whose window we do not know cannot be protected from overflow); the
    gateway display endpoints catch it and degrade to zeros (a read endpoint
    must not 500 over a registry gap)."""


@dataclass(frozen=True)
class ContextBudget:
    """A model's context-window ceiling and the two compaction thresholds
    derived from it, all as absolute token counts."""

    max_context_tokens: int  # the model's full context window (ModelSpec.context_window)
    soft_compact_tokens: int  # wind-down reminder threshold
    hard_compact_tokens: int  # force-compact ceiling


def resolve_context_budget(model: str) -> ContextBudget:
    """The context budget for ``model``: its window, plus the hard threshold
    ``min(auto_compact_fraction * window, auto_compact_ceiling_tokens)`` and the
    soft threshold scaled by the same factor the ceiling applied.

    Raises:
        UnknownModelWindowError: ``model`` has no registry ``context_window``.
    """
    spec = MODELS.get(model)
    if spec is None or spec.context_window is None:
        raise UnknownModelWindowError(
            f"model {model!r} has no context_window in the model registry — add "
            f"it in shared/lm/registry.py:MODELS so its compaction thresholds "
            f"can be derived"
        )
    window = spec.context_window
    soft_fraction: float = resolve_setting("compact_reminder_fraction", model=model)
    hard_fraction: float = resolve_setting("auto_compact_fraction", model=model)
    ceiling: int = resolve_setting("auto_compact_ceiling_tokens", model=model)
    hard_tokens = round(hard_fraction * window)
    soft_tokens = round(soft_fraction * window)
    if 0 < ceiling < hard_tokens:
        # Compress BOTH thresholds by the same factor: capping only the hard one
        # would let the reminder land above the forced ceiling (it would never
        # fire, or fire in the same turn as the compaction it exists to avoid).
        soft_tokens = round(soft_tokens * ceiling / hard_tokens)
        hard_tokens = ceiling
    return ContextBudget(
        max_context_tokens=window,
        soft_compact_tokens=soft_tokens,
        hard_compact_tokens=hard_tokens,
    )


def latest_input_tokens(messages: Sequence[BaseMessage]) -> int | None:
    """The ``input_tokens`` of the most recent ``AIMessage`` carrying
    ``usage_metadata`` — the provider-truth measure of context-window
    occupancy — scanning newest-first.

    Returns ``None`` when no LLM call has completed yet (a just-spawned agent's
    first turn, or immediately after a compaction removed the prior AIMessages):
    the caller falls back to a chars/4 estimate, which only matters while the
    context is trivially small.
    """
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            usage = msg.usage_metadata
            if usage:
                return usage["input_tokens"]
    return None
