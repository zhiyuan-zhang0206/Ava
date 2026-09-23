#!/usr/bin/env python
"""Generate ``shared/config_lite_table.json`` — the boot-lite static config index.

Single source of truth: the field declarations behind ``shared/config_registry``
(name / domain / env alias / per-agent flag / default) plus the explicit
``LITE_MANIFEST`` below. The generated index carries:

- ``lite_fields`` — the boot-path fields ``shared/config/_lite.py`` resolves
  without pydantic, with the parse kind, the default kind, and any
  field-specific validity rule;
- ``field_domains`` / ``field_aliases`` / ``field_scopes`` /
  ``field_capabilities`` / ``per_agent_fields`` — the all-field indexes the
  facade accessors (``field_alias`` / ``field_domain`` / ``field_names`` /
  ``per_agent_field_names``) and ``shared/env_registry.py``'s authority
  projections serve without building the registry.

The manifest is the ONLY admission gate: a field not listed here still works —
reading it upgrades to the eager config chain (correct, just heavier). Adding a
row is a deliberate decision to keep that field's read on the boot path, so each
row carries the read-site evidence for the next reader.

JSON, not a generated ``.py`` module: the all-field faces alone (391 fields x
several columns) blow past the repo's 800-line hard ceiling
(``scripts/lint_code_structure.py``, no new baseline entries), and that rule's remedy —
split into focused modules — does not fit one machine-generated table whose
columns are never read as separate units. A data file carries no line budget
(precedent: ``shared/lm/pricing_catalog_archive.json``);
``shared/config_lite_table.py`` is the hand-written reader that materializes the
named surfaces consumers import.

Index and reader live OUTSIDE the ``shared.config`` package on purpose, like
``shared/config_registry.py``: ``shared/env_registry.py`` (the env-authority
projections that ``load_ava_env`` runs before Settings exists) imports it, and a
package submodule import would execute the ``shared.config`` facade first —
re-entering the boot it is part of.

Run after changing any config field, or let the pre-commit
``config-lite-table-fresh`` hook fail loud:

    .venv/bin/python scripts/gen_config_lite_table.py

The script is its own repair tool, designed to run in exactly the states that
need it: it boots the config package under ``AVA_CONFIG_FETCH=skip`` (no .env
load, no authority pass, no gateway fetch) and recreates a missing or corrupt
index as an empty stub before reading the registry.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, NamedTuple, cast

from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

# Put project root on sys.path so `from shared... import ...` finds this
# checkout's modules (same pattern as gen_event_registry.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_OUT = Path("shared/config_lite_table.json")
_EMPTY_INDEX: dict[str, object] = {
    "_generated": (
        "BOOTSTRAP STUB — regenerate with `.venv/bin/python scripts/gen_config_lite_table.py`."
    ),
    "lite_fields": {},
    "field_domains": {},
    "field_aliases": {},
    "field_scopes": {},
    "field_capabilities": {},
    "per_agent_fields": [],
    "required_fields": [],
}


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a same-directory temp file + os.replace.

    A reader must never observe a half-written index: the config package
    imports this file at boot and CI diffs it byte-for-byte, so the new
    content lands complete or not at all."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


def _ensure_index_present() -> None:
    """Recreate the index when it is missing or corrupt — with an empty stub.

    The config package cannot be imported without this file (its reader loads
    it), so this script's own registry import needs it present, and the one
    state that most needs regeneration must not be the one that cannot run.
    The stub only has to be valid JSON carrying every key."""
    if _OUT.exists():
        try:
            json.loads(_OUT.read_text(encoding="utf-8"))
            return
        except ValueError:
            pass
    _atomic_write_text(_OUT, json.dumps(_EMPTY_INDEX, indent=2) + "\n")


# Regeneration must be as repairable as the settings-lite verbs: a broken .env
# (or an unreachable gateway) must not block the tool that regenerates the
# config index. `AVA_CONFIG_FETCH=skip` stops the config package's import tail
# before its env work (load_ava_env / the authority pass); the index is then
# read for nothing but the surfaces this script rewrites anyway. The registry
# data itself comes from the model declarations, never from the environment.
os.environ["AVA_CONFIG_FETCH"] = "skip"
_ensure_index_present()

from shared.config_registry import (  # noqa: E402 — must follow the bootstrap above
    _build_registry,
    _schema_extra,
    field_alias,
)


class LiteField(NamedTuple):
    """One manifest row: the field, its default source, its validity rule, and why."""

    name: str
    default_kind: (
        str  # literal | none | factory_empty_list | path_home_ava | otel_endpoint_from_port
    )
    check: str | None  # None | iana | port | eval_allowlist — a named rule in _lite.py
    why: str  # the read site that put this field on the boot path


# The boot-path fields. Order = the table's order (curated). Every row's
# `check` names a validity rule that must exist in `shared/config/_lite.py`;
# a field whose declaration carries a validator lists the matching rule (the
# parity test locks behavior against the eager path, the drift test locks this
# table against the live registry).
LITE_MANIFEST: tuple[LiteField, ...] = (
    LiteField(
        "ava_home",
        "path_home_ava",
        None,
        "paths.ava_home() — ~19 boot reads (skills/plugins/memory)",
    ),
    LiteField(
        "timezone",
        "literal",
        "iana",
        "cluster clock: cluster_tz_name/format_timestamp/apply_cluster_timezone",
    ),
    LiteField("message_timestamp_weekday", "literal", None, "format_timestamp weekday rendering"),
    LiteField("machine_name", "literal", None, "shared/machine.py identity reads"),
    LiteField("machine_serve_gateway", "none", None, "machine_role() capability detection"),
    LiteField("machine_serve_agent_runner", "none", None, "machine_role() capability detection"),
    LiteField(
        "machine_serve_observability_station", "none", None, "machine_role() capability detection"
    ),
    LiteField(
        "gateway_client_max_retries",
        "literal",
        None,
        "ava/_gateway_transport.py module level (:66)",
    ),
    LiteField(
        "gateway_client_retry_delay_seconds",
        "literal",
        None,
        "ava/_gateway_transport.py module level (:67)",
    ),
    LiteField("gateway_url", "literal", None, "sdk_call_policy / gateway transport"),
    LiteField("db_pool_acquire_timeout_seconds", "literal", None, "agent/db.py:28 module level"),
    LiteField("eval_isolation", "literal", None, "_process_boot._apply_per_agent_eval_isolation"),
    LiteField(
        "eval_network_allowlist",
        "factory_empty_list",
        "eval_allowlist",
        "eval isolation network gate (conditional: isolation on)",
    ),
    LiteField("llm_model", "literal", None, "ava/_attach.py model gate + lifecycle reads"),
    LiteField("telemetry_otlp_enabled", "literal", None, "shared/telemetry_otlp boot read"),
    LiteField(
        "telemetry_otlp_endpoint",
        "otel_endpoint_from_port",
        None,
        "telemetry_otlp export target (derived from the port when unset)",
    ),
    LiteField(
        "telemetry_otlp_port",
        "literal",
        "port",
        "support row: telemetry_otlp_endpoint's derived default input",
    ),
    LiteField("sdk_call_sampling_enabled", "literal", None, "sdk_call_policy"),
    LiteField("sdk_call_sample_every", "literal", None, "sdk_call_policy"),
    LiteField(
        "events_jsonl_rollup_retention_days", "literal", None, "telemetry._write_batch retention"
    ),
    LiteField(
        "sdk_disable",
        "factory_empty_list",
        None,
        "exec child boot: _apply_per_agent_sdk_disable reads it after the overlay",
    ),
    # The OS-scheduled hold watchdog (task #3887): its one-shot job process is a
    # settings-lite verb (cli.main sets AVA_CONFIG_FETCH=skip for `cluster`), and
    # it must resolve these while the database and gateway are down - that is the
    # full-stop shape it exists for. Read order (pending > env/.env > default) is
    # also the kill-switch contract: the unit's .env or the process environment
    # overrides, and the compiled default is the full-stop answer.
    LiteField(
        "stranded_hold_recovery",
        "literal",
        None,
        "shared/hold_watchdog.py enabled() - the hold watchdog's kill-switch",
    ),
    LiteField(
        "hold_watchdog_min_age_seconds",
        "literal",
        None,
        "shared/hold_watchdog.py min_age_seconds() - the completion bound floor",
    ),
    LiteField(
        "hold_watchdog_cooldown_seconds",
        "literal",
        None,
        "shared/hold_watchdog.py cooldown_seconds() - the per-generation cooldown",
    ),
    # The caller-side stop-incomplete recovery (task #3942): the updater leg
    # reads the pair at its post-stop failure exit - the moment the host is
    # mid-stop and the data plane may be down - so both must resolve without a
    # full config build. Same kill-switch contract as the rows above: pending
    # override > env/.env > the compiled default.
    LiteField(
        "stop_incomplete_recovery",
        "literal",
        None,
        "cli/commands/_update_stop_recovery.py _recovery_enabled() - the caller arm's switch",
    ),
    LiteField(
        "stop_incomplete_recovery_timeout_seconds",
        "literal",
        None,
        "cli/commands/_update_stop_recovery.py _attempt_timeout_s() - the attempt's deadline",
    ),
)

# The named validity rules `_lite.py` implements for the `check` column.
_ALLOWED_CHECKS = frozenset({"iana", "port", "eval_allowlist"})
_ALLOWED_DEFAULT_KINDS = frozenset(
    {"literal", "none", "factory_empty_list", "path_home_ava", "otel_endpoint_from_port"}
)


def _kind(annotation: object) -> str:
    """Map a field annotation to a `_lite.py` parse kind — fail loud on anything new."""
    if annotation is str:
        return "str"
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    if annotation is Path:
        return "path"
    if annotation == (bool | None):
        return "bool_or_none"
    if getattr(annotation, "__origin__", None) is list and getattr(
        annotation, "__args__", None
    ) == (str,):
        return "csv_str_list"
    raise SystemExit(
        f"lite field annotation {annotation!r} has no parse kind — extend _kind() and the documented "
        f"kinds in shared/config/_lite.py before adding this field to LITE_MANIFEST"
    )


def _default(row: LiteField, info: FieldInfo, reg: dict[str, Any]) -> tuple[str, object]:
    """Resolve (default kind, literal default), asserting the manifest matches the field.

    `info` is the field's pydantic `FieldInfo` (the registry stores it behind a
    narrow Protocol so it stays pydantic-free; this script re-narrows it)."""
    if row.default_kind not in _ALLOWED_DEFAULT_KINDS:
        raise SystemExit(f"{row.name!r}: unknown default_kind {row.default_kind!r}")
    default: object = info.default
    if row.default_kind == "literal":
        if default is None or default is PydanticUndefined:
            raise SystemExit(
                f"{row.name!r}: default_kind=literal but the field has no literal default"
            )
        if not isinstance(default, (str, bool, int, float)):
            raise SystemExit(
                f"{row.name!r}: literal default of type {type(default).__name__} is unsupported — "
                f"add a named default kind"
            )
        return "literal", default
    if row.default_kind == "none":
        if default is not None:
            raise SystemExit(
                f"{row.name!r}: default_kind=none but the field default is {default!r}"
            )
        return "none", None
    if row.default_kind == "factory_empty_list":
        if default is not PydanticUndefined or info.default_factory is not list:
            raise SystemExit(
                f"{row.name!r}: default_kind=factory_empty_list but factory/default is "
                f"{info.default_factory!r}/{default!r}"
            )
        return "factory_empty_list", None
    if row.default_kind == "path_home_ava":
        if default != Path.home() / ".ava":
            raise SystemExit(
                f"{row.name!r}: default_kind=path_home_ava but the field default is {default!r}"
            )
        return "path_home_ava", None
    # otel_endpoint_from_port — the field default must really be the port-derived URL.
    port_ref = reg.get("telemetry_otlp_port")
    if port_ref is None or not any(f.name == "telemetry_otlp_port" for f in LITE_MANIFEST):
        raise SystemExit(
            "default_kind=otel_endpoint_from_port requires telemetry_otlp_port in the manifest"
        )
    port = port_ref.info.default
    if default != f"http://127.0.0.1:{port}":
        raise SystemExit(
            f"{row.name!r}: default {default!r} is not f'http://127.0.0.1:{{port}}' of the live port "
            f"default {port!r}"
        )
    return "otel_endpoint_from_port", None


# One `lite_fields` row: (domain, env alias, parse kind, default kind,
# literal default | None, validity check | None).
_LiteRow = tuple[str, str, str, str, object, str | None]


def _collect() -> tuple[
    list[tuple[str, _LiteRow]],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    list[str],
    list[str],
]:
    """Validate the manifest against the live registry and collect every table row."""
    reg = _build_registry()
    seen: set[str] = set()
    lite: list[tuple[str, _LiteRow]] = []
    for row in LITE_MANIFEST:
        if row.name in seen:
            raise SystemExit(f"{row.name!r} appears twice in LITE_MANIFEST")
        seen.add(row.name)
        if row.check is not None and row.check not in _ALLOWED_CHECKS:
            raise SystemExit(
                f"{row.name!r}: unknown check {row.check!r}; allowed: {sorted(_ALLOWED_CHECKS)}"
            )
        ref = reg.get(row.name)
        if ref is None:
            raise SystemExit(
                f"{row.name!r} is in LITE_MANIFEST but not in the live config registry"
            )
        info = cast(FieldInfo, ref.info)
        default_kind, value = _default(row, info, reg)
        lite.append(
            (
                row.name,
                (
                    ref.domain,
                    field_alias(row.name),
                    _kind(info.annotation),
                    default_kind,
                    value,
                    row.check,
                ),
            )
        )
    # Registry insertion order IS eager construction order (the build walks the
    # domains in Settings-aggregate order and each model's fields in declaration
    # order) — keeping LITE_FIELDS in that order makes prepare's fail-fast
    # validate fields in the same order the eager path constructs them, so the
    # first failing boot-path field is the same field either way (#3621 /
    # adversarial review MAJ-1).
    order = {name: idx for idx, name in enumerate(reg)}
    lite.sort(key=lambda item: order[item[0]])
    names = sorted(reg)
    domains = {name: reg[name].domain for name in names}
    aliases = {name: field_alias(name) for name in names}
    scopes = {name: str(_schema_extra(reg[name].info).get("scope")) for name in names}
    capabilities = {name: reg[name].capability for name in names}
    per_agent = [name for name in names if _schema_extra(reg[name].info).get("per_agent") is True]
    required: list[str] = []
    for name in names:
        info = cast(FieldInfo, reg[name].info)
        if info.default is PydanticUndefined and info.default_factory is None:
            required.append(name)
    return lite, domains, aliases, scopes, capabilities, per_agent, required


def render() -> str:
    """Render the whole config-lite index as JSON (byte-stable; `--check` diffs it)."""
    lite, domains, aliases, scopes, capabilities, per_agent, required = _collect()
    payload: dict[str, object] = {
        "_generated": (
            "DO NOT EDIT — generated by scripts/gen_config_lite_table.py; regenerate with "
            "`.venv/bin/python scripts/gen_config_lite_table.py`."
        ),
        "lite_fields": {name: list(row) for name, row in lite},
        "field_domains": domains,
        "field_aliases": aliases,
        "field_scopes": scopes,
        "field_capabilities": capabilities,
        "per_agent_fields": per_agent,
        "required_fields": required,
    }
    return json.dumps(payload, indent=2) + "\n"


def main(*, check: bool = False, out: str | None = None) -> int:
    rendered = render()
    target = Path(out) if out else _OUT
    if check:
        current = target.read_text(encoding="utf-8")
        if current != rendered:
            print(
                "ERROR: shared/config_lite_table.json is out of sync with the config registry.\n"
                "   run .venv/bin/python scripts/gen_config_lite_table.py to regenerate"
            )
            return 1
        print(f"{target} is up to date")
        return 0
    _atomic_write_text(target, rendered)
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(main(check="--check" in sys.argv, out=args[0] if args else None))
