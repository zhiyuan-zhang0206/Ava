"""SQL fragments shared by the readers of `telemetry_events`."""

from __future__ import annotations

# A payload attribute is text in the `attributes` JSONB; only text that is a number is summed.
NUMBER_PATTERN = r"^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$"


def numeric(expression: str, cast_to: str = "float8") -> str:
    """The numeric value of a payload attribute; text that is not a number reads as missing."""
    return f"CASE WHEN {expression} ~ '{NUMBER_PATTERN}' THEN ({expression})::{cast_to} END"
