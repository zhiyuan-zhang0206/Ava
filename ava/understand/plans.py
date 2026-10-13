"""Validate the arguments of `ava.understand` and turn them into a `Plan`, with no ambient reads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ava.sdk_surface.batch import validate_max_concurrent
from ava.sdk_surface.validation import coerce_str
from base.lm.effort import ReasoningEffort, coerce_effort

__all__ = ["Plan", "normalize"]


@dataclass(frozen=True)
class Plan:
    """What one `ava.understand` call answers: its targets, reasoning effort and concurrency."""

    targets: list[dict[str, Any]]
    effort: str | ReasoningEffort
    max_concurrent: int | None


def normalize(
    targets: list[dict[str, Any]], effort: str | ReasoningEffort, max_concurrent: int | None
) -> Plan:
    """Check the targets and coerce the effort; raise on the first invalid input."""
    if not isinstance(targets, list):
        raise TypeError(
            f"understand() takes a list of target dicts, got {type(targets).__name__}. "
            "Example: ava.understand([{'prompt': 'summarize this', 'paths': ['notes.md']}])"
        )
    for i, t in enumerate(targets):
        if not isinstance(t, dict):
            raise TypeError(
                f"targets[{i}] must be a dict, got {type(t).__name__}. "
                "Example: {'prompt': 'what is in this image', 'paths': ['shot.png']}"
            )
        if "prompt" not in t:
            raise ValueError(
                f"targets[{i}] missing required key 'prompt'. "
                "Example: {'prompt': 'summarize this', 'text': 'the material'}"
            )
        srcs = [k for k in ("text", "paths") if k in t]
        if len(srcs) != 1:
            raise ValueError(
                f"targets[{i}] must have exactly one of 'text' / 'paths', got {srcs}. "
                "Example: {'prompt': 'summarize this', 'paths': ['notes.md']}"
            )
        if "paths" in t:
            paths = t["paths"]
            if not isinstance(paths, list):
                raise TypeError(
                    f"targets[{i}]['paths'] must be a list of file paths, "
                    f"got {type(paths).__name__}. "
                    "Example: {'prompt': 'compare these', "
                    "'paths': ['design.png', 'shot.png']}"
                )
            if not paths:
                raise ValueError(
                    f"targets[{i}]['paths'] must not be empty — pass at least one file. "
                    "Example: {'prompt': 'compare these', "
                    "'paths': ['design.png', 'shot.png']}"
                )
            for j, p in enumerate(cast(list[Any], paths)):
                if not isinstance(p, (str, Path)):
                    raise TypeError(
                        f"targets[{i}]['paths'][{j}] must be a path string or Path, "
                        f"got {type(p).__name__}"
                    )
    effort = coerce_str(effort, "effort")
    normalized = coerce_effort(effort, example="ava.understand(targets, effort='low')")
    assert normalized is not None  # noqa: S101 — understand's default is MAX, never None
    validate_max_concurrent(max_concurrent, example="ava.understand(targets, max_concurrent=4)")
    return Plan(targets=targets, effort=normalized, max_concurrent=max_concurrent)
