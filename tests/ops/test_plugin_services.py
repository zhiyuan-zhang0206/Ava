"""Plugin-registered ops services — the discovery + folding machinery in ops.spec.

A plugin ships a `services.py` exposing `services() -> tuple[ServiceSpec, ...]`;
`ops.spec._plugin_services()` discovers the INSTALLED plugins (by code presence,
via `shared.plugins_config`) and folds their specs onto `build_services()` so the
roster stays single-source. These lock the load-bearing invariants:
- the real ava_fleet plugin registers task-maintenance (venv-direct cmd +
  healthcheck_module point at the plugin namespace, not core `services.*`);
- discovery keys on presence, NOT the agent-facing enable-state (a plugin
  disabled via plugins_config still contributes its service);
- no installed plugins -> nothing folded;
- a session-name collision fails fast (the roster is keyed on `session`);
- a `services.py` missing `services()` fails fast.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import shared.plugins_config as pc
from ops import roster, service_spec, spec


def test_fleet_plugin_registers_task_maintenance() -> None:
    """task-maintenance is contributed by the ava_fleet plugin, not hardcoded in
    ops — its cmd + healthcheck_module live under the plugin namespace."""
    by_session = {s.session: s for s in roster.build_services()}
    tm = by_session["task-maintenance"]
    # venv-direct launch (no `uv run` wrapper), relative to the source checkout.
    assert tm.cmd == ".venv/bin/python -m ava_builtins.plugins.ava_fleet.task_maintenance.daemon"
    assert tm.healthcheck_module == "ava_builtins.plugins.ava_fleet.task_maintenance.healthcheck"
    assert tm.capabilities == frozenset({"gateway"})
    # It carries its own gate (the fleet toggle travels with the plugin).
    assert tm.gate is not None


def test_ops_spec_has_no_task_maintenance_hardcoded() -> None:
    """The core roster groups must not name task-maintenance — it only reaches the
    roster via plugin discovery."""
    source = Path(roster.__file__).read_text()
    # The only mentions allowed are the doc/comment references to the plugin; the
    # session string literal must not appear in a core ServiceSpec.
    assert 'session="task-maintenance"' not in source


def test_no_installed_plugins_contributes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no plugin is present, `_plugin_services()` contributes nothing and
    task-maintenance is absent from the roster."""
    monkeypatch.setattr(pc, "installed_plugin_dirs", dict)
    names = {s.session for s in roster.build_services()}
    assert "task-maintenance" not in names


def test_discovery_ignores_agent_enable_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Roster discovery keys on plugin PRESENCE, not the agent-facing enable-state:
    even with every plugin marked disabled in plugins_config, task-maintenance is
    still in the roster — the machine roster must not depend on the
    agent-plugin-registration plane. Its cluster-level on/off is the explicit
    `AVA_TASK_MAINTENANCE_ENABLED` gate, exercised in
    `test_plugin_gate_flows_through_annotation`."""
    from shared.plugins_config import PluginEntry, PluginsConfig

    monkeypatch.setattr(
        pc,
        "load",
        lambda known: PluginsConfig(plugins={n: PluginEntry(enabled=False) for n in known}),  # pyright: ignore[reportUnknownArgumentType]
    )
    names = {s.session for s in roster.build_services()}
    assert "task-maintenance" in names


def test_plugin_gate_flows_through_annotation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plugin service's own gate is honored by ops: disabling task-maintenance
    drops it from the start roster but keeps it (with a reason) in the annotated
    view — same contract as the core config-gated services."""
    monkeypatch.setattr(spec.settings.daemon, "task_maintenance_enabled", False)
    start = {s.session for s in spec.services_for_capabilities(frozenset({"gateway"}))}
    assert "task-maintenance" not in start
    annotated = {
        s.session: r for s, r in spec.services_for_capabilities_annotated(frozenset({"gateway"}))
    }
    assert annotated["task-maintenance"] and "disabled" in annotated["task-maintenance"]


def test_session_collision_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plugin service whose session collides with a core service is rejected —
    the roster is keyed on `session`, a duplicate would silently shadow one."""
    collider = service_spec.ServiceSpec(
        session="gateway",  # collides with the core gateway service
        cmd="noop",
        capabilities=frozenset({"gateway"}),
        requires_db=True,
    )
    monkeypatch.setattr(spec, "_plugin_services", lambda: (collider,))
    with pytest.raises(spec.PluginServiceError, match="collides"):
        roster.build_services()


def test_services_py_without_declare_fails_fast(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A plugin that ships a services.py with no `services()` function is a
    misconfiguration, caught at discovery time."""
    plugin_dir = tmp_path / "brokenplugin"
    plugin_dir.mkdir()
    (plugin_dir / "services.py").write_text("X = 1  # no services() function\n")
    monkeypatch.setattr(pc, "installed_plugin_dirs", lambda: {"brokenplugin": plugin_dir})
    with pytest.raises(spec.PluginServiceError, match="no `services\\(\\)`"):
        roster.build_services()
