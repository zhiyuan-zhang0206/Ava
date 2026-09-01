"""Host-scope config read / write RPC ops.

Read this machine's host-scope config fields (sensitive values masked) and apply
a validated all-or-nothing override write. One of the four op clusters split out
of the former single `ops/operations.py` (the others are ops_lifecycle /
ops_cluster / ops_inventory); each cluster is self-contained.

Dispatched by the agent-runner ops server (`services/agent_ops/daemon.py`) and
called by the gateway config router. `SENSITIVE_MASK` is re-imported by the
router to render masked fields consistently.
"""

from __future__ import annotations

from typing import Any

from ops.rpc_schemas import (
    ConfigReadResult,
    ConfigWriteOpResult,
    FieldWriteResult,
    HostConfigField,
)
from shared import host_config_validators, runtime_config
from shared.config import env_override_values, get_config_metadata
from shared.config.editing import field_editable, split_reducer_patch
from shared.machine import machine_name

SENSITIVE_MASK = "••••••••"


def config_read_op() -> ConfigReadResult:
    """Read this machine's host-scope config fields and return them with metadata.

    Returns a ConfigReadResult with:
      - machine: this machine's name
      - host_fields: one entry per host-scope field with effective value (sensitive
        fields masked), set-in-.env flag, remote_writable flag, and optional
        read-time capability hint
      - raw_overrides: the host's editable override set ({field: value} for the
        remotely-writable host fields set in this machine's .env), sensitive keys
        omitted (the frontend deltas against this to build a write payload)
    """
    metas = get_config_metadata()
    metas_by_name = {m.name: m for m in metas}
    set_fields = runtime_config.env_set_field_names()
    overrides = env_override_values()
    host_fields: dict[str, HostConfigField] = {}
    for meta in metas:
        if meta.scope != "host":
            continue
        value = meta.current_value
        if meta.sensitive and value:
            value = SENSITIVE_MASK
        cap = host_config_validators.read_time_capability(meta.name)
        host_fields[meta.name] = HostConfigField(
            value=value,
            overridden=meta.name in set_fields,
            remote_writable=meta.remote_writable,
            can_enable=cap.ok if cap is not None else None,
            reason=cap.reason if cap is not None else None,
        )

    raw_overrides = {
        name: value
        for name, value in overrides.items()
        if metas_by_name[name].scope == "host" and not metas_by_name[name].sensitive
    }

    return ConfigReadResult(
        machine=machine_name(),
        host_fields=host_fields,
        raw_overrides=raw_overrides,
    )


def config_write_op(overrides: dict[str, Any], *, local: bool = False) -> ConfigWriteOpResult:
    """Validate and merge this machine's host overrides into its .env (reducer semantics).

    `overrides` is a JSON-merge-patch over the host `.env`: a key with a value is
    set/replaced, a key mapped to `None` is unset (reverted to its default), and a
    key that is ABSENT is left untouched. Deletion is always explicit (a null
    value) — never inferred from absence — so a partial payload can never silently
    drop a field it did not mention (a full-replace once wiped a cluster's secrets
    that way). Validates every field against host-scope + editability checks, then
    the host-side capability validator (skipped for an unset — there is no value to
    validate). Only writes if every field passes (atomic). Returns per-field
    results, applied flag, and the union of restart_required values from the
    changed fields.

    `local` distinguishes a self-target edit (the gateway editing its OWN host
    fields) from a remote one. A host field is editable iff `writable` when local,
    else `remote_writable` — `writable` means "a human may edit it on its own host",
    `remote_writable` is the narrower allowlist for editing a *remote* host's field.
    The gate is `shared.config.editing.field_editable` — the same definition the
    gateway's PUT /api/config gate uses, so the two write paths cannot drift.
    """
    metas = {m.name: m for m in get_config_metadata()}

    results: dict[str, FieldWriteResult] = {}
    for field, value in overrides.items():
        if field not in metas:
            results[field] = FieldWriteResult(ok=False, reason="unknown field")
            continue
        meta = metas[field]
        if meta.scope != "host":
            results[field] = FieldWriteResult(ok=False, reason="not a host-scope field")
            continue
        if not field_editable(meta, local=local):
            results[field] = FieldWriteResult(
                ok=False, reason="not editable" if local else "not remotely editable"
            )
            continue
        if value is None:
            # Explicit unset — no value to validate.
            results[field] = FieldWriteResult(ok=True, reason=None)
            continue
        vr = host_config_validators.validate(field, value)
        results[field] = FieldWriteResult(ok=vr.ok, reason=vr.reason)

    # all() over an empty dict is True, so an empty payload is trivially applied.
    applied = all(r.ok for r in results.values())

    restart_required: list[str] = []
    if applied:
        writes, removals = split_reducer_patch(overrides, metas)
        if writes or removals:
            runtime_config.write_fields(writes, removals, audit_site="ops_config_write")
        restart_required = sorted(
            {metas[f].restart_required for f in set(writes) | removals if metas[f].restart_required}
        )

    return ConfigWriteOpResult(
        machine=machine_name(),
        results=results,
        applied=applied,
        restart_required=restart_required,
    )
