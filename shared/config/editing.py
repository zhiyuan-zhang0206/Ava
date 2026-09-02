"""Write-path config policy shared by the gateway router and the ops config op.

`put_config` (gateway/routers/config.py) and `config_write_op`
(ops/ops_config.py) each implemented the same editability gate and
JSON-merge-patch reducer in their own shape; this module is the single
definition both call so the policy cannot drift apart again. Everything here is
pure (no IO, no HTTP) — callers translate the plan's violations into their own
400s.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import CONFIG_UNCHANGED_SENTINEL, ConfigFieldMeta

__all__ = [
    "ConfigPatchPlan",
    "coerce_config_scalar",
    "field_editable",
    "split_reducer_patch",
]


def field_editable(meta: ConfigFieldMeta, *, local: bool) -> bool:
    """Whether `meta` may be edited by the config write path at all.

    One definition for both write paths: the gateway's PUT /api/config field
    gate and the host-side config_write_op's per-field gate. A host-scope field
    is editable on its own host iff `writable`, and through a machine-addressed
    (remote) edit iff `remote_writable` — `writable` means "a human may edit it
    on its own host", `remote_writable` is the narrower allowlist for editing a
    *remote* host's field. A non-host field (cluster / agent scope) is editable
    iff `writable` regardless of locality; agent-scope fields are writable=False
    by construction, so they are rejected here.
    """
    if meta.scope == "host":
        return meta.writable if local else meta.remote_writable
    return meta.writable


_BOOL_TRUE = frozenset({"true", "1", "yes", "on"})
_BOOL_FALSE = frozenset({"false", "0", "no", "off"})


def coerce_config_scalar(
    field_type: str, value: object, choices: list[str] | None = None
) -> object:
    """Coerce a cluster write value to its scalar field type, or raise ValueError.

    String / list fields pass through unchanged. Rejects a bool for an int/float
    field and a float for an int field, so a wrong-typed JSON value cannot pass and
    then be stringified into .env as e.g. "true" or "1.2" (which breaks Settings at
    the next restart). An enum field rejects a value outside its choices, so an
    unknown enum can never be written to .env and blow up Settings at the next
    restart (fail-fast at the write site, not the far-away consumer). The returned
    value is what gets written, so .env carries the normalized form.
    """
    if field_type == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _BOOL_TRUE:
            return True
        if text in _BOOL_FALSE:
            return False
        raise ValueError
    if field_type == "int":
        if isinstance(value, (bool, float)):
            raise ValueError
        return int(value)  # type: ignore[arg-type]
    if field_type == "float":
        if isinstance(value, bool):
            raise ValueError
        return float(value)  # type: ignore[arg-type]
    if field_type == "enum":
        if not isinstance(value, str) or (choices is not None and value not in choices):
            raise ValueError
        return value
    return value


def split_reducer_patch(
    fields: dict[str, object], metas: dict[str, ConfigFieldMeta]
) -> tuple[dict[str, object], set[str]]:
    """Split a JSON-merge-patch body into (writes, removals), reducer semantics.

    A key with a value is set/replaced, a key mapped to None is unset (reverted
    to its default), and an ABSENT key is left untouched. A sensitive field
    carrying the unchanged-sentinel is neither written nor unset ("keep as-is").
    Deletion is only ever the explicit None, never inferred from a key's
    absence, so a partial PUT can never drop a field it did not name (a
    full-replace once wiped a cluster's secrets that way).

    Shared by the cluster-side PUT reducer and the host-side config_write_op —
    the host side gains the sentinel guard here too (the frontend never
    sends a sentinel for a host field today, but the two PITR OSS credential
    paths are host-scoped and sensitive, so the guard now covers them as
    well).
    """
    writes = {
        k: v
        for k, v in fields.items()
        if v is not None and not (metas[k].sensitive and v == CONFIG_UNCHANGED_SENTINEL)
    }
    removals = {k for k, v in fields.items() if v is None}
    return writes, removals


@dataclass(frozen=True)
class ConfigPatchPlan:
    """A gated + routed + reduced PUT /api/config body, ready for IO.

    `parse` is pure: it applies the editability gate, routes keys by scope, runs
    the merge-patch reducer, and coerces cluster scalars — everything that can
    fail with a 400 — so the route handler is left with only the IO
    orchestration (host write first, cluster write iff applied). It never raises:
    problems are reported in `violations` (unknown / read-only field names),
    `remote_cluster_keys` (cluster keys on a machine-addressed PUT) and
    `scalar_error` (the first malformed cluster scalar's 400 detail).
    """

    host_body: dict[str, object]
    cluster_writes: dict[str, object]
    cluster_removals: set[str]
    violations: tuple[str, ...] = ()
    remote_cluster_keys: tuple[str, ...] = ()
    scalar_error: str | None = None

    @staticmethod
    def parse(
        body: dict[str, object],
        metas: dict[str, ConfigFieldMeta],
        *,
        is_remote: bool,
    ) -> ConfigPatchPlan:
        """Gate, route, reduce and coerce `body` against `metas`.

        `is_remote` is whether the PUT was machine-addressed (`?machine=`), which
        switches host-field editability from `writable` to `remote_writable` and
        forbids cluster keys altogether.
        """
        # Editability gate — one table-driven answer per field. A machine-addressed
        # edit of a host field requires remote_writable; a local edit requires
        # writable; non-host fields always require writable (agent-scope fields are
        # writable=False by construction).
        allowed = {name for name, m in metas.items() if field_editable(m, local=not is_remote)}
        violations = tuple(sorted(set(body) - allowed))
        host_body = {k: v for k, v in body.items() if k in allowed and metas[k].scope == "host"}
        cluster_body = {k: v for k, v in body.items() if k in allowed and metas[k].scope != "host"}
        if is_remote:
            # Cluster config is machine-independent; only host fields go. The
            # cluster keys are reported separately so the handler can 400 with the
            # machine-independent message rather than the read-only one.
            return ConfigPatchPlan(
                host_body=host_body,
                cluster_writes={},
                cluster_removals=set(),
                violations=violations,
                remote_cluster_keys=tuple(sorted(cluster_body)),
            )
        # Reducer + coerce before any write so a bad scalar 400s atomically
        # (nothing persisted) and .env carries the normalized typed value.
        cluster_writes, cluster_removals = split_reducer_patch(cluster_body, metas)
        coerced: dict[str, object] = {}
        scalar_error: str | None = None
        for name, value in cluster_writes.items():
            try:
                coerced[name] = coerce_config_scalar(
                    metas[name].field_type, value, metas[name].choices
                )
            except (ValueError, TypeError):
                scalar_error = f"{name}: invalid {metas[name].field_type} value {value!r}"
                break
        return ConfigPatchPlan(
            host_body=host_body,
            cluster_writes=coerced,
            cluster_removals=cluster_removals,
            violations=violations,
            scalar_error=scalar_error,
        )
