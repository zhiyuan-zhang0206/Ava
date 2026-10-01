"""Setup-field resolution (the home's `.env`, read through Settings).

Called by `cmd_start`. Resolution never writes; the home's durable identity is
persisted by `ava init` (`cli/init_intent.py`, `cli/start_identity.py`), so starts take
no identity input.

Capabilities (serve_gateway / serve_agent_runner / serve_observability_station)
are independent booleans, each the settings bool of its `AVA_MACHINE_SERVE_*` key
(unset = off); the string setup fields (machine_name / description / memory_remote /
gateway_url) are resolved by `_SetupField` below, gated by which capabilities this
host carries.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import NotRequired, TypedDict, cast

from base.config import get_field


class SetupValues(TypedDict):
    """The resolved host setup fields `_collect_setup_values` returns and
    `_register_machine_or_die` / `ava start` consume. `machine_role` (the derived
    comma-string of this host's capabilities), `machine_name`, and `gateway_url`
    are guaranteed once the returned `missing` list is empty — a required field
    that failed to resolve lands in `missing`, which the caller checks first. The
    two `optional=True` fields are NotRequired (absent unresolved). The empty-caps
    early return is an empty dict cast to this type; the caller's `missing` check
    short-circuits before any consumer reads a value."""

    machine_role: str
    machine_name: str
    gateway_url: str
    machine_description: NotRequired[str]
    memory_remote: NotRequired[str]


@dataclass(frozen=True)
class _SetupField:
    """Metadata for a single setup field — used by the collector and error message.

    Attributes:
        name: `settings.<name>` Python attribute
        env_var: equivalent env var (e.g. `AVA_MACHINE_NAME`)
        hint: value hint shown in error messages (e.g. "<name>, e.g. host-a")
        roles: tuple of capability values this field is required by (("gateway",), ("agent-runner",), or both)
        optional: missing field does not enter the missing-error list; caller decides fallback
        validator: optional, value validity check; raises a ValueError subclass
    """

    name: str
    env_var: str
    hint: str
    roles: tuple[str, ...] = ("gateway", "agent-runner")
    optional: bool = False
    validator: Callable[[str], None] | None = None


@dataclass(frozen=True)
class _Capability:
    """Metadata for one serve-capability flag — a boolean (serve or not), not a
    string field. Resolved from the settings bool (unset = off).

    Attributes:
        capability: the capability token this flag declares ("gateway" /
            "agent-runner" / "observability-station")
        env_var: the `.env` key (e.g. `AVA_MACHINE_SERVE_GATEWAY`)
        settings_attr: `settings.<attr>` (bool | None — None = env unset)
    """

    capability: str
    env_var: str
    settings_attr: str


_CAPABILITIES: tuple[_Capability, ...] = (
    _Capability(
        capability="gateway",
        env_var="AVA_MACHINE_SERVE_GATEWAY",
        settings_attr="machine_serve_gateway",
    ),
    _Capability(
        capability="agent-runner",
        env_var="AVA_MACHINE_SERVE_AGENT_RUNNER",
        settings_attr="machine_serve_agent_runner",
    ),
    _Capability(
        capability="observability-station",
        env_var="AVA_MACHINE_SERVE_OBSERVABILITY_STATION",
        settings_attr="machine_serve_observability_station",
    ),
)


_SETUP_FIELDS: tuple[_SetupField, ...] = (
    _SetupField(
        name="machine_name",
        env_var="AVA_MACHINE_NAME",
        hint="<name>, e.g. host-a / host-b",
    ),
    _SetupField(
        name="machine_description",
        env_var="AVA_MACHINE_DESCRIPTION",
        hint='<free text>, e.g. "voice IO + browser + always-on"',
        optional=True,
    ),
    _SetupField(
        name="memory_remote",
        env_var="AVA_MEMORY_REMOTE",
        hint="<git-url>, e.g. git@github.com:you/AvaMemory.git (empty = local init, no remote)",
        optional=True,
    ),
    # gateway_url is the URL of the gateway.
    # On the gateway, this is the URL advertised to the cluster (register_self).
    # On an agent-runner, this is the gateway it reaches for self-heal updates
    # and cluster status (the gateway dials the agent-runner the other way).
    _SetupField(
        name="gateway_url",
        env_var="AVA_GATEWAY_URL",
        hint="<url>, e.g. http://<gateway-host>:8800 (gateway: this host's own URL; agent-runner: the gateway it reaches)",
        roles=("gateway", "agent-runner"),
    ),
)


def _resolve_capability(cap: _Capability) -> bool:
    """The capability's settings bool; unset means the capability is off."""
    env_val: bool | None = get_field(cap.settings_attr)
    return bool(env_val)


def _resolve_setup_field(field: _SetupField) -> str | None:
    """The field's settings value, or None when it is empty.

    The field's validator gates the value; an invalid one raises immediately.
    """
    value = get_field(field.name).strip()
    if not value:
        return None
    if field.validator:
        field.validator(value)
    return value


def _collect_setup_values() -> tuple[SetupValues, list[_SetupField | _Capability]]:
    """Resolve all fields. Returns (resolved dict, list of missing required items).

    Phase 1: resolve the serve-capabilities first — they gate which other
    fields are required. If a host serves no capability, only the capability
    declarations are reported as missing (no point asking for fields that may
    or may not apply).

    Phase 2: filter the rest of `_SETUP_FIELDS` by `roles`, then resolve and
    collect missing. Missing optional fields do not enter the missing list —
    caller decides fallback (e.g. when memory_remote is missing, explicit
    `ava memory init` takes the local-init path).
    """
    caps = {cap.capability for cap in _CAPABILITIES if _resolve_capability(cap)}
    if not caps:
        # No capability resolved: the only missing items are the serve flags;
        # every value field is unresolved, so the caller's `missing` check returns
        # before reading any (the empty dict never surfaces a required key).
        return cast(SetupValues, {}), list(_CAPABILITIES)

    # The derived comma-string is what callers print / pass downstream as the
    # resolved `machine_role`. A field applies when any of its `roles` is a
    # capability this host carries.
    resolved: dict[str, str] = {"machine_role": ",".join(sorted(caps))}
    missing: list[_SetupField | _Capability] = []
    for field in _SETUP_FIELDS:
        if not (caps & set(field.roles)):
            continue
        value = _resolve_setup_field(field)
        if value is None:
            if not field.optional:
                missing.append(field)
        else:
            resolved[field.name] = value
    # Built with dynamic `field.name` keys (a TypedDict rejects a non-literal item
    # write); every key IS a SetupValues field by _SETUP_FIELDS construction.
    return cast(SetupValues, resolved), missing


def _missing_setup_message(missing: list[_SetupField | _Capability]) -> str:
    """Why a start cannot proceed: the identity this home recorded lacks required fields.

    `ava init` records them once and refuses an initialized home, so the repair is
    to set the named keys in the home's `.env` (or its environment), not to re-run
    init.
    """
    lines = ["\n✗ ava start: this home's recorded setup is incomplete:"]
    for f in missing:
        hint = "true / false" if isinstance(f, _Capability) else f.hint
        lines.append(f"  {f.env_var}  {hint}")
    lines.append(
        "\nA home initialized by `ava init` records these; set the keys above in "
        "$AVA_HOME/.env (`ava init` refuses an initialized home). A home that was never "
        "initialized needs `ava init` first (see `ava init --help`)."
    )
    return "\n".join(lines)
