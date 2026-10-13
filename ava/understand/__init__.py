"""Understand almost any input — text, images, video, audio, PDF — by asking a
question about it.
"""

from __future__ import annotations

__all_for_ava__ = [
    "UnderstandError",
    "understand",
]

from typing import overload

from ava.sdk_surface.batch import DEFAULT_BATCH_MAX_CONCURRENT
from ava.understand import plans
from ava.understand.errors import UnderstandError
from base.lm.effort import ReasoningEffort


@overload
def understand(
    targets: list[dict[str, str]],
    effort: str | ReasoningEffort = ReasoningEffort.MAX,
    max_concurrent: int | None = DEFAULT_BATCH_MAX_CONCURRENT,
) -> list[str]: ...


@overload
def understand(
    targets: list[dict[str, str | list[str]]],
    effort: str | ReasoningEffort = ReasoningEffort.MAX,
    max_concurrent: int | None = DEFAULT_BATCH_MAX_CONCURRENT,
) -> list[str]: ...


def understand[TargetValue: str | list[str]](
    targets: list[dict[str, TargetValue]],
    effort: str | ReasoningEffort = ReasoningEffort.MAX,
    max_concurrent: int | None = DEFAULT_BATCH_MAX_CONCURRENT,
) -> list[str]:
    """Answer a prompt about each target in parallel.

    Each target is a dict with `prompt` plus exactly one of `text` / `paths`.
    `paths` is a non-empty list of file paths (text, image, video, audio, PDF),
    sent together in one model call; any media file routes the call to the media model.

    `effort` (`none` … `max`, default `max`) sets the answering model's reasoning
    depth; `max_concurrent` (default 12) caps parallel targets. Results are
    auto-saved to `.exec_output/` in your workspace.
    Returns:
        One answer per target, in input order.
    """
    plan = plans.normalize(targets, effort, max_concurrent)
    # Imported here: `import ava` loads this module, and the provider stack is not needed until
    # a call is made.
    from ava.understand import answer, wiring

    return answer.run(plan, wiring.understand_deps())


# Hang the exception class off the function so agents can catch it as
# `ava.understand.UnderstandError` — `ava.understand` is the top-level function,
# there is no `ava.understand` submodule to host the class. Same for the effort
# enum, so agents can pass `ava.understand.ReasoningEffort.LOW` without knowing
# the shared module path.
understand.UnderstandError = UnderstandError  # type: ignore[attr-defined]
understand.ReasoningEffort = ReasoningEffort  # type: ignore[attr-defined]
