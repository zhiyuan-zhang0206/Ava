"""Plugin + MCP-enable inventory RPC ops (agent-runner only).

Read this host's plugin / MCP enable inventory (each with metadata + a host
capability verdict) and apply a validated all-or-nothing toggle write. One of
the op clusters beside `ops.lifecycle`, `ops.cluster.operations` and `ops.host_config`;
each cluster is self-contained.

Plugins + MCP servers are an agent-runner-only concern — a gateway runs no
agent, so every op here asserts the agent-runner capability and fails loud on a
gateway. Dispatched by the agent-runner ops server (`services/agent_runner/agent_ops/daemon.py`).
"""

from __future__ import annotations

from typing import Any

from base.cluster.machine import MachineIdentity
from base.host.system.hostport import Host
from base.packages.plugins import enable_config, mcp_enabled
from ops.rpc_schemas import (
    FieldWriteResult,
    InventoryReadItem,
    InventoryReadResult,
    InventoryWriteOpResult,
)


def _mcp_summary(spec: dict[str, Any]) -> str:
    """One-line human summary of an MCP server entry (url, or command + args).

    Local copy of the cli list-view helper — `gateway` must not import `cli`
    (layering), so the small formatting logic is duplicated here.
    """
    if not isinstance(spec, dict):
        return "(malformed entry)"
    if "url" in spec:
        return str(spec["url"])
    command = spec.get("command", "")
    args: list[Any] = spec.get("args", [])
    parts = [str(command), *(str(a) for a in args)] if isinstance(args, list) else [str(command)]
    return " ".join(p for p in parts if p)


def _assert_agent_runner(*, local_machine: MachineIdentity) -> None:
    """Declare the invariant that inventory ops run only on an agent-runner.

    Plugins + MCP servers are an agent-runner-only concern: a gateway runs no
    agent, so its inventory is whatever built-in `<repo>/meiva_builtins/plugins/` + `<repo>/meiva_builtins/mcps/`
    its checkout happens to carry — never actually loaded. The gateway enforces
    this at the boundary (it never targets a gateway), and the ops server
    that invokes these ops is itself agent-runner-only, so reaching here on a
    gateway is a broken call path, not a degraded mode: fail loud rather
    than resurface the gateway's built-in items as if installed.
    """
    if "agent-runner" not in local_machine.role():
        raise RuntimeError(
            f"inventory ops run only on an agent-runner, not {sorted(local_machine.role())!r}"
        )


def inventory_read_op(*, local_machine: MachineIdentity, host: Host) -> InventoryReadResult:
    """Read this host's plugin + MCP enable inventory, each with metadata.

    Returns an InventoryReadResult with:
      - machine: this machine's name
      - plugins: one entry per discovered plugin with effective enabled flag,
        a description, and no static host gate (can_enable/reason are None —
        an installed plugin is always enable-able here)
      - mcp_servers: one entry per server in the full merged map with effective
        enabled flag (the per-host overlay default-on), a one-line description,
        and a read-time capability verdict (can_enable/reason) for the `requires`
        precondition
    """
    from meiva.mcps.config import load_mcp_config, server_capability

    _assert_agent_runner(local_machine=local_machine)
    discovered = enable_config.discover_plugins()
    cfg = enable_config.load_for_runtime(set(discovered))
    plugins: dict[str, InventoryReadItem] = {}
    for name, plugin_dir in discovered.items():
        try:
            description = enable_config.parse_description(plugin_dir / "plugin.py")
        except (OSError, SyntaxError):
            description = ""
        plugins[name] = InventoryReadItem(
            enabled=cfg.plugins[name].enabled,
            can_enable=None,
            reason=None,
            description=description,
        )

    merged = load_mcp_config(include_disabled=True)
    overlay = mcp_enabled.read_enabled()
    mcp_servers: dict[str, InventoryReadItem] = {}
    for name, spec in merged.items():
        ok, reason = server_capability(spec, host=host)
        mcp_servers[name] = InventoryReadItem(
            enabled=overlay.get(name, True),
            can_enable=ok,
            reason=reason,
            description=_mcp_summary(spec),
        )

    return InventoryReadResult(
        machine=local_machine.name(),
        plugins=plugins,
        mcp_servers=mcp_servers,
    )


def inventory_write_op(
    plugins: dict[str, bool],
    mcp_servers: dict[str, bool],
    *,
    local_machine: MachineIdentity,
    host: Host,
) -> InventoryWriteOpResult:
    """Validate and atomically apply a set of plugin + MCP enable toggles.

    Validates every requested toggle first (no writes), mirroring
    `config_write_op`'s all-or-nothing atomicity:
      - a plugin not installed on this host is rejected; enabling and disabling
        an installed plugin are both always ok
      - an MCP name not in the merged map is rejected; enabling one runs the
        host capability check (`server_capability`); disabling is always ok

    Only writes if every requested toggle passes. Returns per-item results and
    an `applied` flag. No restart_required field — plugin/MCP toggles take
    effect on the next agent step / next connect, no restart.
    """
    from meiva.mcps.config import load_mcp_config, server_capability

    _assert_agent_runner(local_machine=local_machine)
    discovered = set(enable_config.discover_plugins())
    plugin_results: dict[str, FieldWriteResult] = {}
    for name in plugins:
        if name not in discovered:
            plugin_results[name] = FieldWriteResult(ok=False, reason="not installed on this host")
        else:
            plugin_results[name] = FieldWriteResult(ok=True, reason=None)

    merged = load_mcp_config(include_disabled=True)
    mcp_results: dict[str, FieldWriteResult] = {}
    for name, want_enabled in mcp_servers.items():
        if name not in merged:
            mcp_results[name] = FieldWriteResult(
                ok=False, reason="not a known MCP server on this host"
            )
        elif want_enabled:
            ok, reason = server_capability(merged[name], host=host)
            mcp_results[name] = FieldWriteResult(ok=ok, reason=reason)
        else:
            mcp_results[name] = FieldWriteResult(ok=True, reason=None)

    # all() over an empty dict is True, so an empty payload is trivially applied.
    applied = all(r.ok for r in plugin_results.values()) and all(r.ok for r in mcp_results.values())

    if applied:
        for name, want in plugins.items():
            enable_config.set_local_enabled(name, enabled=want)
        for name, want in mcp_servers.items():
            mcp_enabled.set_mcp_enabled(name, enabled=want)

    return InventoryWriteOpResult(
        machine=local_machine.name(),
        plugin_results=plugin_results,
        mcp_results=mcp_results,
        applied=applied,
    )
