"""The sole service owner, exact launch inputs, and fresh readiness."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import psutil
import pytest

import base.host.permissions_helper.client as permissions_helper_client
import base.paths as base_paths
import cli.commands.lifecycle.start_generation as lifecycle_start_generation
from base.clock.tests.manual_timers import ManualTimers
from base.daemon.health import DaemonProbe
from base.host.system.tests.fake_commands import DARWIN_ARM64, fake_host
from base.native_process.ownership import OwnedProcess
from base.sessions.tests.refuse_helper import refuse_helper
from base.sessions.tests.scripted_helper import ScriptedHelper, serve
from cli.commands.data_plane import bringup
from cli.commands.lifecycle import root_driver as driver
from cli.commands.lifecycle import root_readiness as readiness
from services.supervision.roster import plugin_services
from services.supervision.roster.service_spec import ServiceSpec

# Keep the public readiness entry point real when this owner suite reaches it.
pytestmark = pytest.mark.real_service_readiness_gate


def spec(*, probe: Any = None) -> ServiceSpec:
    return ServiceSpec(
        session="gateway",
        cmd="python -m gateway",
        capabilities=frozenset({"gateway"}),
        requires_db=False,
        identity_probe=probe,
    )


def status(*, pid: int = 100, state: str = "running") -> dict[str, Any]:
    return {
        "root": {"pid": 90},
        "units": [
            {"id": "gateway", "state": state, "pid": pid, "create_time": 12.0, "starttime": None}
        ],
    }


@pytest.mark.parametrize("verdict", [None, "unavailable", "down", "port-taken"])
def test_no_missing_or_failed_health_is_ready(verdict: str | None) -> None:
    assert not readiness.unit_ready({"state": "running"}, verdict)


def test_readiness_probes_now_instead_of_believing_cached_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = status()
    snapshot["health"] = {"gateway": {"last_verdict": "alive"}}
    monkeypatch.setattr(driver, "root_client", object)

    def read_status(_client: object) -> dict[str, Any]:
        return snapshot

    monkeypatch.setattr(driver, "root_status", read_status)
    result = readiness.wait_for_root_services_ready(
        (spec(probe=lambda: DaemonProbe.down("unready")),), 0, timers=ManualTimers()
    )
    assert result.unready


def test_raising_identity_probe_reads_unavailable_and_warns() -> None:
    from base.log import logger

    def broken() -> DaemonProbe:
        raise ValueError("probe bug")

    records: list[str] = []
    sink = logger.add(lambda message: records.append(str(message)), level="WARNING")
    try:
        assert readiness.health_verdicts((spec(probe=broken),), status()) == {
            "gateway": "unavailable"
        }
    finally:
        logger.remove(sink)
    assert len(records) == 1
    assert "ValueError: probe bug" in records[0]


def test_readiness_uses_one_status_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshots = iter([status(pid=100), status(pid=101)])
    monkeypatch.setattr(driver, "root_client", object)

    def read_status(_client: object) -> dict[str, Any]:
        return next(snapshots)

    monkeypatch.setattr(driver, "root_status", read_status)
    result = readiness.wait_for_root_services_ready(
        (spec(probe=lambda: DaemonProbe.up("ready")),), 0, timers=ManualTimers()
    )
    assert not result.unready
    assert next(snapshots)["units"][0]["pid"] == 101


def test_fresh_ready_generation_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver, "root_client", object)

    def read_status(_client: object) -> dict[str, Any]:
        return status()

    monkeypatch.setattr(driver, "root_status", read_status)
    verdicts = iter([DaemonProbe.down("starting"), DaemonProbe.up("ready")])
    timers = ManualTimers(start=14.0)
    result = readiness.wait_for_root_services_ready(
        (spec(probe=lambda: next(verdicts)),), 10.0, timers=timers
    )
    assert not result.unready
    assert result.elapsed_s == 0.5
    assert timers.sleeps == [0.5]


def test_optional_failure_does_not_delay_a_ready_core(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = status()
    snapshot["units"].append({"id": "browser-mcp", "state": "stopped"})
    background = ServiceSpec(
        session="browser-mcp",
        cmd="unused",
        capabilities=frozenset({"gateway"}),
        requires_db=False,
        identity_probe=lambda: DaemonProbe.down("unavailable"),
    )

    def read_status(_client: object) -> dict[str, Any]:
        return snapshot

    monkeypatch.setattr(driver, "root_client", object)
    monkeypatch.setattr(driver, "root_status", read_status)
    timers = ManualTimers()
    result = readiness.wait_for_root_services_ready(
        (spec(probe=lambda: DaemonProbe.up("ready")), background),
        100,
        timers=timers,
    )
    assert not result.unready
    assert result.non_critical_unready == (background,)
    assert timers.sleeps == []


def test_mac_helper_protocol_refuses_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    silent = ScriptedHelper(ping=permissions_helper_client.PermissionsHelperError("no socket"))
    with pytest.raises(RuntimeError, match="root_stop_intent_v1"):
        driver.bring_up_root(
            tmp_path,
            tmp_path,
            tmp_path / "manifest",
            object(),
            {},
            retained_children=[],
            helper=serve(silent),
        )


@pytest.mark.parametrize("missing", ["root_stop_intent_v1", "helper_shutdown_v1", None])
def test_helper_admission_requires_normal_retirement_protocol(missing: str | None) -> None:
    capabilities = {"root_stop_intent_v1": True, "helper_shutdown_v1": True}
    if missing is not None:
        del capabilities[missing]
    assert driver.helper_wire_ok(serve(ScriptedHelper(ping=capabilities))) is (missing is None)


def test_collector_config_bytes_change_the_live_unit_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from services.supervision import roster
    from services.supervision.ava_root.manifest import UnitManifest

    config = tmp_path / "collector.yaml"
    config.write_text("receivers: {otlp: {}}\n")
    monkeypatch.setattr(roster, "otel_collector_config", lambda: config)
    monkeypatch.setattr(plugin_services, "plugin_services", tuple)
    collector = next(s for s in roster.build_services() if s.session == "otel-collector")
    before = driver.tree_manifest((collector,), tmp_path, roles=frozenset({"gateway"}))
    rows = cast("list[dict[str, object]]", before["units"])
    unit = UnitManifest.from_mapping(rows[0], origin="test")
    live = {"units": [{"id": unit.id, "manifest_digest": unit.digest()}]}
    assert not driver.changed_units(before, live)
    config.write_text("receivers: {otlp: {protocols: {http: {}}}}\n")
    after = driver.tree_manifest((collector,), tmp_path, roles=frozenset({"gateway"}))
    assert driver.changed_units(after, live) == {"otel-collector"}


def test_obsolete_custody_records_do_not_block_an_absent_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = tmp_path / "custody"
    directory.mkdir()
    (directory / "gateway.json").write_text("unknown")
    monkeypatch.setattr(base_paths, "root_run_dir", lambda: tmp_path)
    driver.require_root_absent()


def test_manifest_change_requires_generation_replacement(tmp_path: Path) -> None:
    from services.supervision.ava_root.manifest import load_manifests
    from services.supervision.ava_root_glue.manifests import generate

    path = tmp_path / "manifest.json"
    generate(
        path,
        capabilities={"gateway"},
        repo_root=tmp_path,
        specs=[spec()],
        environments={"gateway": {"AVA_PROCESS_PROFILE": "gateway"}},
    )
    manifest = load_manifests(path).units[0]
    snapshot = status()
    snapshot["units"][0]["manifest_digest"] = manifest.digest()
    assert not driver.changed_units(json.loads(path.read_text()), snapshot)
    generate(
        path,
        capabilities={"gateway"},
        repo_root=tmp_path,
        specs=[spec()],
        environments={"gateway": {"AVA_PROCESS_PROFILE": "agent"}},
    )
    assert driver.changed_units(json.loads(path.read_text()), snapshot) == {"gateway"}
    assert path.stat().st_mode & 0o777 == 0o600


def test_missing_root_ipc_is_unknown_not_positive_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver, "root_client", object)

    def unavailable(_client: object) -> None:
        return None

    monkeypatch.setattr(driver, "root_status", unavailable)
    timers = ManualTimers(start=14.0)
    outcome = readiness.wait_for_root_services_ready((spec(),), 1.0, timers=timers)
    assert outcome.unready and not outcome.sessions_gone
    assert outcome.elapsed_s == 1.0
    assert timers.sleeps == [0.5, 0.5]


def test_a_stopped_core_unit_is_not_ready_even_if_its_endpoint_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(driver, "root_client", object)

    def read_status(_client: object) -> dict[str, Any]:
        return status(state="stopped")

    monkeypatch.setattr(driver, "root_status", read_status)
    result = readiness.wait_for_root_services_ready(
        (spec(probe=lambda: DaemonProbe.up("ready")),), 0, timers=ManualTimers()
    )
    assert result.unready and result.sessions_gone


def test_helper_seed_is_durable_before_wire_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from base.host.permissions_helper import client

    monkeypatch.setattr(driver, "root_child_env", lambda: {"AVA_HOME": str(tmp_path)})

    def seed(config: client.RootSeedConfig) -> client.RootStatus:
        path = tmp_path / "seed.json"
        assert json.loads(path.read_text()) == config
        assert path.stat().st_mode & 0o777 == 0o600
        return {"state": "running", "seeded": True, "restarts": 0, "stop_requested": False}

    driver.seed_via_helper(
        tmp_path,
        tmp_path,
        tmp_path / "manifests.json",
        {"AVA_HOME": str(tmp_path)},
        helper=serve(ScriptedHelper(seed_root=seed)),
    )


def test_stop_without_root_ipc_cancels_pending_helper_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.host.permissions_helper import client

    monkeypatch.setattr(driver, "require_root_absent", lambda: None)
    calls: list[str] = []

    def stop() -> client.RootStatus:
        calls.append("durable stop")
        return {"state": "stopped", "seeded": True, "restarts": 1, "stop_requested": True}

    helper = ScriptedHelper(
        ping={"pong": True, "root_stop_intent_v1": True, "helper_shutdown_v1": True},
        root_status={"state": "backoff", "seeded": True, "restarts": 1, "stop_requested": False},
        stop_root=stop,
    )
    driver.stop_dormant_helper_root(float("inf"), helper=serve(helper))
    assert calls == ["durable stop"]


def test_linux_root_launch_never_consults_a_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    spawned: list[Path] = []
    children: list[subprocess.Popen[bytes]] = []

    def spawn(
        run_dir: Path,
        _repo: Path,
        _manifests: Path,
        _env: dict[str, str],
        _runtime: object = None,
        *,
        retained_children: list[subprocess.Popen[bytes]],
    ) -> SimpleNamespace:
        assert retained_children is children
        spawned.append(run_dir)
        return SimpleNamespace(pid=123)

    def await_status(_client: object, _run_dir: Path, **_kwargs: object) -> dict[str, Any]:
        return status()

    monkeypatch.setattr(driver, "spawn_direct", spawn)
    monkeypatch.setattr(driver, "await_root_status", await_status)
    assert (
        driver.bring_up_root(
            tmp_path,
            tmp_path,
            tmp_path / "manifest",
            object(),
            {},
            retained_children=children,
            helper=None,
        )
        == status()
    )
    assert spawned == [tmp_path]


@pytest.mark.parametrize("removed", [False, True])
def test_generation_change_refuses_without_signal_or_seed_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, removed: bool
) -> None:
    from services.supervision.ava_root.manifest import UnitManifest
    from services.supervision.ava_root_glue.manifests import build_manifest

    requested = (spec(),)
    manifest = build_manifest(capabilities={"gateway"}, repo_root=tmp_path, specs=requested)
    snapshot = status()
    row = cast("list[dict[str, object]]", manifest["units"])[0]
    snapshot["units"][0]["manifest_digest"] = (
        UnitManifest.from_mapping(row, origin="test").digest() if removed else "old-digest"
    )
    if removed:
        snapshot["units"].append({"id": "agent-host", "state": "running"})
    published = tmp_path / "manifests.json"
    published.write_text("old generation")
    monkeypatch.setattr(base_paths, "root_run_dir", lambda: tmp_path)
    monkeypatch.setattr(base_paths, "root_manifests_path", lambda: published)

    def tree(*_args: object, **_kwargs: object) -> dict[str, object]:
        return manifest

    def snapshot_now(_client: object) -> dict[str, Any]:
        return snapshot

    def source_identity(_repo: Path) -> str:
        return "a" * 64

    monkeypatch.setattr(lifecycle_start_generation, "source_digest", source_identity)
    monkeypatch.setattr(driver, "root_child_env", dict)
    monkeypatch.setattr(driver, "tree_manifest", tree)
    monkeypatch.setattr(driver, "root_client", object)
    monkeypatch.setattr(driver, "root_status", snapshot_now)
    # The recorded root is this process and its helper is the parent, so the custody check
    # passes and only the generation comparison can refuse.
    me = OwnedProcess.capture(psutil.Process())
    snapshot["root"] = {"pid": me.pid, "create_time": me.birth, "starttime": me.starttime}
    helper = ScriptedHelper(
        ping={
            "pong": True,
            "root_stop_intent_v1": True,
            "helper_shutdown_v1": True,
            "pid": os.getppid(),
        },
        root_status={"pid": me.pid, "run_dir": str(tmp_path)},
    )
    result = driver.ensure_root_service_tree(
        requested,
        tmp_path,
        roles=frozenset({"gateway"}),
        reconcile=True,
        retained_children=[],
        helper=serve(helper),
    )
    assert result.failed
    assert published.read_text() == "old generation"
    assert not {"stop_root", "seed_root"} & set(helper.calls)  # a refused generation never signals


def test_a_platform_without_a_helper_asks_only_that_the_root_is_live() -> None:
    me = OwnedProcess.capture(psutil.Process())
    live_root = {"root": {"pid": me.pid, "create_time": me.birth, "starttime": me.starttime}}
    driver.require_root_owner(live_root, helper=None)
    child = subprocess.Popen(["sleep", "60"])
    try:
        born = OwnedProcess.capture(psutil.Process(child.pid))
    finally:
        child.kill()
        child.wait()
    gone = {"root": {"pid": born.pid, "create_time": born.birth, "starttime": born.starttime}}
    with pytest.raises(driver.RootDriverError, match="no longer live"):
        driver.require_root_owner(gone, helper=None)


def test_a_platform_without_a_helper_has_no_dormant_helper_root_to_stop() -> None:
    driver.stop_dormant_helper_root(float("inf"), helper=None)


def test_mac_boot_tail_does_not_read_or_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver, "root_client", lambda: pytest.fail("no root read"))
    driver.complete_boot_start(helper=refuse_helper, host=fake_host(platform=DARWIN_ARM64))


def test_root_launch_digest_controls_reuse(tmp_path: Path) -> None:
    import hashlib

    from services.supervision.ava_root.manifest import UnitManifest
    from services.supervision.ava_root_glue.manifests import build_manifest, write_manifest

    requested = (spec(),)
    manifest = build_manifest(capabilities={"gateway"}, repo_root=tmp_path, specs=requested)
    launch_env = {"AVA_HOME": str(tmp_path), "AVA_TOKEN": "test-private-value"}
    digest = hashlib.sha256(
        json.dumps(launch_env, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest["launch_digest"] = digest
    snapshot = status()
    row = cast("list[dict[str, object]]", manifest["units"])[0]
    snapshot["units"][0]["manifest_digest"] = UnitManifest.from_mapping(row, origin="test").digest()
    snapshot["root"]["launch_digest"] = digest
    path = write_manifest(tmp_path / "manifest.json", manifest)
    before = path.read_bytes()
    driver.require_same_generation(manifest, snapshot, requested, reconcile=True)
    manifest["launch_digest"] = "0" * 64
    with pytest.raises(RuntimeError, match="root launch inputs"):
        driver.require_same_generation(manifest, snapshot, requested, reconcile=True)
    assert path.read_bytes() == before
    assert b"test-private-value" not in before


def test_root_launch_path_uses_home_declaration_across_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(driver.settings.general, "service_path", str(tmp_path / "tools"))
    monkeypatch.setenv("PATH", str(tmp_path / "interactive"))
    interactive = driver.root_child_env()
    monkeypatch.setenv("PATH", str(tmp_path / "systemd"))
    assert driver.root_child_env() == interactive
    monkeypatch.setattr(driver.settings.general, "service_path", str(tmp_path / "changed"))
    assert driver.root_child_env() != interactive


# What each service manager adds to its job's environment on its own. systemd
# with `User=` sets USER/LOGNAME/SHELL, plus its invocation bookkeeping; a
# launchd job also gets its per-user TMPDIR, the agent socket and XPC names.
_SERVICE_MANAGER_INJECTIONS = {
    "systemd User=": {
        "USER": "ava-owner",
        "LOGNAME": "ava-owner",
        "SHELL": "/bin/sh",
        "INVOCATION_ID": "0" * 32,
        "JOURNAL_STREAM": "8:12345",
        "SYSTEMD_EXEC_PID": "4242",
    },
    "launchd": {
        "USER": "ava-owner",
        "LOGNAME": "ava-owner",
        "SHELL": "/bin/zsh",
        "TMPDIR": "/private/var/folders/xy/T/",
        "SSH_AUTH_SOCK": "/private/tmp/com.apple.launchd.x/Listeners",
        "XPC_SERVICE_NAME": "0",
        "XPC_FLAGS": "0x0",
        "OSLogRateLimit": "64",
    },
}


@pytest.mark.parametrize("manager", sorted(_SERVICE_MANAGER_INJECTIONS))
def test_root_launch_digest_ignores_service_manager_injections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manager: str
) -> None:
    """The started root and the stage observer derive one launch digest.

    The start action runs inside the boot unit, whose manager injects variables
    of its own; the observer runs with the fixed stage environment only. Both
    sides must still name the same immutable generation.
    """
    from cli.commands.lifecycle import start_generation

    monkeypatch.setattr(driver.settings.general, "service_path", str(tmp_path / "tools"))

    def source(_repo: Path) -> str:
        return "source"

    monkeypatch.setattr(start_generation, "source_digest", source)
    injected = _SERVICE_MANAGER_INJECTIONS[manager]
    for key in injected:
        monkeypatch.delenv(key, raising=False)
    observed = start_generation.launch_digest(tmp_path, driver.root_child_env(), home=tmp_path)
    for key, value in injected.items():
        monkeypatch.setenv(key, value)
    started = start_generation.launch_digest(tmp_path, driver.root_child_env(), home=tmp_path)
    assert started == observed
    monkeypatch.setattr(driver.settings.general, "service_path", str(tmp_path / "changed"))
    changed = start_generation.launch_digest(tmp_path, driver.root_child_env(), home=tmp_path)
    assert changed != observed


def test_root_child_env_beyond_the_launch_inputs_is_only_ambient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every key root hands its units is a declared launch input or an ambient host fact.

    `launch_input_keys` is a positive list, so a non-ambient key a delivery
    adds would silently stay out of the launch digest; this names it instead.
    Every key the registry declares (each passthrough row, every settings
    alias) and the finalizer projection are set, including keys forwarded only
    when present, so what this catches does not depend on this host's environment.
    """
    from base.host.env import registry

    ambient = (
        registry.HOST_PASSTHROUGH_KEYS
        | registry._TEMP_DIR_KEYS
        | registry.NETWORK_PROXY_KEYS
        | {"PYTHONUTF8"}
    )
    declared = (
        {row.key for row in registry._PASSTHROUGH_ROWS}
        | set(registry.FIELD_ALIASES.values())
        | {"PYTHONUTF8"}
    )
    paths = {"HOME", "PATH", *registry._TEMP_DIR_KEYS}
    for key in declared:
        monkeypatch.setenv(key, str(tmp_path) if key in paths else "ambient")
    monkeypatch.setattr(driver.settings.general, "service_path", str(tmp_path / "tools"))

    undeclared = set(driver.root_child_env()) - registry.launch_input_keys() - ambient
    assert not undeclared, f"declare these in launch_input_keys or as ambient: {sorted(undeclared)}"


@pytest.mark.parametrize("loaded", [False, True])
def test_unusable_helper_socket_requires_positive_native_absence(
    monkeypatch: pytest.MonkeyPatch, loaded: bool
) -> None:
    from base.host.permissions_helper import client
    from services.desktop.permissions_helper import launchd_job

    def query(_target: str, _deadline: float) -> str | None:
        return "state = spawn scheduled" if loaded else None

    monkeypatch.setattr(launchd_job, "retirement_query", query)
    unavailable = serve(
        ScriptedHelper(root_status=client.PermissionsHelperError("AF_UNIX path too long"))
    )
    if loaded:
        with pytest.raises(RuntimeError, match="custody is unavailable"):
            driver.stop_dormant_helper_root(float("inf"), helper=unavailable)
    else:
        driver.stop_dormant_helper_root(float("inf"), helper=unavailable)


def test_plugin_birth_config_is_part_of_the_existing_unit_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from pydantic import BaseModel, ConfigDict

    from base.host.env.registry import SERVICE_PLUGIN_CONFIG_ENV
    from services.supervision.ava_root.manifest import UnitManifest

    class Config(BaseModel):
        model_config = ConfigDict(frozen=True)
        interval: float = 300

    def no_api_delivery(_cls: str, *, remote_data_plane: bool) -> dict[str, str]:
        return {}

    monkeypatch.setattr(bringup, "api_delivery", no_api_delivery)
    base = replace(spec(), plugin_config=("probe", Config(interval=40)))
    same = replace(base, plugin_config=("probe", Config(interval=40)))
    changed = replace(base, plugin_config=("probe", Config(interval=41)))
    manifests = [
        driver.tree_manifest((row,), tmp_path, roles=frozenset({"gateway"}))
        for row in (base, same, changed)
    ]
    units = [
        UnitManifest.from_mapping(
            cast("list[dict[str, object]]", manifest["units"])[0], origin="test"
        )
        for manifest in manifests
    ]
    assert SERVICE_PLUGIN_CONFIG_ENV in dict(units[0].env)
    assert units[0].digest() == units[1].digest()
    assert units[0].digest() != units[2].digest()


class _UnitClient:
    """Records which units an operator-style reconcile asked root to bring up."""

    def __init__(self) -> None:
        self.up_calls: list[str] = []

    def up(self, unit_id: str) -> dict[str, Any]:
        self.up_calls.append(unit_id)
        return {"ok": True}


def _no_refresh(_client: object) -> None:
    return None


def _spent_budget_status() -> dict[str, Any]:
    exhausted = status(state="exited")
    exhausted["health"] = {"gateway": {"restart_limit_reached": True}}
    return exhausted


def test_an_operator_start_brings_up_a_unit_whose_restart_budget_is_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit `ava start` is the operator's way to revive an exhausted unit."""
    monkeypatch.setattr(driver, "root_status", _no_refresh)
    client = _UnitClient()
    driver.reconcile_units((spec(),), client, _spent_budget_status())
    assert client.up_calls == ["gateway"]


def test_a_boot_attempt_leaves_a_unit_down_whose_restart_budget_is_spent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The boot job's retried start must not spend root's budget again."""
    monkeypatch.setattr(driver, "root_status", _no_refresh)
    client = _UnitClient()
    driver.reconcile_units((spec(),), client, _spent_budget_status(), honor_restart_budget=True)
    assert client.up_calls == []
    assert "spent its restart budget" in capsys.readouterr().out


def test_a_boot_attempt_still_brings_up_a_unit_with_budget_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(driver, "root_status", _no_refresh)
    client = _UnitClient()
    down = status(state="exited")
    down["health"] = {"gateway": {"restart_limit_reached": False}}
    driver.reconcile_units((spec(),), client, down, honor_restart_budget=True)
    assert client.up_calls == ["gateway"]


def test_seed_wait_reports_a_keeper_that_stopped_restarting_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(driver, "root_child_env", lambda: {"AVA_HOME": str(tmp_path)})
    exited = {
        "state": "exited",
        "seeded": True,
        "restarts": 2,
        "stop_requested": False,
        "last_exit": {"kind": "crash", "code": 1, "at": 0.0},
    }

    with pytest.raises(driver.RootDriverError, match="stopped restarting"):
        driver.seed_via_helper(
            tmp_path,
            tmp_path,
            tmp_path / "manifests.json",
            {"AVA_HOME": str(tmp_path)},
            helper=serve(ScriptedHelper(seed_root=exited)),
        )
