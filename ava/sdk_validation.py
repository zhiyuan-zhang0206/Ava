"""Unified SDK argument validation — one implementation for every entry point.

LLM-generated calls routinely write a parenthesized adjacent-string group with
a trailing comma — `("line1" "line2",)` — which evaluates to a one-element
tuple. The SDK serialized it as a JSON array, and the gateway's string wire
fields rejected the call with a bare 422 (issue #1343; send_message,
2026-08-28, agents 2697/2986). Per the user ruling (2026-08-28) the SDK
validates its own arguments instead:

- A string-expected argument (`coerce_str`) unwraps a one-element list/tuple
  whose element is a string — the trailing-comma class. Multi-element
  sequences, sequences whose element is not a string, and any other type raise
  TypeError naming the parameter, so the mistake fails loud at the SDK
  boundary instead of as a bare 422 or a silent no-op.
- An argument that is inherently not a string (`coerce_typed`) is checked
  strictly against its declared type and never unwrapped — a one-element
  tuple for a list/int/dict parameter stays a TypeError.
"""

from __future__ import annotations

from typing import Any


def _type_names(expected: type | tuple[type, ...]) -> str:
    """Render a type union as an error-message phrase ("int", "int or None",
    "int, float, or str")."""
    types = expected if isinstance(expected, tuple) else (expected,)
    names = [t.__name__ for t in types]
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" or {names[-1]}"


def coerce_str(
    value: object,
    name: str,
    *,
    allow_none: bool = False,
    allow_types: tuple[type, ...] = (),
) -> Any:
    """Normalize one string-expected argument at an ava.* entry point.

    `str` passes through; a one-element `list`/`tuple` whose element is a
    `str` unwraps to that element (the trailing-comma class). Anything else —
    a multi-element sequence, a sequence with a non-string element, or a
    different type — raises TypeError naming the parameter.

    `allow_types` admits a parameter's legitimate non-string forms by
    isinstance, without expansion (e.g. `watcher.at`'s datetime/timedelta,
    `files`' os.PathLike). A sequence admitted through `allow_types` must be a
    list of dicts — the multimodal content-block shape — because an
    all-string array is exactly the shape the gateway rejects.
    """
    if value is None:
        if allow_none:
            return None
        raise TypeError(f"{name} must be a string, got None")
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)) and len(value) == 1 and isinstance(value[0], str):
        return value[0]
    if allow_types and isinstance(value, allow_types):
        if isinstance(value, (list, tuple)) and not all(isinstance(part, dict) for part in value):
            raise TypeError(
                f"{name} must be a string or a list of dicts, got a "
                f"{type(value).__name__} of {type(value[0]).__name__} elements"
            )
        return value
    raise TypeError(f"{name} must be a string, got {type(value).__name__}")


def coerce_typed(
    value: object,
    name: str,
    expected: type | tuple[type, ...],
    *,
    allow_none: bool = False,
) -> Any:
    """Strictly check one argument that is inherently not a string.

    The declared-type union (`expected`) is checked with isinstance; a
    one-element list/tuple is NOT unwrapped here — the value must already be
    the expected type. `allow_none` marks an optional parameter.
    """
    if value is None:
        if allow_none:
            return None
        raise TypeError(f"{name} must be {_type_names(expected)}, got None")
    if isinstance(value, expected):
        return value
    raise TypeError(f"{name} must be {_type_names(expected)}, got {type(value).__name__}")
