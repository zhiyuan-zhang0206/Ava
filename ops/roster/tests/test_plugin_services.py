"""Plugin-registered ops services — the discovery + folding machinery in ops.spec.

A plugin ships a `services.py` exposing `services() -> tuple[ServiceSpec, ...]`;
`ops.spec.plugin_services()` discovers the INSTALLED plugins (by code presence,
via `base.packages.plugins.enable_config`) and folds their specs onto `build_services()` so the
roster stays single-source. These lock the load-bearing invariants:
- the real ava_fleet plugin registers task-maintenance (venv-direct cmd whose
  module lives in the plugin namespace, not core `services.*`, as a standard
  `/healthz` daemon);
- discovery keys on presence, NOT the agent-facing enable-state (a plugin
  disabled via plugins_config still contributes its service);
- no installed plugins -> nothing folded;
- a session-name collision fails fast (the roster is keyed on `session`);
- a present broken / declaration-less `services.py` reports and aborts the roster;
  an absent optional services face contributes nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import base.packages.plugins.enable_config as pc
from ops import roster, spec
from ops.roster import service_spec


def test_fleet_plugin_registers_task_maintenance() -> None:
    """task-maintenance is contributed by the ava_fleet plugin, not hardcoded in
    ops — its entry module lives under the plugin namespace."""
    by_session = {s.session: s for s in roster.build_services()}
    tm = by_session["task-maintenance"]
    # venv-direct launch (no `uv run` wrapper), relative to the source checkout.
    assert tm.cmd == ".venv/bin/python -m ava_builtins.plugins.ava_fleet.task_maintenance.daemon"
    assert tm.health_name == "task_maintenance"
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
    """When no plugin is present, `plugin_services()` contributes nothing and
    task-maintenance is absent from the roster."""
    monkeypatch.setattr(pc, "installed_plugin_dirs", dict)
    names = {s.session for s in roster.build_services()}
    assert "task-maintenance" not in names


def test_discovery_ignores_agent_enable_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Roster discovery keys on plugin PRESENCE, not the agent-facing enable-state:
    even with every plugin marked disabled in plugins_config, task-maintenance is
    still in the roster — the machine roster must not depend on the
    agent-plugin-registration plane. Its host-owned on/off is the explicit
    Fleet config image gate, exercised in
    `test_plugin_gate_flows_through_annotation`."""
    from base.packages.plugins.enable_config import PluginEntry, PluginsConfig

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
    from ava_builtins.plugins.ava_fleet.default_config import FleetConfig
    from base.packages.plugins.config_registration import disk_image_path

    image = disk_image_path("ava_fleet")
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_text(FleetConfig(task_maintenance_enabled=False).model_dump_json())
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
    monkeypatch.setattr(spec, "plugin_services", lambda: (collider,))
    with pytest.raises(spec.PluginServiceError, match="collides"):
        roster.build_services()


def test_services_py_without_declare_reports_and_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, loguru_records: list[dict]
) -> None:
    """A present malformed face is not optional absence."""
    plugin_dir = tmp_path / "brokenplugin"
    plugin_dir.mkdir()
    (plugin_dir / "services.py").write_text("X = 1  # no services() function\n")
    monkeypatch.setattr(pc, "installed_plugin_dirs", lambda: {"brokenplugin": plugin_dir})

    with pytest.raises(spec.PluginServiceError, match="callable services"):
        spec.plugin_services()

    assert any(
        "brokenplugin" in r["message"] and "failed to load" in r["message"] for r in loguru_records
    )


def test_broken_services_py_reports_and_aborts_roster(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, loguru_records: list[dict]
) -> None:
    """An import error cannot produce a successful partial roster."""
    good_dir = tmp_path / "goodplugin"
    good_dir.mkdir()
    (good_dir / "services.py").write_text(
        "from ops.roster.service_spec import ServiceSpec\n"
        "def services():\n"
        "    return (ServiceSpec(session='probe-good', cmd='noop',\n"
        "            capabilities=frozenset({'gateway'}), requires_db=False),)\n"
    )
    bad_dir = tmp_path / "brokenplugin"
    bad_dir.mkdir()
    (bad_dir / "services.py").write_text("raise RuntimeError('services boom')\n")
    monkeypatch.setattr(
        pc,
        "installed_plugin_dirs",
        lambda: {"brokenplugin": bad_dir, "goodplugin": good_dir},
    )

    with pytest.raises(RuntimeError, match="services boom"):
        spec.plugin_services()
    assert any(
        "brokenplugin" in r["message"] and "failed to load" in r["message"] for r in loguru_records
    )


@pytest.mark.parametrize("prior", [False, True])
def test_external_service_healthcheck_loads_without_agent_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prior: bool
) -> None:
    """Discovery supplies the exact module identity root health monitoring imports."""
    import importlib
    import sys
    from types import ModuleType

    name = "quota_service_probe"
    dotted = f"plugins.{name}.services"
    if prior:
        monkeypatch.setitem(sys.modules, dotted, ModuleType(dotted))
    else:
        monkeypatch.delitem(sys.modules, dotted, raising=False)
    plugin_dir = tmp_path / name
    plugin_dir.mkdir()
    (plugin_dir / "services.py").write_text(
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "from ops.roster.service_spec import ServiceSpec\n"
        "@dataclass\n"
        "class Sample:\n"
        "    value: int = 1\n"
        "def main():\n"
        "    return Sample().value\n"
        "def services():\n"
        "    return (ServiceSpec(session='quota-probe', cmd='noop',\n"
        "            capabilities=frozenset({'agent-runner'}), requires_db=True,\n"
        "            healthcheck_module=__name__),)\n"
    )
    monkeypatch.setattr(pc, "installed_plugin_dirs", lambda: {name: plugin_dir})
    declared = spec.plugin_services()
    assert len(declared) == 1
    assert declared[0].healthcheck_module == dotted
    assert importlib.import_module(dotted).main() == 1


@pytest.mark.parametrize("prior", [False, True])
def test_failed_service_import_does_not_publish_partial_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prior: bool
) -> None:
    import sys
    from types import ModuleType

    dotted = "plugins.quota_failed_probe.services"
    previous = ModuleType(dotted) if prior else None
    if previous is None:
        monkeypatch.delitem(sys.modules, dotted, raising=False)
    else:
        monkeypatch.setitem(sys.modules, dotted, previous)
    services_py = tmp_path / "services.py"
    services_py.write_text("PARTIAL = True\nraise RuntimeError('broken collector')\n")
    with pytest.raises(RuntimeError, match="broken collector"):
        spec._load_plugin_module("quota_failed_probe", services_py)
    assert sys.modules.get(dotted) is previous


@pytest.mark.parametrize(
    ("body", "error", "match"),
    [
        ("def services():\n    return undefined_roster\n", NameError, "undefined_roster"),
        ("def services():\n    return [1]\n", spec.PluginServiceError, "tuple\\[ServiceSpec"),
        ("services = 3\n", spec.PluginServiceError, "callable services"),
        (
            "from pydantic import BaseModel\n"
            "class Config(BaseModel):\n    enabled: bool\n"
            "def services():\n    Config(enabled='not-a-bool')\n    return ()\n",
            ValueError,
            "validation error",
        ),
    ],
)
def test_invalid_service_face_propagates_to_roster_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    loguru_records: list[dict],
    body: str,
    error: type[Exception],
    match: str,
) -> None:
    plugin_dir = tmp_path / "brokenplugin"
    plugin_dir.mkdir()
    (plugin_dir / "services.py").write_text(body)
    monkeypatch.setattr(pc, "installed_plugin_dirs", lambda: {"brokenplugin": plugin_dir})
    with pytest.raises(error, match=match):
        roster.build_services()
    assert any(
        "brokenplugin" in r["message"] and "failed to load" in r["message"] for r in loguru_records
    )


def test_absent_optional_service_face_does_not_fail_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plugin_dir = tmp_path / "no_service_face"
    plugin_dir.mkdir()
    monkeypatch.setattr(pc, "installed_plugin_dirs", lambda: {"no_service_face": plugin_dir})
    assert spec.plugin_services() == ()


@pytest.mark.parametrize("diagnostic", [False, True])
def test_failed_gate_never_produces_a_start_or_status_roster(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict], diagnostic: bool
) -> None:
    def broken_gate() -> str | None:
        raise RuntimeError("broken gate")

    service = service_spec.ServiceSpec(
        session="invalid-gate",
        cmd="noop",
        capabilities=frozenset({"gateway"}),
        requires_db=False,
        gate=broken_gate,
    )
    monkeypatch.setattr(spec, "build_services", lambda: (service,))
    read = (
        spec.services_for_capabilities_annotated if diagnostic else spec.services_for_capabilities
    )
    with pytest.raises(RuntimeError, match="broken gate"):
        read(frozenset({"gateway"}))
    assert any("roster evaluation aborted" in r["message"] for r in loguru_records)


def test_invalid_face_aborts_manifest_generation_before_any_unit_is_born(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.supervision.ava_root_glue.manifests import build_units

    plugin_dir = tmp_path / "invalid_config"
    plugin_dir.mkdir()
    (plugin_dir / "services.py").write_text(
        "def services():\n    raise ValueError('invalid service configuration')\n"
    )
    monkeypatch.setattr(pc, "installed_plugin_dirs", lambda: {"invalid_config": plugin_dir})
    with pytest.raises(ValueError, match="invalid service configuration"):
        build_units(None, capabilities={"gateway"}, repo_root=tmp_path)
