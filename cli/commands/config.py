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
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pydantic_core import PydanticUndefined

from shared.api_contracts.config import ConfigFieldView, ConfigView, ConfigWriteResult

_HTTP_TIMEOUT_S = 15.0


def _gateway_base() -> str:
    """Resolve the configured gateway without constructing ``Settings``."""
    for key in ("AVA_GATEWAY_URL", "AVA_PRIMARY_GATEWAY_URL"):
        gateway_url = os.environ.get(key, "").strip()
        if gateway_url:
            return gateway_url.rstrip("/")
    from shared import runtime_config

    aliases = runtime_config.read_env_aliases()
    for key in ("AVA_GATEWAY_URL", "AVA_PRIMARY_GATEWAY_URL"):
        gateway_url = aliases.get(key, "").strip()
        if gateway_url:
            return gateway_url.rstrip("/")
    from shared.dotenv_boot import AVA_ENV_PATH

    gateway_url_path = AVA_ENV_PATH.parent / "gateway_url"
    if gateway_url_path.exists():
        gateway_url = gateway_url_path.read_text().strip()
        if gateway_url:
            return gateway_url.rstrip("/")
    raise _ConfigError(
        "gateway_url unset — `ava enroll` writes it on an agent-runner; "
        "or `export AVA_GATEWAY_URL=<gateway url>`"
    )


def _auth_headers() -> dict[str, str]:
    """Read this unit's bearer secret without constructing Settings."""
    from shared import runtime_config
    from shared.cluster_auth import bearer_header

    secret = os.environ.get("AVA_CLUSTER_SECRET")
    if secret is None:
        secret = runtime_config.read_env_aliases().get("AVA_CLUSTER_SECRET", "")
    return bearer_header(secret) if secret else {}


def _get_config(machine: str | None) -> ConfigView:
    from shared.http_dial import get as dial_get

    params = {"machine": machine} if machine else None
    resp = dial_get(
        f"{_gateway_base()}/api/config",
        params=params,
        timeout=_HTTP_TIMEOUT_S,
        headers=_auth_headers(),
    )
    resp.raise_for_status()
    return ConfigView.model_validate(resp.json())


def _put_config(body: dict[str, Any], machine: str | None) -> ConfigWriteResult:
    from shared.http_dial import put as dial_put

    params = {"machine": machine} if machine else None
    resp = dial_put(
        f"{_gateway_base()}/api/config",
        json=body,
        params=params,
        timeout=_HTTP_TIMEOUT_S,
        headers=_auth_headers(),
    )
    if resp.status_code == 400:
        # The gateway rejects unknown / read-only fields with a 400 + detail.
        raise _ConfigError(resp.json().get("detail", resp.text))
    resp.raise_for_status()
    return ConfigWriteResult.model_validate(resp.json())


class _ConfigError(Exception):
    """A user-facing config error (bad key, read-only field, gateway 400)."""


@dataclass(frozen=True)
class _LocalConfigField:
    """Settings-free field metadata needed by direct `.env` operations."""

    name: str
    env_var: str
    field_type: str
    choices: list[str] | None
    default_value: object
    writable: bool
    sensitive: bool
    scope: str
    restart_required: str


def _field_extra(field_info: Any) -> dict[str, Any]:
    extra = field_info.json_schema_extra
    return cast("dict[str, Any]", extra) if isinstance(extra, dict) else {}


def _local_fields() -> dict[str, _LocalConfigField]:
    """Build local-edit metadata from the registry, never from Settings values."""
    from shared.config_registry import FIELD_INFOS, field_alias, field_editor_type

    fields: dict[str, _LocalConfigField] = {}
    for name, info in FIELD_INFOS.items():
        extra = _field_extra(info)
        default = info.get_default(call_default_factory=True)
        if default is PydanticUndefined:
            default = None
        if isinstance(default, Path):
            default = str(default)
        field_type, choices = field_editor_type(info.annotation)
        fields[name] = _LocalConfigField(
            name=name,
            env_var=field_alias(name),
            field_type=field_type,
            choices=choices,
            default_value=default,
            writable=extra.get("writable") is True,
            sensitive=extra.get("sensitive") is True,
            scope=str(extra["scope"]),
            restart_required=str(extra.get("restart_required", "")),
        )
    return fields


def _index_local_fields(
    fields: dict[str, _LocalConfigField],
) -> dict[str, _LocalConfigField]:
    index: dict[str, _LocalConfigField] = {}
    for field in fields.values():
        index[field.name] = field
        index[field.env_var] = field
    return index


def _resolve_local_field(key: str, index: dict[str, _LocalConfigField]) -> _LocalConfigField:
    field = index.get(key)
    if field is None:
        raise _ConfigError(
            f"unknown config key: {key!r} (use the env-var name like AVA_MODEL or the field name)"
        )
    return field


def _config_source_is_local() -> bool:
    from shared.bootstrap import config_source_is_local

    return config_source_is_local()


def _check_local_writable(field: _LocalConfigField) -> None:
    if not field.writable:
        raise _ConfigError(f"{field.env_var} is read-only")
    if field.scope == "host":
        return
    if field.scope in ("cluster-pinned", "cluster-default"):
        if _config_source_is_local():
            return
        raise _ConfigError(
            f"{field.env_var} is cluster config fetched from the gateway on this unit; "
            "use `ava config set` without `--local` to edit it there"
        )
    raise _ConfigError(f"{field.env_var} is not writable on this unit")


def _local_shown_value(field: _LocalConfigField, aliases: dict[str, str]) -> object:
    value = aliases.get(field.env_var, field.default_value)
    if field.sensitive and value not in (None, ""):
        return "••••••••"
    return "(empty)" if value in (None, "") else value


def _reject_local_machine(machine: str | None, verb: str) -> int | None:
    if machine is None:
        return None
    print(f"[ava config {verb}] --local cannot be combined with --machine", file=sys.stderr)
    return 1


def _local_get(key: str | None) -> int:
    from shared import runtime_config

    aliases = runtime_config.read_env_aliases()
    fields = _local_fields()
    if key is not None:
        try:
            field = _resolve_local_field(key, _index_local_fields(fields))
        except _ConfigError as e:
            print(f"[ava config get] {e}", file=sys.stderr)
            return 1
        print(f"{field.env_var} ({field.name}) = {_local_shown_value(field, aliases)}")
        print(f"  scope={field.scope}  restart_required={field.restart_required or '(none)'}")
        return 0

    name_w = max((len(field.env_var) for field in fields.values()), default=4)
    for field in sorted(fields.values(), key=lambda item: (item.scope, item.name)):
        marker = "*" if field.env_var in aliases else " "
        print(f"{marker} {field.env_var.ljust(name_w)}  {_local_shown_value(field, aliases)}")
    print("\n(* = explicitly set in .env; others are at their default)")
    return 0


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


def _field_editable(field: ConfigFieldView, *, remote: bool) -> bool:
    # Mirrors shared.config.editing.field_editable. The CLI has the wire view,
    # not ConfigFieldMeta, so these two definitions must stay in lockstep.
    return field.remote_writable if remote and field.scope == "host" else field.writable


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


def cmd_config_get(key: str | None, machine: str | None, *, local: bool = False) -> int:
    """`ava config get [KEY] [--machine M] [--local]` — print config values."""
    if local:
        rejected = _reject_local_machine(machine, "get")
        return rejected if rejected is not None else _local_get(key)
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
    view: ConfigView,
    pairs: dict[str, Any] | None,
    unset_keys: list[str] | None,
    *,
    machine: str | None,
) -> dict[str, Any]:
    """Build a merge patch carrying ONLY the changed keys (reducer semantics).

    A set is `{field: value}`; an unset is `{field: None}` (explicit deletion).
    The endpoint leaves any key not in the body untouched, so we send just the
    delta — never the whole override set — and deletion is the explicit None, not
    an omission. `view` is used only to resolve key aliases and gate read-only edits.

    Raises:
        _ConfigError: an unknown key, or an edit of a read-only field.
    """
    index = _index_fields(view)
    body: dict[str, Any] = {}
    for key, raw in (pairs or {}).items():
        field = _resolve_field(key, index)
        if not _field_editable(field, remote=machine is not None):
            raise _ConfigError(f"{field.env_var} is read-only")
        body[field.name] = _coerce(field.field_type, raw)
    for key in unset_keys or []:
        field = _resolve_field(key, index)
        if not _field_editable(field, remote=machine is not None):
            raise _ConfigError(f"{field.env_var} is read-only")
        body[field.name] = None
    return body


def _build_local_patch(
    pairs: dict[str, str] | None, unset_keys: list[str] | None
) -> tuple[dict[str, object], set[str], list[_LocalConfigField]]:
    """Resolve, gate, and coerce one direct `.env` patch before validation."""
    from shared.config.editing import coerce_config_scalar

    index = _index_local_fields(_local_fields())
    writes: dict[str, object] = {}
    removals: set[str] = set()
    changed: list[_LocalConfigField] = []
    for key, raw in (pairs or {}).items():
        field = _resolve_local_field(key, index)
        _check_local_writable(field)
        try:
            writes[field.name] = coerce_config_scalar(field.field_type, raw, field.choices)
        except (TypeError, ValueError):
            raise _ConfigError(f"invalid {field.field_type} value: {raw!r}") from None
        changed.append(field)
    for key in unset_keys or []:
        field = _resolve_local_field(key, index)
        _check_local_writable(field)
        removals.add(field.name)
        changed.append(field)
    return writes, removals, changed


def _edit_local_config(
    pairs: dict[str, str] | None, unset_keys: list[str] | None, verb: str
) -> int:
    """Validate and persist one local `.env` patch without booting Settings."""
    from shared import runtime_config
    from shared.config.candidate import validate_env_patch_for_write

    try:
        writes, removals, changed = _build_local_patch(pairs, unset_keys)
    except _ConfigError as e:
        print(f"[ava config {verb}] {e}", file=sys.stderr)
        return 1

    candidate = validate_env_patch_for_write(writes, removals)
    if candidate.errors:
        for error in candidate.errors:
            print(f"[ava config {verb}] {error}", file=sys.stderr)
        return 1

    try:
        runtime_config.write_fields(
            writes,
            removals,
            expected_digest=candidate.expected_digest,
            audit_site="cli_config_local",
        )
    except RuntimeError as e:
        print(f"[ava config {verb}] {e}; retry against the current .env", file=sys.stderr)
        return 1
    restart_required = sorted(
        {field.restart_required for field in changed if field.restart_required}
    )
    print(f"[ava config {verb}] applied.")
    _print_restart_hint(
        ConfigWriteResult(applied=True, results={}, restart_required=restart_required), machine=None
    )
    return 0


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
        body = _build_delta(view, pairs, unset_keys, machine=machine)
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


def cmd_config_set(assignments: list[str], machine: str | None, *, local: bool = False) -> int:
    """`ava config set KEY=VALUE [KEY=VALUE ...] [--machine M] [--local]`."""
    pairs: dict[str, str] = {}
    for item in assignments:
        if "=" not in item:
            print(f"[ava config set] expected KEY=VALUE, got {item!r}", file=sys.stderr)
            return 1
        key, value = item.split("=", 1)
        pairs[key.strip()] = value
    if local:
        rejected = _reject_local_machine(machine, "set")
        return rejected if rejected is not None else _edit_local_config(pairs, None, "set")
    return _edit_config(pairs, None, machine, "set")


def cmd_config_unset(keys: list[str], machine: str | None, *, local: bool = False) -> int:
    """`ava config unset KEY [KEY ...] [--machine M] [--local]` — revert fields."""
    if local:
        rejected = _reject_local_machine(machine, "unset")
        return rejected if rejected is not None else _edit_local_config(None, keys, "unset")
    return _edit_config(None, keys, machine, "unset")


# argparse handlers (co-located with the commands; wired by the cli/parsers tree)


def h_config_get(args: argparse.Namespace) -> int:
    return cmd_config_get(key=args.key, machine=args.machine, local=args.local)


def h_config_set(args: argparse.Namespace) -> int:
    return cmd_config_set(assignments=args.assignments, machine=args.machine, local=args.local)


def h_config_unset(args: argparse.Namespace) -> int:
    return cmd_config_unset(keys=args.keys, machine=args.machine, local=args.local)
