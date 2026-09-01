"""Setup-field resolution (env > $AVA_HOME/<name> file > CLI arg).

Called by `cmd_start`, which writes the resolved value to file when the arg is
given so subsequent starts need no flags.

Capabilities (serve_gateway / serve_agent_runner) are two independent booleans,
each resolved env (settings bool) > `$AVA_HOME/machine_serve_<cap>` file > CLI
arg; the string setup fields (machine_name / description / memory_remote /
gateway_url) are resolved by `_SetupField` below, gated by which capabilities
this host carries.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import NotRequired, TypedDict, cast

from shared.config import get_field


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
        name: `$AVA_HOME/<name>` filename + `settings.<name>` Python attribute
        cli_flag: argparse arg name (e.g. `--machine-name`)
        env_var: equivalent env var (e.g. `AVA_MACHINE_NAME`)
        hint: value hint shown in error messages (e.g. "<name>, e.g. host-a")
        roles: tuple of capability values this field is required by (("gateway",), ("agent-runner",), or both)
        optional: missing field does not enter the missing-error list; caller decides fallback
        validator: optional, value validity check; raises a ValueError subclass
    """

    name: str
    cli_flag: str
    env_var: str
    hint: str
    roles: tuple[str, ...] = ("gateway", "agent-runner")
    optional: bool = False
    validator: Callable[[str], None] | None = None


@dataclass(frozen=True)
class _Capability:
    """Metadata for one serve-capability flag — a boolean (serve or not), not a
    string field. Resolved env (settings bool) > `$AVA_HOME/<file>` > CLI arg.

    Attributes:
        capability: the capability token this flag declares ("gateway" / "agent-runner")
        file: `$AVA_HOME/<file>` filename + the basename for the env var below
        cli_flag: argparse arg name (e.g. `--serve-gateway`)
        env_var: equivalent env var (e.g. `AVA_MACHINE_SERVE_GATEWAY`)
        settings_attr: `settings.<attr>` (bool | None — None = env unset)
    """

    capability: str
    file: str
    cli_flag: str
    env_var: str
    settings_attr: str


_CAPABILITIES: tuple[_Capability, ...] = (
    _Capability(
        capability="gateway",
        file="machine_serve_gateway",
        cli_flag="--serve-gateway",
        env_var="AVA_MACHINE_SERVE_GATEWAY",
        settings_attr="machine_serve_gateway",
    ),
    _Capability(
        capability="agent-runner",
        file="machine_serve_agent_runner",
        cli_flag="--serve-agent-runner",
        env_var="AVA_MACHINE_SERVE_AGENT_RUNNER",
        settings_attr="machine_serve_agent_runner",
    ),
    _Capability(
        capability="observability-station",
        file="machine_serve_observability_station",
        cli_flag="--serve-observability-station",
        env_var="AVA_MACHINE_SERVE_OBSERVABILITY_STATION",
        settings_attr="machine_serve_observability_station",
    ),
)


_SETUP_FIELDS: tuple[_SetupField, ...] = (
    _SetupField(
        name="machine_name",
        cli_flag="--machine-name",
        env_var="AVA_MACHINE_NAME",
        hint="<name>, e.g. host-a / host-b",
    ),
    _SetupField(
        name="machine_description",
        cli_flag="--machine-description",
        env_var="AVA_MACHINE_DESCRIPTION",
        hint='<free text>, e.g. "voice IO + browser + always-on"',
        optional=True,
    ),
    _SetupField(
        name="memory_remote",
        cli_flag="--memory-remote",
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
        cli_flag="--gateway-url",
        env_var="AVA_GATEWAY_URL",
        hint="<url>, e.g. http://<gateway-host>:8800 (gateway: this host's own URL; agent-runner: the gateway it reaches)",
        roles=("gateway", "agent-runner"),
    ),
)


def _resolve_capability(cap: _Capability, arg_value: bool | None) -> bool:  # noqa: FBT001 — tri-state capability flag, passed by name
    """env (settings bool) > `$AVA_HOME/<file>` > arg (write file + return) > False.

    A non-None env / file / arg is honored as-is; only when all three are unset
    does the capability default to off. When the arg is given, write the file so
    a subsequent `ava start` resolves the same value without the flag (mirroring
    `_resolve_setup_field`'s persistence behavior).
    """
    from shared.machine import parse_serve_value
    from shared.paths import ava_home

    env_val: bool | None = get_field(cap.settings_attr)
    if env_val is not None:
        return env_val
    p = ava_home() / cap.file
    if p.exists():
        text = p.read_text()
        if text.strip():
            return parse_serve_value(text, str(p))
    if arg_value is not None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("true" if arg_value else "false")
        print(f"  · wrote {p} ({cap.file}={'true' if arg_value else 'false'})")
        return arg_value
    return False


def _resolve_setup_field(field: _SetupField, arg_value: str | None) -> str | None:
    """env > file > arg (write file + return) > None.

    If arg is given, validate before writing — invalid values raise immediately
    (do not persist the bad value to file).
    """
    from shared.paths import ava_home

    env_val = get_field(field.name).strip()
    if env_val:
        if field.validator:
            field.validator(env_val)
        return env_val
    p = ava_home() / field.name
    if p.exists():
        file_val = p.read_text().strip()
        if file_val:
            if field.validator:
                field.validator(file_val)
            return file_val
    if arg_value:
        if field.validator:
            field.validator(arg_value)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(arg_value)
        print(f"  · wrote {p} ({field.name}={arg_value})")
        return arg_value
    return None


def _collect_setup_values(
    args: dict[str, str | bool | None],
) -> tuple[SetupValues, list[_SetupField | _Capability]]:
    """Resolve all fields. Returns (resolved dict, list of missing required items).

    `args` must carry every capability + setup-field key (value None = arg not
    given) — the caller builds the full dict, so a missing key is a programming
    error and is indexed with `[]` to fail loud rather than silently read None.

    Phase 1: resolve the serve-capabilities first — they gate which other
    fields are required. If a host serves no capability, only the `--serve-*`
    flags are reported as missing (no point asking for fields that may or may
    not apply).

    Phase 2: filter the rest of `_SETUP_FIELDS` by `roles`, then resolve and
    collect missing. Missing optional fields do not enter the missing list —
    caller decides fallback (e.g. when memory_remote is missing, explicit
    `ava memory init` takes the local-init path).
    """
    caps = {
        cap.capability
        for cap in _CAPABILITIES
        if _resolve_capability(cap, _as_bool(args[cap.settings_attr]))
    }
    if not caps:
        # No capability resolved: the only missing items are the two serve flags;
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
        value = _resolve_setup_field(field, _as_str(args[field.name]))
        if value is None:
            if not field.optional:
                missing.append(field)
        else:
            resolved[field.name] = value
    # Built with dynamic `field.name` keys (a TypedDict rejects a non-literal item
    # write); every key IS a SetupValues field by _SETUP_FIELDS construction.
    return cast(SetupValues, resolved), missing


def _as_bool(value: str | bool | None) -> bool | None:  # noqa: FBT001 — value-narrowing helper for the args dict
    """Narrow an args-dict value to bool|None (capability args are bool|None)."""
    if value is None or isinstance(value, bool):
        return value
    raise TypeError(f"expected bool|None for a serve-capability arg, got {value!r}")


def _as_str(value: str | bool | None) -> str | None:  # noqa: FBT001 — value-narrowing helper for the args dict
    """Narrow an args-dict value to str|None (string setup-field args are str|None)."""
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"expected str|None for a string setup field, got {value!r}")


def _print_missing_setup_error(missing: list[_SetupField | _Capability], role: str | None) -> None:
    """Print actionable error + role-aware first-time setup example.

    `role` is the already-resolved capability set as a comma-string (None when
    the host serves no capability — that case prints both gateway and
    agent-runner example commands). A non-TTY agent can copy the example verbatim
    and rerun.
    """
    print(
        "\n✗ ava start: missing required multi-machine setup (pass --flag or set env):",
        file=sys.stderr,
    )
    flag_w = max(len(f.cli_flag) for f in missing)
    for f in missing:
        hint = "true / false" if isinstance(f, _Capability) else f.hint
        print(
            f"  {f.cli_flag.ljust(flag_w)} {hint}      (env: {f.env_var})",
            file=sys.stderr,
        )
    caps = {c.strip() for c in role.split(",")} if role else set()
    print("\nfirst-time setup example:", file=sys.stderr)
    if role is None:
        print(
            "  # single box (owns data plane + runs agents):\n"
            "  ava start --machine-name <name> --serve-gateway --serve-agent-runner \\\n"
            "            --gateway-url http://localhost:8000",
            file=sys.stderr,
        )
    if role is None or "gateway" in caps:
        print(
            "  # gateway (data plane + HTTP gateway):\n"
            "  ava start --machine-name <name> --serve-gateway \\\n"
            "            --memory-remote <git-url> --gateway-url <https-url>",
            file=sys.stderr,
        )
    if role is None or "agent-runner" in caps:
        print(
            "  # agent-runner (machine key set via `ava enroll`):\n"
            "  ava start --machine-name <name> --serve-agent-runner \\\n"
            "            --memory-remote <git-url> --gateway-url <https-url>",
            file=sys.stderr,
        )
    print(
        "\nAfter the first successful run, the CLI writes values to $AVA_HOME/<field> "
        "files; subsequent `ava start` calls do not need the args.",
        file=sys.stderr,
    )
