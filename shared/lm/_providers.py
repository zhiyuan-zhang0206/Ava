"""Cross-provider thinking configuration shared by factory and plugins.

Provider construction is plugin-owned. This module retains only the typed
thinking surface that callers pass through ``shared.lm.factory`` into plugin
build contexts.
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


class ThinkingConfig(TypedDict):
    """The Anthropic extended-thinking config passed to `build_chat_model`.

    `{"type": "disabled"}` turns thinking off (short-text paths); `{"type":
    "enabled", "budget_tokens": N}` turns it on with a token budget;
    `{"type": "adaptive"}` (adaptive-thinking claude models only) lets the
    model decide whether to think, with `display` choosing summarized text
    vs signature-only. Only the claude / deepseek branches pass the dict
    through on the wire; every other branch reads `type` and mirrors
    disabled onto its own switch (the provider plugins own those switches);
    kimi logs a warning instead.
    """

    type: Literal["enabled", "disabled", "adaptive"]
    budget_tokens: NotRequired[int]
    display: NotRequired[Literal["summarized", "omitted"]]
