"""Cross-provider reasoning-effort vocabulary and clamping.

Split out of `shared/lm/factory.py` (its companion module — factory re-exports
these names, so callers and tests keep importing from factory). Provider plugins
own endpoint vocabularies and wire switches. This module keeps only the shared
``AVA_REASONING_EFFORT`` vocabulary, its public SDK enum/coercion surface, and
the clamp that maps a cross-provider value onto a plugin's declared levels.
"""

from __future__ import annotations

from enum import StrEnum

from loguru import logger

# Core owns no provider-specific effort vocabularies; plugins declare them on
# their bindings. The empty compatibility table remains as a cross-provider
# import surface.
_PROVIDER_EFFORT_LEVELS: dict[str, tuple[str, ...]] = {}

# The cross-provider AVA_REASONING_EFFORT vocabulary, ordered weakest →
# strongest. Superset of the public `ReasoningEffort` enum — the extra
# "minimal" is a gemini-only thinking_level that some paths still accept as
# input. _clamp_effort maps a value onto what a provider actually accepts;
# anything outside this vocabulary is a typo and fails fast.
_EFFORT_VOCAB: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


class ReasoningEffort(StrEnum):
    """Public reasoning-effort levels for `ava.understand` / `ava.web.fetch`.

    Ordered weakest → strongest. `minimal` is deliberately absent — it is a
    gemini-only `thinking_level`, not a cross-provider effort level. Members
    are plain `str`, so a member and its literal value are interchangeable.
    """

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


def coerce_effort(effort: str | ReasoningEffort | None, *, example: str) -> ReasoningEffort | None:
    """Normalize the public `effort` knob onto the enum; None passes through.

    The one effort contract for the SDK batch entry points (`ava.web.fetch` /
    `ava.understand`): a `ReasoningEffort` member or its literal string value
    normalizes to the enum; None (fetch's "keep the settings default") passes
    through. `example` names the calling API in error messages.

    Raises:
        TypeError: `effort` is neither a string nor a `ReasoningEffort` member.
        ValueError: a string outside the public vocabulary. `minimal` is
            gemini-internal (a `thinking_level`, not a cross-provider effort
            level) and is rejected here — it is not a `ReasoningEffort` member.
    """
    if effort is None:
        return None
    if not isinstance(effort, (str, ReasoningEffort)):
        raise TypeError(
            f"effort must be a ReasoningEffort member or one of "
            f"{'/'.join(e.value for e in ReasoningEffort)}, got {type(effort).__name__}. "
            f"Example: {example}"
        )
    try:
        return ReasoningEffort(effort)
    except ValueError:
        raise ValueError(
            f"unknown effort {effort!r} — expected one of "
            f"{'/'.join(e.value for e in ReasoningEffort)}. "
            f"Example: {example}"
        ) from None


def _clamp_effort(effort: str, allowed: tuple[str, ...], *, target: str) -> str:
    """Clamp a cross-provider effort value onto the levels `target` accepts.

    In-range values pass through. Known-vocabulary values outside the range
    clamp to the nearest allowed level, ties rounding up — reproducing
    DeepSeek's server-side precedent (low/medium→high, xhigh→max). Unknown
    strings raise: a typo'd AVA_REASONING_EFFORT should explode at build time,
    not as a provider 400 after the agent is already running.
    """
    if effort in allowed:
        return effort
    if effort not in _EFFORT_VOCAB:
        raise ValueError(
            f"unknown reasoning effort {effort!r} — expected one of {'/'.join(_EFFORT_VOCAB)} "
            f"(or empty for the provider default)"
        )
    idx = _EFFORT_VOCAB.index(effort)
    clamped = min(
        allowed,
        key=lambda a: (abs(_EFFORT_VOCAB.index(a) - idx), -_EFFORT_VOCAB.index(a)),
    )
    logger.info(
        "reasoning effort {effort!r} not supported by {target}; clamped to {clamped!r}",
        effort=effort,
        target=target,
        clamped=clamped,
    )
    return clamped
