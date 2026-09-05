"""Conservative archive-to-provider source synchronization.

Candidate provider files are parsed, never imported: the pricing workflow may
resume an existing bot branch, so executing code from that branch would cross
the workflow's trusted-main boundary. The parser accepts only the repository's
literal ``register(models=..., pricing=...)`` shape and fails closed otherwise.

Current synchronization selects only the zero-input base tier from the active
period. BLOCK-1 will extend plugin pricing to preserve all periods, tiers, and
recurring windows.
"""

from __future__ import annotations

import ast
import json
import sys
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NamedTuple, cast


class FlatRates(NamedTuple):
    """Flat cache-miss, cache-hit, and output rates in exact decimal form."""

    cache_miss: Decimal
    cache_hit: Decimal
    output: Decimal


class PluginSyncResult(NamedTuple):
    """Observed drift and files changed by one synchronization pass."""

    drifted_models: tuple[str, ...]
    changed_files: tuple[Path, ...]


class _PriceDeclaration(NamedTuple):
    rates: FlatRates
    source_url: str
    source_checked_at: str
    vendor: str | None


class _ProviderManifest(NamedTuple):
    models: frozenset[str]
    prices: dict[str, _PriceDeclaration]
    value_nodes: dict[str, dict[str, ast.expr]]


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


def _price_declaration(
    call: ast.expr, *, context: str
) -> tuple[_PriceDeclaration, dict[str, ast.expr]]:
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "PriceRates"
        and not call.args
    ):
        raise RuntimeError(f"{context} must be a literal PriceRates(...) call")
    if any(keyword.arg is None for keyword in call.keywords):
        raise RuntimeError(f"{context} cannot contain keyword expansion")
    nodes = {cast(str, keyword.arg): keyword.value for keyword in call.keywords}
    if len(nodes) != len(call.keywords):
        raise RuntimeError(f"{context} contains duplicate PriceRates fields")
    required = {"cache_miss", "cache_hit", "output", "source_url", "source_checked_at"}
    if not required <= nodes.keys():
        raise RuntimeError(
            f"{context} is missing PriceRates fields {sorted(required - nodes.keys())}"
        )
    try:
        rates = FlatRates(
            *(Decimal(str(ast.literal_eval(nodes[field]))) for field in FlatRates._fields)
        )
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise RuntimeError(f"{context} rates must be numeric literals") from exc
    if any(not rate.is_finite() or rate < 0 for rate in rates):
        raise RuntimeError(f"{context} rates must be finite and non-negative")
    source_url = _literal(nodes["source_url"], str, context=f"{context} source_url")
    checked_at = _literal(nodes["source_checked_at"], str, context=f"{context} source_checked_at")
    vendor_node = nodes.get("vendor")
    try:
        vendor = None if vendor_node is None else ast.literal_eval(vendor_node)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"{context} vendor must be a string or None literal") from exc
    source_url, checked_at, vendor = _validated_provenance(
        source_url,
        checked_at,
        vendor,
        context=context,
    )
    return _PriceDeclaration(rates, source_url, checked_at, vendor), nodes


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
    value_nodes: dict[str, dict[str, ast.expr]] = {}
    for model, value in zip(price_models, pricing_node.values, strict=True):
        prices[model], value_nodes[model] = _price_declaration(
            value,
            context=f"{path}: {model}",
        )
    return _ProviderManifest(models, prices, value_nodes)


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


def _base_rates(period: dict[str, Any], *, context: str) -> FlatRates:
    tiers = cast(list[dict[str, Any]], period["tiers"])
    base = [tier for tier in tiers if tier["input_tokens_min"] == 0]
    if len(base) != 1:
        raise RuntimeError(f"{context} must contain exactly one zero-input base tier")
    raw = cast(dict[str, str], base[0]["rates"])
    try:
        rates = FlatRates(Decimal(raw["input"]), Decimal(raw["cache_read"]), Decimal(raw["output"]))
    except (InvalidOperation, KeyError) as exc:
        raise RuntimeError(f"{context} rates must contain decimal input/cache_read/output") from exc
    if any(not rate.is_finite() or rate < 0 for rate in rates):
        raise RuntimeError(f"{context} rates must be finite and non-negative")
    return rates


def _archive_price(
    model: str,
    entry: dict[str, Any],
    now: datetime,
) -> tuple[_PriceDeclaration, tuple[tuple[str, FlatRates], ...]]:
    periods = cast(list[dict[str, Any]], entry["periods"])
    covering: list[dict[str, Any]] = []
    upcoming: list[tuple[str, FlatRates]] = []
    for index, period in enumerate(periods):
        context = f"archive {model!r} period {index}"
        raw_start = cast(str | None, period["effective_from"])
        start = _instant(raw_start, context=f"{context} effective_from")
        end = _instant(
            cast(str | None, period["effective_until"]), context=f"{context} effective_until"
        )
        rates = _base_rates(period, context=context)
        if (start is None or now >= start) and (end is None or now < end):
            covering.append(period)
        if start is not None and start > now:
            upcoming.append((cast(str, raw_start), rates))
    if len(covering) != 1:
        raise RuntimeError(f"archive {model!r} must have exactly one period covering now")
    current = _base_rates(covering[0], context=f"archive {model!r} current period")
    source_url, checked_at, vendor = _validated_provenance(
        entry["source_url"],
        entry["source_checked_at"],
        entry.get("vendor"),
        context=f"archive {model!r}",
    )
    return _PriceDeclaration(current, source_url, checked_at, vendor), tuple(upcoming)


def _replace_literal(
    lines: list[str], node: ast.expr, field: str, literal: str, *, path: Path
) -> None:
    if node.lineno != node.end_lineno:
        raise RuntimeError(f"{path}:{node.lineno}: {field} must stay on one line")
    index = node.lineno - 1
    line = lines[index]
    newline = "\n" if line.endswith("\n") else ""
    body = line[: -len(newline)] if newline else line
    if not body[: node.col_offset].endswith(f"{field}=") or body[node.end_col_offset :] != ",":
        raise RuntimeError(f"{path}:{node.lineno}: {field} is not in the line-anchored form")
    lines[index] = body[: node.col_offset] + literal + body[node.end_col_offset :] + newline


def _rewrite_provider(
    source: str,
    path: Path,
    manifest: _ProviderManifest,
    targets: dict[str, _PriceDeclaration],
) -> tuple[str, tuple[str, ...]]:
    lines = source.splitlines(keepends=True)
    drifted: list[str] = []
    for model, declared in manifest.prices.items():
        target = targets[model]
        if (declared.source_url, declared.vendor) != (target.source_url, target.vendor):
            raise RuntimeError(f"{path}: {model!r} source_url/vendor drift requires human review")
        replacements: dict[str, str] = {}
        for field, current, wanted in zip(
            FlatRates._fields, declared.rates, target.rates, strict=True
        ):
            if current != wanted:
                replacements[field] = str(wanted)
        if declared.source_checked_at != target.source_checked_at:
            replacements["source_checked_at"] = json.dumps(target.source_checked_at)
        if not replacements:
            continue
        drifted.append(model)
        for field, literal in replacements.items():
            _replace_literal(lines, manifest.value_nodes[model][field], field, literal, path=path)
    rewritten = "".join(lines)
    round_trip = _provider_manifest(rewritten, path)
    if round_trip.models != manifest.models or round_trip.prices.keys() != manifest.prices.keys():
        raise RuntimeError(f"{path}: rewrite changed the registered model or pricing set")
    for model, target in targets.items():
        if round_trip.prices[model] != target:
            raise RuntimeError(f"{path}: {model!r} did not round-trip to the archive price")
    compile(rewritten, str(path), "exec")
    return rewritten, tuple(drifted)


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
    """Synchronize every built-in provider's flat rates from the archive."""
    root = repo_root.resolve(strict=True)
    archive = _regular_file(archive_path, root, label="pricing archive")
    raw = cast(dict[str, Any], json.loads(archive.read_text(encoding="utf-8")))
    archive_models = cast(dict[str, dict[str, Any]], raw["models"])
    provider_paths = sorted((root / "ava_builtins/plugins").glob("lm_*/provider.py"))
    if not provider_paths:
        raise RuntimeError(f"no built-in provider.py files found under {root}")

    plans: list[tuple[Path, str]] = []
    drifted_models: list[str] = []
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
        rewritten, drifted = _rewrite_provider(source, path, manifest, targets)
        drifted_models.extend(drifted)
        if drifted:
            plans.append((path, rewritten))

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
    return PluginSyncResult(tuple(sorted(drifted_models)), changed_files)
