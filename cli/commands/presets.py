"""`ava presets` — manage agent config presets (thin client over the gateway).

Operate on named config templates: list, inspect, create, update, delete. Each
verb forwards to an existing gateway route (`GET/POST/PATCH/DELETE /api/presets`)
and renders the result. This exists so a host operator can manage presets from the
shell without curling the gateway or opening the web UI, and so agents can create
presets with a single `ava presets create ...` invocation.
"""

from __future__ import annotations

import contextlib
import json
import sys
from typing import Any

_TIMEOUT_S = 15.0


def _gateway_base() -> str:
    from shared.machine import gateway_api_base

    return gateway_api_base()


# ── list ──


def cmd_presets_ls() -> int:
    """`ava presets ls` — list all presets (id / name / label / description)."""
    from shared.http_dial import get as dial_get
    from shared.machine import gateway_auth_headers

    url = f"{_gateway_base()}/api/presets"
    resp = dial_get(url, timeout=_TIMEOUT_S, headers=gateway_auth_headers())
    resp.raise_for_status()
    rows = resp.json()

    if not rows:
        print("(no presets)")
        return 0

    id_w = max(len("id"), max((len(str(r["id"])) for r in rows), default=2))
    name_w = max(len("name"), max((len(r["name"]) for r in rows), default=4))
    print(f"{'id'.rjust(id_w)}  {'name'.ljust(name_w)}  label")
    for r in rows:
        label = r.get("label", "") or ""
        print(f"{str(r['id']).rjust(id_w)}  {r['name'].ljust(name_w)}  {label}")
    return 0


# ── get ──


def cmd_presets_get(identifier: str) -> int:
    """`ava presets get <name-or-id>` — fetch one preset by name or numeric id.

    Tries name match first (list + filter), then integer id. Prints the full
    preset including its config JSON."""
    from shared.http_dial import get as dial_get
    from shared.machine import gateway_auth_headers

    # Try numeric id first — fast path
    preset_id: int | None = None
    with contextlib.suppress(ValueError):
        preset_id = int(identifier)

    if preset_id is not None:
        url = f"{_gateway_base()}/api/presets/{preset_id}"
        resp = dial_get(url, timeout=_TIMEOUT_S, headers=gateway_auth_headers())
        if resp.status_code == 404:
            print(f"preset {preset_id} not found", file=sys.stderr)
            return 1
        resp.raise_for_status()
        row = resp.json()
    else:
        # Name lookup — list then filter
        list_url = f"{_gateway_base()}/api/presets"
        resp = dial_get(list_url, timeout=_TIMEOUT_S, headers=gateway_auth_headers())
        resp.raise_for_status()
        rows = resp.json()
        match = next((r for r in rows if r["name"] == identifier), None)
        if match is None:
            print(f"preset named {identifier!r} not found", file=sys.stderr)
            return 1
        row = match

    _print_preset(row)
    return 0


# ── create ──


def cmd_presets_create(
    name: str, label: str, description: str | None, config_json: str | None
) -> int:
    """`ava presets create --name N --label L [--description D] [--config JSON]`."""
    from shared.http_dial import post as dial_post
    from shared.machine import gateway_auth_headers

    config: dict[str, object] = {}
    if config_json:
        try:
            parsed = json.loads(config_json)
        except json.JSONDecodeError as e:
            print(f"invalid config JSON: {e}", file=sys.stderr)
            return 1
        if not isinstance(parsed, dict):
            print('config must be a JSON object, e.g. {"llm_model":"..."}', file=sys.stderr)
            return 1
        config = parsed

    body: dict[str, object] = {"name": name, "label": label, "config": config}
    if description is not None:
        body["description"] = description

    url = f"{_gateway_base()}/api/presets"
    resp = dial_post(url, json=body, timeout=_TIMEOUT_S, headers=gateway_auth_headers())
    if resp.status_code == 409:
        print(f"preset named {name!r} already exists", file=sys.stderr)
        return 1
    if resp.status_code >= 400:
        print(resp.text, file=sys.stderr)
        resp.raise_for_status()
    row = resp.json()
    print(f"  \u2713 created preset #{row['id']} ({row['name']})")
    _print_preset(row)
    return 0


# ── update ──


def cmd_presets_update(
    identifier: str,
    name: str | None,
    label: str | None,
    description: str | None,
    config_json: str | None,
) -> int:
    """`ava presets update <name-or-id> [--name N] [--label L] [--description D] [--config JSON]`.

    Partial update — only the fields passed change. At least one of --name / --label /
    --description / --config must be given."""
    from shared.http_dial import patch as dial_patch
    from shared.machine import gateway_auth_headers

    # Resolve identifier to id
    preset_id = _resolve_id(identifier)
    if preset_id is None:
        return 1

    body: dict[str, object] = {}
    if name is not None:
        body["name"] = name
    if label is not None:
        body["label"] = label
    if description is not None:
        body["description"] = description
    if config_json is not None:
        try:
            parsed = json.loads(config_json)
        except json.JSONDecodeError as e:
            print(f"invalid config JSON: {e}", file=sys.stderr)
            return 1
        if not isinstance(parsed, dict):
            print("config must be a JSON object", file=sys.stderr)
            return 1
        body["config"] = parsed

    if not body:
        print(
            "at least one of --name / --label / --description / --config is required",
            file=sys.stderr,
        )
        return 1

    url = f"{_gateway_base()}/api/presets/{preset_id}"
    resp = dial_patch(url, json=body, timeout=_TIMEOUT_S, headers=gateway_auth_headers())
    if resp.status_code == 404:
        print(f"preset {preset_id} not found", file=sys.stderr)
        return 1
    if resp.status_code == 409:
        detail = resp.json().get("detail", resp.text)
        print(f"name conflict: {detail}", file=sys.stderr)
        return 1
    if resp.status_code >= 400:
        print(resp.text, file=sys.stderr)
        resp.raise_for_status()
    row = resp.json()
    print(f"  \u2713 updated preset #{row['id']} ({row['name']})")
    _print_preset(row)
    return 0


# ── delete ──


def cmd_presets_delete(identifier: str, *, force: bool = False) -> int:
    """`ava presets delete <name-or-id> [--force]`.

    Prompts for confirmation unless --force. Does NOT affect agents already
    spawned from this preset — they carry a snapshot of their config at spawn
    time and are independent of the preset thereafter."""
    from shared.http_dial import delete as dial_delete
    from shared.machine import gateway_auth_headers

    preset_id = _resolve_id(identifier)
    if preset_id is None:
        return 1

    if not force:
        answer = input(f"Delete preset {identifier!r} (id={preset_id})? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("cancelled")
            return 0

    url = f"{_gateway_base()}/api/presets/{preset_id}"
    resp = dial_delete(url, timeout=_TIMEOUT_S, headers=gateway_auth_headers())
    if resp.status_code == 404:
        print(f"preset {preset_id} not found", file=sys.stderr)
        return 1
    resp.raise_for_status()
    print(f"  \u2713 deleted preset #{preset_id}")
    return 0


# ── helpers ──


def _resolve_id(identifier: str) -> int | None:
    """Resolve a name-or-id string to a numeric preset id. Returns None + prints
    to stderr on lookup failure."""
    from shared.http_dial import get as dial_get
    from shared.machine import gateway_auth_headers

    with contextlib.suppress(ValueError):
        return int(identifier)

    # Name lookup
    url = f"{_gateway_base()}/api/presets"
    resp = dial_get(url, timeout=_TIMEOUT_S, headers=gateway_auth_headers())
    resp.raise_for_status()
    rows = resp.json()
    match = next((r for r in rows if r["name"] == identifier), None)
    if match is None:
        print(f"preset named {identifier!r} not found", file=sys.stderr)
        return None
    return int(match["id"])


def _print_preset(row: dict[str, Any]) -> None:
    """Pretty-print a preset row."""
    print(f"  name:        {row['name']}")
    print(f"  label:       {row.get('label', '')}")
    desc = row.get("description")
    if desc:
        print(f"  description: {desc}")
    config = row.get("config", {})
    if config:
        print(f"  config:      {json.dumps(config, indent=4)}")
    else:
        print("  config:      {}")


# ── argparse handlers (wired by the cli/parsers tree) ──

import argparse  # noqa: E402


def h_presets_ls(_args: argparse.Namespace) -> int:
    return cmd_presets_ls()


def h_presets_get(args: argparse.Namespace) -> int:
    return cmd_presets_get(args.identifier)


def h_presets_create(args: argparse.Namespace) -> int:
    return cmd_presets_create(args.name, args.label, args.description, args.config)


def h_presets_update(args: argparse.Namespace) -> int:
    return cmd_presets_update(args.identifier, args.name, args.label, args.description, args.config)


def h_presets_delete(args: argparse.Namespace) -> int:
    return cmd_presets_delete(args.identifier, force=args.force)
