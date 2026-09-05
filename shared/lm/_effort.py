"""Per-provider reasoning-effort dispatch for build_chat_model.

Split out of `shared/lm/factory.py` (its companion module — factory re-exports
these names, so callers and tests keep importing from factory). Per-MODEL facts
(output caps, per-model effort vocabularies, the extended-thinking-only flag)
live in `shared/lm/registry.py`; this module keeps the per-PROVIDER wire
concerns:

- **Effort dispatch** (`_clamp_effort` + `_PROVIDER_EFFORT_LEVELS`): the
  cross-provider `AVA_REASONING_EFFORT` vocabulary is mapped onto what each
  provider's endpoint actually accepts; out-of-range values clamp (logged),
  unknown strings fail fast at build time instead of surfacing as a provider
  400 after the agent is already running.
- **Binary-thinking resolver** (`mimo_extra_body`): for a provider with no
  graded effort field, only a thinking on/off switch, this folds the clamp +
  switch-selection logic that build_chat_model's branch would otherwise inline.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from loguru import logger

# Per-provider clamp targets for the cross-provider AVA_REASONING_EFFORT knob
# on the remaining core OpenAI-style branches. mimo's official chat-completions
# reference documents no reasoning_effort field, only a body-level
# thinking.type enabled/disabled toggle — so its tuple is a binary
# "none"/"high": "none" sends thinking.type=disabled, anything else leaves the
# provider default (thinking already on) untouched. Keyed by provider because
# the vocabulary is an endpoint contract shared by every model of the
# provider.
_PROVIDER_EFFORT_LEVELS: dict[str, tuple[str, ...]] = {
    "kimi": ("low", "high", "max"),
    "glm": ("low", "high", "max"),
    "mimo": ("none", "high"),
}

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


def mimo_extra_body(*, thinking: Mapping[str, Any] | None, reasoning_effort: str) -> dict[str, Any]:
    """Resolve the mimo branch's `extra_body` kwarg.

    Caller-explicit `thinking={"type":"disabled"}` (short-text paths) wins
    outright. Otherwise `reasoning_effort` clamped onto
    `_PROVIDER_EFFORT_LEVELS["mimo"]` toggles the same body switch: "none"
    disables thinking, "high" is the provider default (already on — nothing
    to send). Empty dict = no override, provider default applies.
    """
    if thinking is not None and thinking.get("type") == "disabled":
        return {"thinking": {"type": "disabled"}}
    if reasoning_effort:
        tier = _clamp_effort(reasoning_effort, _PROVIDER_EFFORT_LEVELS["mimo"], target="mimo")
        if tier == "none":
            return {"thinking": {"type": "disabled"}}
    return {}
