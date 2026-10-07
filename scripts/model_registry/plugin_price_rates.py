"""Exact optional rate dimensions for non-executing provider synchronization."""

from __future__ import annotations

import ast
import json
from decimal import Decimal, InvalidOperation
from typing import NamedTuple


class FlatRates(NamedTuple):
    """Flat cache-miss, cache-hit, and output rates in exact decimal form."""

    cache_miss: Decimal
    cache_hit: Decimal
    output: Decimal
    cache_write_5m: Decimal | None = None
    cache_write_1h: Decimal | None = None


def _decimal_node(node: ast.expr, *, context: str) -> Decimal:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Decimal"
        and len(node.args) == 1
        and not node.keywords
    ):
        try:
            raw: object = ast.literal_eval(node.args[0])
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"{context} must be a string literal") from exc
        if not isinstance(raw, str):
            raise TypeError(f"{context} must be a string literal")
    else:
        try:
            raw = ast.literal_eval(node)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"{context} must be a numeric literal") from exc
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise TypeError(f"{context} must be a numeric literal")
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise RuntimeError(f"{context} must be a decimal literal") from exc
    if not value.is_finite() or value < 0:
        raise RuntimeError(f"{context} must be finite and non-negative")
    return value


def rates_from_nodes(fields: dict[str, ast.expr], *, context: str) -> FlatRates:
    return FlatRates(
        cache_miss=_decimal_node(fields["cache_miss"], context=f"{context} cache_miss"),
        cache_hit=_decimal_node(fields["cache_hit"], context=f"{context} cache_hit"),
        output=_decimal_node(fields["output"], context=f"{context} output"),
        **{
            key: _optional_decimal_node(fields.get(key), context=f"{context} {key}")
            for key in ("cache_write_5m", "cache_write_1h")
        },
    )


def _optional_decimal_node(node: ast.expr | None, *, context: str) -> Decimal | None:
    if node is None or (isinstance(node, ast.Constant) and node.value is None):
        return None
    return _decimal_node(node, context=context)


def _archive_decimal(value: object, *, context: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TypeError(f"{context} must be a decimal value")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise RuntimeError(f"{context} must be a decimal value") from exc
    if not parsed.is_finite() or parsed < 0:
        raise RuntimeError(f"{context} must be finite and non-negative")
    return parsed


def archive_rates(raw: object, *, context: str) -> FlatRates:
    if not isinstance(raw, dict):
        raise TypeError(f"{context} must be a rates object")
    return FlatRates(
        cache_miss=_archive_decimal(raw["input"], context=f"{context} input"),
        cache_hit=_archive_decimal(raw["cache_read"], context=f"{context} cache_read"),
        output=_archive_decimal(raw["output"], context=f"{context} output"),
        **{
            key: _archive_decimal(raw[key], context=f"{context} {key}")
            if raw.get(key) is not None
            else None
            for key in ("cache_write_5m", "cache_write_1h")
        },
    )


def cache_write_lines(rates: FlatRates, indent: str, *, quoted: bool = True) -> list[str]:
    """Render only the cache-write rates declared by the reviewed archive."""
    return [
        f"{indent}{key}={json.dumps(str(value)) if quoted else str(value)},"
        for key in ("cache_write_5m", "cache_write_1h")
        if (value := getattr(rates, key)) is not None
    ]
