"""Conservative archive-to-provider source synchronization.

Candidate provider files are parsed, never imported: the pricing workflow may
resume an existing bot branch, so executing code from that branch would cross
the workflow's trusted-main boundary. The parser accepts only the repository's
literal ``register(models=..., pricing=...)`` shape and fails closed otherwise.

Synchronization preserves the complete pricing lattice: effective periods,
input-token tiers, and recurring UTC windows. The flat ``PriceRates`` fields
remain the base tier covering the synchronization instant.
"""

from __future__ import annotations

import ast
import json
import sys
from collections.abc import Set
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NamedTuple, cast


class FlatRates(NamedTuple):
    """Flat cache-miss, cache-hit, and output rates in exact decimal form."""

    cache_miss: Decimal
    cache_hit: Decimal
    output: Decimal


class _WindowDeclaration(NamedTuple):
    start: str
    end: str
    rates: FlatRates


class _TierDeclaration(NamedTuple):
    input_tokens_min: int
    input_tokens_max: int | None
    rates: FlatRates
    windows: tuple[_WindowDeclaration, ...]


class _PeriodDeclaration(NamedTuple):
    effective_from: str | None
    effective_until: str | None
    tiers: tuple[_TierDeclaration, ...]


class PluginSyncResult(NamedTuple):
    """Observed drift, differing periods, and files changed by one pass."""

    drifted_models: tuple[str, ...]
    changed_files: tuple[Path, ...]
    drifted_periods: tuple[str, ...]


class _PriceDeclaration(NamedTuple):
    rates: FlatRates
    source_url: str
    source_checked_at: str
    vendor: str | None
    periods: tuple[_PeriodDeclaration, ...]


class _ProviderManifest(NamedTuple):
    models: frozenset[str]
    prices: dict[str, _PriceDeclaration]
    price_nodes: dict[str, ast.Call]


def _validated_provenance(
    source_url: object,
    source_checked_at: object,
    vendor: object,
    *,
    context: str,
) -> tuple[str, str, str | None]:
    if not isinstance(source_url, str) or not source_url.startswith("https://"):
        raise RuntimeError(f"{context} source_url must be an HTTPS string")
    if not isinstance(source_checked_at, str):
        raise TypeError(f"{context} source_checked_at must be a YYYY-MM-DD string")
    try:
        checked_at = date.fromisoformat(source_checked_at)
    except ValueError as exc:
        raise RuntimeError(f"{context} source_checked_at must be YYYY-MM-DD") from exc
    if checked_at.isoformat() != source_checked_at:
        raise RuntimeError(f"{context} source_checked_at must be YYYY-MM-DD")
    if vendor is not None and not isinstance(vendor, str):
        raise RuntimeError(f"{context} vendor must be a string or None")
    return source_url, source_checked_at, vendor


def _literal(node: ast.expr, expected: type[Any], *, context: str) -> Any:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"{context} must be a literal") from exc
    if not isinstance(value, expected):
        raise TypeError(f"{context} must be a {expected.__name__} literal")
    return value


def _optional_string_literal(node: ast.expr, *, context: str) -> str | None:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"{context} must be a string or None literal") from exc
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{context} must be a string or None literal")
    return value


def _decimal_node(node: ast.expr, *, context: str) -> Decimal:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Decimal"
        and len(node.args) == 1
        and not node.keywords
    ):
        raw: object = _literal(node.args[0], str, context=context)
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


def _integer_literal(node: ast.expr, *, context: str) -> int:
    value = _literal(node, int, context=context)
    if isinstance(value, bool):
        raise TypeError(f"{context} must be an integer literal")
    return cast(int, value)


def _optional_integer_literal(node: ast.expr, *, context: str) -> int | None:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"{context} must be an integer or None literal") from exc
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise TypeError(f"{context} must be an integer or None literal")
    return value


def _call_fields(
    node: ast.expr,
    constructor: str,
    *,
    required: set[str],
    optional: Set[str] = frozenset(),
    context: str,
) -> tuple[ast.Call, dict[str, ast.expr]]:
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == constructor
        and not node.args
    ):
        raise RuntimeError(f"{context} must be a literal {constructor}(...) call")
    if any(keyword.arg is None for keyword in node.keywords):
        raise RuntimeError(f"{context} cannot contain keyword expansion")
    fields = {cast(str, keyword.arg): keyword.value for keyword in node.keywords}
    if len(fields) != len(node.keywords):
        raise RuntimeError(f"{context} contains duplicate {constructor} fields")
    missing = required - fields.keys()
    unexpected = fields.keys() - required - optional
    if missing:
        raise RuntimeError(f"{context} is missing {constructor} fields {sorted(missing)}")
    if unexpected:
        raise RuntimeError(f"{context} has unexpected {constructor} fields {sorted(unexpected)}")
    return node, fields


def _tuple_items(node: ast.expr, *, context: str) -> tuple[ast.expr, ...]:
    if not isinstance(node, ast.Tuple):
        raise TypeError(f"{context} must be a tuple literal")
    return tuple(node.elts)


def _rates_from_nodes(fields: dict[str, ast.expr], *, context: str) -> FlatRates:
    return FlatRates(
        cache_miss=_decimal_node(fields["cache_miss"], context=f"{context} cache_miss"),
        cache_hit=_decimal_node(fields["cache_hit"], context=f"{context} cache_hit"),
        output=_decimal_node(fields["output"], context=f"{context} output"),
    )


def _window_declaration(node: ast.expr, *, context: str) -> _WindowDeclaration:
    _call, fields = _call_fields(
        node,
        "PriceWindow",
        required={"start", "end", *FlatRates._fields},
        context=context,
    )
    return _WindowDeclaration(
        start=_literal(fields["start"], str, context=f"{context} start"),
        end=_literal(fields["end"], str, context=f"{context} end"),
        rates=_rates_from_nodes(fields, context=context),
    )


def _tier_declaration(node: ast.expr, *, context: str) -> _TierDeclaration:
    _call, fields = _call_fields(
        node,
        "PriceTier",
        required={"input_tokens_min", "input_tokens_max", *FlatRates._fields},
        optional={"windows"},
        context=context,
    )
    window_nodes = (
        _tuple_items(fields["windows"], context=f"{context} windows") if "windows" in fields else ()
    )
    windows = tuple(
        _window_declaration(item, context=f"{context} window {index}")
        for index, item in enumerate(window_nodes)
    )
    return _TierDeclaration(
        input_tokens_min=_integer_literal(
            fields["input_tokens_min"], context=f"{context} input_tokens_min"
        ),
        input_tokens_max=_optional_integer_literal(
            fields["input_tokens_max"], context=f"{context} input_tokens_max"
        ),
        rates=_rates_from_nodes(fields, context=context),
        windows=windows,
    )


def _period_declaration(node: ast.expr, *, context: str) -> _PeriodDeclaration:
    _call, fields = _call_fields(
        node,
        "PricePeriod",
        required={"effective_from", "effective_until", "tiers"},
        context=context,
    )
    return _PeriodDeclaration(
        effective_from=_optional_string_literal(
            fields["effective_from"], context=f"{context} effective_from"
        ),
        effective_until=_optional_string_literal(
            fields["effective_until"], context=f"{context} effective_until"
        ),
        tiers=tuple(
            _tier_declaration(item, context=f"{context} tier {index}")
            for index, item in enumerate(_tuple_items(fields["tiers"], context=f"{context} tiers"))
        ),
    )


def _price_declaration(call: ast.expr, *, context: str) -> tuple[_PriceDeclaration, ast.Call]:
    price_call, fields = _call_fields(
        call,
        "PriceRates",
        required={"cache_miss", "cache_hit", "output", "source_url", "source_checked_at"},
        optional={"vendor", "periods"},
        context=context,
    )
    source_url = _literal(fields["source_url"], str, context=f"{context} source_url")
    checked_at = _literal(fields["source_checked_at"], str, context=f"{context} source_checked_at")
    vendor = (
        None
        if "vendor" not in fields
        else _optional_string_literal(fields["vendor"], context=f"{context} vendor")
    )
    source_url, checked_at, vendor = _validated_provenance(
        source_url,
        checked_at,
        vendor,
        context=context,
    )
    period_nodes = (
        _tuple_items(fields["periods"], context=f"{context} periods") if "periods" in fields else ()
    )
    periods = tuple(
        _period_declaration(item, context=f"{context} period {index}")
        for index, item in enumerate(period_nodes)
    )
    return (
        _PriceDeclaration(
            rates=_rates_from_nodes(fields, context=context),
            source_url=source_url,
            source_checked_at=checked_at,
            vendor=vendor,
            periods=periods,
        ),
        price_call,
    )


def _mapping_keyword(call: ast.Call, name: str, *, path: Path) -> ast.Dict:
    values = [keyword.value for keyword in call.keywords if keyword.arg == name]
    if len(values) != 1 or not isinstance(values[0], ast.Dict):
        raise RuntimeError(f"{path}: register() must have one literal {name}= mapping")
    return values[0]


def _mapping_keys(mapping: ast.Dict, *, context: str) -> tuple[str, ...]:
    if len(mapping.keys) != len(mapping.values) or any(key is None for key in mapping.keys):
        raise RuntimeError(f"{context} cannot contain mapping expansion")
    keys = tuple(
        cast(str, _literal(cast(ast.expr, key), str, context=f"{context} key"))
        for key in mapping.keys
    )
    if len(keys) != len(set(keys)):
        raise RuntimeError(f"{context} contains duplicate model ids")
    return keys


def _provider_manifest(source: str, path: Path) -> _ProviderManifest:
    tree = ast.parse(source, filename=str(path))
    registrations = [
        statement.value
        for statement in tree.body
        if isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id == "register"
    ]
    if len(registrations) != 1:
        raise RuntimeError(f"{path}: expected exactly one top-level register() call")
    models_node = _mapping_keyword(registrations[0], "models", path=path)
    pricing_node = _mapping_keyword(registrations[0], "pricing", path=path)
    models = frozenset(_mapping_keys(models_node, context=f"{path}: models"))
    price_models = _mapping_keys(pricing_node, context=f"{path}: pricing")
    if not set(price_models) <= models:
        raise RuntimeError(f"{path}: pricing contains ids absent from models")
    prices: dict[str, _PriceDeclaration] = {}
    price_nodes: dict[str, ast.Call] = {}
    for model, value in zip(price_models, pricing_node.values, strict=True):
        prices[model], price_nodes[model] = _price_declaration(
            value,
            context=f"{path}: {model}",
        )
    return _ProviderManifest(models, prices, price_nodes)


def _instant(value: str | None, *, context: str) -> datetime | None:
    if value is None:
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"{context} must be an ISO-8601 instant") from exc
    if instant.utcoffset() is None:
        raise RuntimeError(f"{context} must carry a UTC offset")
    return instant.astimezone(UTC)


def _clock(value: object, *, context: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{context} must be an HH:MM:SS string")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError(f"{context} must be an HH:MM:SS string") from exc
    if parsed.tzinfo is not None:
        raise RuntimeError(f"{context} must be offset-free UTC wall time")
    return value


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


def _archive_rates(raw: object, *, context: str) -> FlatRates:
    if not isinstance(raw, dict):
        raise TypeError(f"{context} must be a rates object")
    return FlatRates(
        cache_miss=_archive_decimal(raw["input"], context=f"{context} input"),
        cache_hit=_archive_decimal(raw["cache_read"], context=f"{context} cache_read"),
        output=_archive_decimal(raw["output"], context=f"{context} output"),
    )


def _archive_periods(model: str, raw: object) -> tuple[_PeriodDeclaration, ...]:
    if not isinstance(raw, list) or not raw:
        raise RuntimeError(f"archive {model!r} must contain periods")
    periods: list[_PeriodDeclaration] = []
    previous_until: str | None = None
    for period_index, period_raw in enumerate(raw):
        context = f"archive {model!r} period {period_index}"
        if not isinstance(period_raw, dict):
            raise TypeError(f"{context} must be an object")
        effective_from = cast(str | None, period_raw["effective_from"])
        effective_until = cast(str | None, period_raw["effective_until"])
        start = _instant(effective_from, context=f"{context} effective_from")
        end = _instant(effective_until, context=f"{context} effective_until")
        if period_index == 0 and start is not None:
            raise RuntimeError(f"archive {model!r} must cover all history")
        if period_index > 0 and effective_from != previous_until:
            raise RuntimeError(f"archive {model!r} has a gap or overlap in periods")
        if start is not None and end is not None and end <= start:
            raise RuntimeError(f"{context} is empty")
        tiers_raw = period_raw["tiers"]
        if not isinstance(tiers_raw, list) or not tiers_raw:
            raise RuntimeError(f"{context} must contain tiers")
        tiers: list[_TierDeclaration] = []
        expected_min = 0
        for tier_index, tier_raw in enumerate(tiers_raw):
            tier_context = f"{context} tier {tier_index}"
            if not isinstance(tier_raw, dict):
                raise TypeError(f"{tier_context} must be an object")
            lower = tier_raw["input_tokens_min"]
            upper = tier_raw["input_tokens_max"]
            if isinstance(lower, bool) or not isinstance(lower, int):
                raise TypeError(f"{tier_context} input_tokens_min must be an integer")
            if upper is not None and (isinstance(upper, bool) or not isinstance(upper, int)):
                raise TypeError(f"{tier_context} input_tokens_max must be an integer or null")
            if lower != expected_min or (upper is not None and upper < lower):
                raise RuntimeError(f"archive {model!r} has a gap or overlap in tiers")
            windows_raw = tier_raw["utc_daily_overrides"]
            if not isinstance(windows_raw, list):
                raise TypeError(f"{tier_context} utc_daily_overrides must be an array")
            windows = tuple(
                _WindowDeclaration(
                    start=_clock(window["start"], context=f"{tier_context} window {index} start"),
                    end=_clock(window["end"], context=f"{tier_context} window {index} end"),
                    rates=_archive_rates(
                        window["rates"], context=f"{tier_context} window {index} rates"
                    ),
                )
                for index, window in enumerate(windows_raw)
                if isinstance(window, dict)
            )
            if len(windows) != len(windows_raw):
                raise TypeError(f"{tier_context} windows must be objects")
            tiers.append(
                _TierDeclaration(
                    input_tokens_min=lower,
                    input_tokens_max=upper,
                    rates=_archive_rates(tier_raw["rates"], context=f"{tier_context} rates"),
                    windows=windows,
                )
            )
            expected_min = -1 if upper is None else upper + 1
        if expected_min != -1:
            raise RuntimeError(f"archive {model!r} tiers must cover all input sizes")
        periods.append(_PeriodDeclaration(effective_from, effective_until, tuple(tiers)))
        previous_until = effective_until
    if periods[-1].effective_until is not None:
        raise RuntimeError(f"archive {model!r} must cover the present")
    return tuple(periods)


def _base_rates(period: _PeriodDeclaration, *, context: str) -> FlatRates:
    base = [tier.rates for tier in period.tiers if tier.input_tokens_min == 0]
    if len(base) != 1:
        raise RuntimeError(f"{context} must contain exactly one zero-input base tier")
    return base[0]


def _archive_price(
    model: str,
    entry: dict[str, Any],
    now: datetime,
) -> tuple[_PriceDeclaration, tuple[tuple[str, FlatRates], ...]]:
    periods = _archive_periods(model, entry["periods"])
    covering: list[_PeriodDeclaration] = []
    upcoming: list[tuple[str, FlatRates]] = []
    for index, period in enumerate(periods):
        context = f"archive {model!r} period {index}"
        start = _instant(period.effective_from, context=f"{context} effective_from")
        end = _instant(period.effective_until, context=f"{context} effective_until")
        rates = _base_rates(period, context=context)
        if (start is None or now >= start) and (end is None or now < end):
            covering.append(period)
        if start is not None and start > now:
            upcoming.append((cast(str, period.effective_from), rates))
    if len(covering) != 1:
        raise RuntimeError(f"archive {model!r} must have exactly one period covering now")
    current = _base_rates(covering[0], context=f"archive {model!r} current period")
    source_url, checked_at, vendor = _validated_provenance(
        entry["source_url"],
        entry["source_checked_at"],
        entry.get("vendor"),
        context=f"archive {model!r}",
    )
    return _PriceDeclaration(current, source_url, checked_at, vendor, periods), tuple(upcoming)


def _render_window(window: _WindowDeclaration, indent: int) -> list[str]:
    outer = " " * indent
    inner = " " * (indent + 4)
    return [
        f"{outer}PriceWindow(",
        f"{inner}start={json.dumps(window.start)},",
        f"{inner}end={json.dumps(window.end)},",
        f"{inner}cache_miss={json.dumps(str(window.rates.cache_miss))},",
        f"{inner}cache_hit={json.dumps(str(window.rates.cache_hit))},",
        f"{inner}output={json.dumps(str(window.rates.output))},",
        f"{outer}),",
    ]


def _render_tier(tier: _TierDeclaration, indent: int) -> list[str]:
    outer = " " * indent
    inner = " " * (indent + 4)
    lines = [
        f"{outer}PriceTier(",
        f"{inner}input_tokens_min={tier.input_tokens_min},",
        f"{inner}input_tokens_max={tier.input_tokens_max},",
        f"{inner}cache_miss={json.dumps(str(tier.rates.cache_miss))},",
        f"{inner}cache_hit={json.dumps(str(tier.rates.cache_hit))},",
        f"{inner}output={json.dumps(str(tier.rates.output))},",
    ]
    if tier.windows:
        lines.append(f"{inner}windows=(")
        for window in tier.windows:
            lines.extend(_render_window(window, indent + 8))
        lines.append(f"{inner}),")
    lines.append(f"{outer}),")
    return lines


def _string_or_none_literal(value: str | None) -> str:
    return "None" if value is None else json.dumps(value)


def _render_period(period: _PeriodDeclaration, indent: int) -> list[str]:
    outer = " " * indent
    inner = " " * (indent + 4)
    lines = [
        f"{outer}PricePeriod(",
        f"{inner}effective_from={_string_or_none_literal(period.effective_from)},",
        f"{inner}effective_until={_string_or_none_literal(period.effective_until)},",
        f"{inner}tiers=(",
    ]
    for tier in period.tiers:
        lines.extend(_render_tier(tier, indent + 8))
    lines.extend((f"{inner}),", f"{outer}),"))
    return lines


def _render_price(price: _PriceDeclaration, indent: int) -> str:
    outer = " " * indent
    inner = " " * (indent + 4)
    lines = [
        "PriceRates(",
        f"{inner}cache_miss={price.rates.cache_miss},",
        f"{inner}cache_hit={price.rates.cache_hit},",
        f"{inner}output={price.rates.output},",
        f"{inner}source_url={json.dumps(price.source_url)},",
        f"{inner}source_checked_at={json.dumps(price.source_checked_at)},",
        f"{inner}vendor={_string_or_none_literal(price.vendor)},",
        f"{inner}periods=(",
    ]
    for period in price.periods:
        lines.extend(_render_period(period, indent + 8))
    lines.extend((f"{inner}),", f"{outer})"))
    return "\n".join(lines)


def _call_offsets(source: str, node: ast.Call, *, path: Path) -> tuple[int, int]:
    if node.end_lineno is None or node.end_col_offset is None:
        raise RuntimeError(f"{path}:{node.lineno}: PriceRates span is unavailable")
    lines = source.splitlines(keepends=True)
    start = sum(len(line) for line in lines[: node.lineno - 1]) + node.col_offset
    end = sum(len(line) for line in lines[: node.end_lineno - 1]) + node.end_col_offset
    if not source[start:end].startswith("PriceRates("):
        raise RuntimeError(f"{path}:{node.lineno}: PriceRates span is not line-anchored")
    return start, end


def _entry_indent(source: str, node: ast.Call) -> int:
    line = source.splitlines()[node.lineno - 1]
    return len(line) - len(line.lstrip(" "))


def _period_label(period: _PeriodDeclaration, index: int) -> str:
    return f"period {index} [{period.effective_from!r}, {period.effective_until!r})"


def _drift_labels(
    model: str, declared: _PriceDeclaration, target: _PriceDeclaration
) -> tuple[str, ...]:
    labels: list[str] = []
    if declared.rates != target.rates:
        labels.append(f"{model}: current flat rate")
    if declared.source_checked_at != target.source_checked_at:
        labels.append(f"{model}: source_checked_at")
    if declared.periods != target.periods:
        for index in range(max(len(declared.periods), len(target.periods))):
            if index >= len(target.periods):
                labels.append(f"{model}: extra declared period {index}")
            elif index >= len(declared.periods) or declared.periods[index] != target.periods[index]:
                labels.append(f"{model}: {_period_label(target.periods[index], index)}")
    return tuple(labels)


def _rewrite_provider(
    source: str,
    path: Path,
    manifest: _ProviderManifest,
    targets: dict[str, _PriceDeclaration],
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    replacements: list[tuple[int, int, str]] = []
    drifted: list[str] = []
    drifted_periods: list[str] = []
    for model, declared in manifest.prices.items():
        target = targets[model]
        if (declared.source_url, declared.vendor) != (target.source_url, target.vendor):
            raise RuntimeError(f"{path}: {model!r} source_url/vendor drift requires human review")
        labels = _drift_labels(model, declared, target)
        if not labels:
            continue
        drifted.append(model)
        drifted_periods.extend(labels)
        node = manifest.price_nodes[model]
        start, end = _call_offsets(source, node, path=path)
        replacements.append((start, end, _render_price(target, _entry_indent(source, node))))
    rewritten = source
    for start, end, replacement in sorted(replacements, reverse=True):
        rewritten = rewritten[:start] + replacement + rewritten[end:]
    round_trip = _provider_manifest(rewritten, path)
    if round_trip.models != manifest.models or round_trip.prices.keys() != manifest.prices.keys():
        raise RuntimeError(f"{path}: rewrite changed the registered model or pricing set")
    for model, target in targets.items():
        if round_trip.prices[model] != target:
            raise RuntimeError(f"{path}: {model!r} did not round-trip to the archive price")
    compile(rewritten, str(path), "exec")
    return rewritten, tuple(drifted), tuple(drifted_periods)


def _regular_file(path: Path, root: Path, *, label: str) -> Path:
    resolved = path.resolve(strict=True)
    if path.is_symlink() or resolved != path.absolute() or not resolved.is_relative_to(root):
        raise RuntimeError(f"{label} must be a regular file inside {root}")
    if not resolved.is_file():
        raise RuntimeError(f"{label} must be a regular file")
    return resolved


def sync_plugin_rates(
    archive_path: Path,
    repo_root: Path,
    *,
    now: datetime,
    write: bool = True,
) -> PluginSyncResult:
    """Synchronize every built-in provider's complete price lattice."""
    root = repo_root.resolve(strict=True)
    archive = _regular_file(archive_path, root, label="pricing archive")
    raw = cast(dict[str, Any], json.loads(archive.read_text(encoding="utf-8")))
    archive_models = cast(dict[str, dict[str, Any]], raw["models"])
    provider_paths = sorted((root / "ava_builtins/plugins").glob("lm_*/provider.py"))
    if not provider_paths:
        raise RuntimeError(f"no built-in provider.py files found under {root}")

    plans: list[tuple[Path, str]] = []
    drifted_models: list[str] = []
    drifted_periods: list[str] = []
    future_notes: list[tuple[str, str, FlatRates]] = []
    seen_models: set[str] = set()
    for candidate in provider_paths:
        path = _regular_file(candidate, root, label="provider source")
        source = path.read_text(encoding="utf-8")
        manifest = _provider_manifest(source, path)
        duplicate = seen_models & manifest.prices.keys()
        if duplicate:
            raise RuntimeError(f"plugin prices registered more than once: {sorted(duplicate)}")
        seen_models.update(manifest.prices)
        targets: dict[str, _PriceDeclaration] = {}
        for model in manifest.prices:
            if model not in archive_models:
                raise RuntimeError(f"archive has no entry for plugin-priced model {model!r}")
            target, upcoming = _archive_price(model, archive_models[model], now)
            targets[model] = target
            future_notes.extend(
                (model, effective_from, rates)
                for effective_from, rates in upcoming
                if rates != target.rates
            )
        rewritten, drifted, differing_periods = _rewrite_provider(source, path, manifest, targets)
        drifted_models.extend(drifted)
        drifted_periods.extend(differing_periods)
        if drifted:
            plans.append((path, rewritten))

    for detail in sorted(drifted_periods):
        print(f"- Plugin pricing drift: {detail}.", file=sys.stderr)
    for model, effective_from, rates in sorted(future_notes):
        print(
            f"- `{model}` changes at `{effective_from}` to cache miss {rates.cache_miss}, "
            f"cache hit {rates.cache_hit}, output {rates.output} USD/1M tokens.",
            file=sys.stderr,
        )
    changed_files: tuple[Path, ...] = ()
    if write:
        for path, rewritten in plans:
            path.write_text(rewritten, encoding="utf-8")
        changed_files = tuple(path for path, _rewritten in plans)
    return PluginSyncResult(
        tuple(sorted(drifted_models)),
        changed_files,
        tuple(sorted(drifted_periods)),
    )
