"""`ava config` — read / set / unset cluster + host config from the terminal.

Thin client over the gateway's `GET/PUT /api/config` (the same endpoint the
settings panel uses). A value lives in exactly one `.env` (cluster fields in the
gateway's, host fields in the target machine's); the gateway persists the edit
and reports `restart_required` — this command prints that and never restarts
anything, so the operator restarts the named processes when ready.

Keys may be given as the env-var name (`DEEPSEEK_API_KEY`) or the field name
(`deepseek_api_key`); both resolve to the same field. `--machine` targets a
remote agent-runner's host fields.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from shared.api_contracts.config import ConfigFieldView, ConfigView, ConfigWriteResult

_HTTP_TIMEOUT_S = 15.0


def _gateway_base() -> str:
    from shared.machine import gateway_api_base

    return gateway_api_base()


def _get_config(machine: str | None) -> ConfigView:
    from shared.http_dial import get as dial_get
    from shared.machine import gateway_auth_headers

    params = {"machine": machine} if machine else None
    resp = dial_get(
        f"{_gateway_base()}/api/config",
        params=params,
        timeout=_HTTP_TIMEOUT_S,
        headers=gateway_auth_headers(),
    )
    resp.raise_for_status()
    return ConfigView.model_validate(resp.json())


def _put_config(body: dict[str, Any], machine: str | None) -> ConfigWriteResult:
    from shared.http_dial import put as dial_put
    from shared.machine import gateway_auth_headers

    params = {"machine": machine} if machine else None
    resp = dial_put(
        f"{_gateway_base()}/api/config",
        json=body,
        params=params,
        timeout=_HTTP_TIMEOUT_S,
        headers=gateway_auth_headers(),
    )
    if resp.status_code == 400:
        # The gateway rejects unknown / read-only fields with a 400 + detail.
        raise _ConfigError(resp.json().get("detail", resp.text))
    resp.raise_for_status()
    return ConfigWriteResult.model_validate(resp.json())


class _ConfigError(Exception):
    """A user-facing config error (bad key, read-only field, gateway 400)."""


def _index_fields(view: ConfigView) -> dict[str, ConfigFieldView]:
    """Map both the field name and its env_var to the field's view model."""
    index: dict[str, ConfigFieldView] = {}
    for f in view.fields:
        index[f.name] = f
        index[f.env_var] = f
    return index


def _resolve_field(key: str, index: dict[str, ConfigFieldView]) -> ConfigFieldView:
    field = index.get(key)
    if field is None:
        raise _ConfigError(
            f"unknown config key: {key!r} (use the env-var name like AVA_MODEL or the field name)"
        )
    return field


def _coerce(field_type: str, raw: str) -> Any:
    """Coerce a CLI string value to the field's type so the gateway validator and
    typed consumers see the right shape."""
    if field_type == "int":
        try:
            return int(raw)
        except ValueError:
            raise _ConfigError(f"invalid int value: {raw!r}") from None
    if field_type == "float":
        try:
            return float(raw)
        except ValueError:
            raise _ConfigError(f"invalid float value: {raw!r}") from None
    if field_type == "bool":
        low = raw.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        raise _ConfigError(f"invalid bool value: {raw!r} (use true/false)")
    return raw


def _print_restart_hint(result: ConfigWriteResult, machine: str | None) -> None:
    targets = result.restart_required
    where = machine or "this cluster"
    if targets:
        print(f"  restart to apply on {where}: {', '.join(targets)}")
    else:
        print("  takes effect on the next process start; no restart required.")


def cmd_config_get(key: str | None, machine: str | None) -> int:
    """`ava config get [KEY] [--machine M]` — print all fields, or one field's value."""
    import httpx

    try:
        view = _get_config(machine)
    except httpx.HTTPError as e:
        print(f"[ava config get] gateway request failed: {e}", file=sys.stderr)
        return 1

    if key is not None:
        index = _index_fields(view)
        try:
            field = _resolve_field(key, index)
        except _ConfigError as e:
            print(f"[ava config get] {e}", file=sys.stderr)
            return 1
        value = field.current_value
        shown = "(empty)" if value in (None, "") else value
        print(f"{field.env_var} ({field.name}) = {shown}")
        print(f"  scope={field.scope}  restart_required={field.restart_required or '(none)'}")
        return 0

    overrides = view.raw_overrides
    name_w = max((len(f.env_var) for f in view.fields), default=4)
    for f in sorted(view.fields, key=lambda x: (x.scope, x.name)):
        value = f.current_value
        shown = "(empty)" if value in (None, "") else value
        marker = "*" if f.name in overrides else " "
        print(f"{marker} {f.env_var.ljust(name_w)}  {shown}")
    print("\n(* = explicitly set in .env; others are at their default)")
    return 0


def _build_delta(
    view: ConfigView, pairs: dict[str, Any] | None, unset_keys: list[str] | None
) -> dict[str, Any]:
    """Build a merge patch carrying ONLY the changed keys (reducer semantics).

    A set is `{field: value}`; an unset is `{field: None}` (explicit deletion).
    The endpoint leaves any key not in the body untouched, so we send just the
    delta — never the whole override set — and deletion is the explicit None, not
    an omission. `view` is used only to resolve key aliases and gate read-only sets.

    Raises:
        _ConfigError: an unknown key, or a set on a read-only field.
    """
    index = _index_fields(view)
    body: dict[str, Any] = {}
    for key, raw in (pairs or {}).items():
        field = _resolve_field(key, index)
        if not field.writable:
            raise _ConfigError(f"{field.env_var} is read-only")
        body[field.name] = _coerce(field.field_type, raw)
    for key in unset_keys or []:
        field = _resolve_field(key, index)
        body[field.name] = None
    return body


def _edit_config(
    pairs: dict[str, Any] | None, unset_keys: list[str] | None, machine: str | None, verb: str
) -> int:
    """Shared set/unset path: GET config (to resolve key aliases + gate read-only),
    build the merge patch, PUT it.

    The endpoint merges the patch (reducer semantics), so the body carries only the
    changed keys — a set as {field: value}, an unset as {field: None}."""
    import httpx

    try:
        view = _get_config(machine)
    except httpx.HTTPError as e:
        print(f"[ava config {verb}] gateway request failed: {e}", file=sys.stderr)
        return 1

    try:
        body = _build_delta(view, pairs, unset_keys)
    except _ConfigError as e:
        print(f"[ava config {verb}] {e}", file=sys.stderr)
        return 1

    try:
        result = _put_config(body, machine)
    except _ConfigError as e:
        print(f"[ava config {verb}] {e}", file=sys.stderr)
        return 1
    except httpx.HTTPError as e:
        print(f"[ava config {verb}] gateway request failed: {e}", file=sys.stderr)
        return 1

    if not result.applied:
        print(f"[ava config {verb}] rejected:", file=sys.stderr)
        for name, r in result.results.items():
            if not r.ok:
                print(f"  {name}: {r.reason}", file=sys.stderr)
        return 1

    print(f"[ava config {verb}] applied.")
    _print_restart_hint(result, machine)
    return 0


def cmd_config_set(assignments: list[str], machine: str | None) -> int:
    """`ava config set KEY=VALUE [KEY=VALUE ...] [--machine M]`."""
    pairs: dict[str, str] = {}
    for item in assignments:
        if "=" not in item:
            print(f"[ava config set] expected KEY=VALUE, got {item!r}", file=sys.stderr)
            return 1
        key, value = item.split("=", 1)
        pairs[key.strip()] = value
    return _edit_config(pairs, None, machine, "set")


def cmd_config_unset(keys: list[str], machine: str | None) -> int:
    """`ava config unset KEY [KEY ...] [--machine M]` — revert fields to their default."""
    return _edit_config(None, keys, machine, "unset")


# argparse handlers (co-located with the commands; wired by the cli/parsers tree)


def h_config_get(args: argparse.Namespace) -> int:
    return cmd_config_get(key=args.key, machine=args.machine)


def h_config_set(args: argparse.Namespace) -> int:
    return cmd_config_set(assignments=args.assignments, machine=args.machine)


def h_config_unset(args: argparse.Namespace) -> int:
    return cmd_config_unset(keys=args.keys, machine=args.machine)
