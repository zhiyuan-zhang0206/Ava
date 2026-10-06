"""The sole service owner, exact launch inputs, and fresh readiness."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from base.daemon.health import DaemonProbe
from cli.commands._repo import ServiceSpec
from cli.commands.lifecycle import root_driver as driver

# The repo-wide readiness guard replaces `_wait_for_root_services_ready` itself;
# without this opt-out every readiness test here would assert on that stub.
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
    assert not driver._unit_ready({"state": "running"}, verdict)


def test_readiness_probes_now_instead_of_believing_cached_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = status()
    snapshot["health"] = {"gateway": {"last_verdict": "alive"}}
    monkeypatch.setattr(driver, "root_client", object)

    def read_status(_client: object) -> dict[str, Any]:
        return snapshot

    monkeypatch.setattr(driver, "_root_status", read_status)
    result = driver._wait_for_root_services_ready(
        (spec(probe=lambda: DaemonProbe.down("unready")),), 0
    )
    assert result.unready


def test_raising_identity_probe_reads_unavailable_and_warns() -> None:
    from base.log import logger

    def broken() -> DaemonProbe:
        raise ValueError("probe bug")

    records: list[str] = []
    sink = logger.add(lambda message: records.append(str(message)), level="WARNING")
    try:
        assert driver._health_verdicts((spec(probe=broken),), status()) == {
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

    monkeypatch.setattr(driver, "_root_status", read_status)
    result = driver._wait_for_root_services_ready((spec(probe=lambda: DaemonProbe.up("ready")),), 0)
    assert not result.unready
    assert next(snapshots)["units"][0]["pid"] == 101


def test_fresh_ready_generation_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver, "root_client", object)

    def read_status(_client: object) -> dict[str, Any]:
        return status()

    monkeypatch.setattr(driver, "_root_status", read_status)
    result = driver._wait_for_root_services_ready((spec(probe=lambda: DaemonProbe.up("ready")),), 0)
    assert not result.unready


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

    def no_optional_wait(_seconds: float) -> None:
        raise AssertionError("optional services must not delay a ready core")

    monkeypatch.setattr(driver, "root_client", object)
    monkeypatch.setattr(driver, "_root_status", read_status)
    monkeypatch.setattr(driver, "_poll_sleep", no_optional_wait)
    result = driver._wait_for_root_services_ready(
        (spec(probe=lambda: DaemonProbe.up("ready")), background),
        100,
    )
    assert not result.unready
    assert result.non_critical_unready == (background,)


def test_mac_helper_protocol_refuses_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(driver, "_helper_spawn_committed", lambda: True)
    monkeypatch.setattr(driver, "_helper_wire_ok", lambda: False)
    with pytest.raises(RuntimeError, match="root_stop_intent_v1"):
        driver._bring_up_root(tmp_path, tmp_path, tmp_path / "manifest", object(), {})


@pytest.mark.parametrize("missing", ["root_stop_intent_v1", "helper_shutdown_v1", None])
def test_helper_admission_requires_normal_retirement_protocol(
    monkeypatch: pytest.MonkeyPatch, missing: str | None
) -> None:
    capabilities = {"root_stop_intent_v1": True, "helper_shutdown_v1": True}
    if missing is not None:
        del capabilities[missing]
    monkeypatch.setattr("services.desktop.permissions_helper.client.ping", lambda: capabilities)
    assert driver._helper_wire_ok() is (missing is None)


def test_collector_config_bytes_change_the_live_unit_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ops import roster
    from services.supervision.ava_root.manifest import UnitManifest

    config = tmp_path / "collector.yaml"
    config.write_text("receivers: {otlp: {}}\n")
    monkeypatch.setattr(roster, "otel_collector_config", lambda: config)
    monkeypatch.setattr(roster, "_plugin_services", tuple)
    collector = next(s for s in roster.build_services() if s.session == "otel-collector")
    before = driver.tree_manifest((collector,), tmp_path, roles=frozenset({"gateway"}))
    rows = cast("list[dict[str, object]]", before["units"])
    unit = UnitManifest.from_mapping(rows[0], origin="test")
    live = {"units": [{"id": unit.id, "manifest_digest": unit.digest()}]}
    assert not driver._changed_units(before, live)
    config.write_text("receivers: {otlp: {protocols: {http: {}}}}\n")
    after = driver.tree_manifest((collector,), tmp_path, roles=frozenset({"gateway"}))
    assert driver._changed_units(after, live) == {"otel-collector"}


def test_windows_adapter_gap_is_explicit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(driver.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="Windows root supervision requires"):
        driver._bring_up_root(tmp_path, tmp_path, tmp_path / "manifest", object(), {})


def test_obsolete_custody_records_do_not_block_an_absent_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = tmp_path / "custody"
    directory.mkdir()
    (directory / "gateway.json").write_text("unknown")
    monkeypatch.setattr("base.paths.root_run_dir", lambda: tmp_path)
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
    assert not driver._changed_units(json.loads(path.read_text()), snapshot)
    generate(
        path,
        capabilities={"gateway"},
        repo_root=tmp_path,
        specs=[spec()],
        environments={"gateway": {"AVA_PROCESS_PROFILE": "agent"}},
    )
    assert driver._changed_units(json.loads(path.read_text()), snapshot) == {"gateway"}
    assert path.stat().st_mode & 0o777 == 0o600


def test_missing_root_ipc_is_unknown_not_positive_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver, "root_client", object)

    def unavailable(_client: object) -> None:
        return None

    monkeypatch.setattr(driver, "_root_status", unavailable)
    outcome = driver._wait_for_root_services_ready((spec(),), 0)
    assert outcome.unready and not outcome.sessions_gone


def test_a_stopped_core_unit_is_not_ready_even_if_its_endpoint_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(driver, "root_client", object)

    def read_status(_client: object) -> dict[str, Any]:
        return status(state="stopped")

    monkeypatch.setattr(driver, "_root_status", read_status)
    result = driver._wait_for_root_services_ready((spec(probe=lambda: DaemonProbe.up("ready")),), 0)
    assert result.unready and result.sessions_gone


def test_helper_seed_is_durable_before_wire_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from services.desktop.permissions_helper import client

    monkeypatch.setattr(driver, "root_child_env", lambda: {"AVA_HOME": str(tmp_path)})

    def seed(config: client.RootSeedConfig) -> client.RootStatus:
        path = tmp_path / "seed.json"
        assert json.loads(path.read_text()) == config
        assert path.stat().st_mode & 0o777 == 0o600
        return {"state": "running", "seeded": True, "restarts": 0, "stop_requested": False}

    monkeypatch.setattr(client, "seed_root", seed)
    driver._seed_via_helper(
        tmp_path, tmp_path, tmp_path / "manifests.json", {"AVA_HOME": str(tmp_path)}
    )


def test_stop_without_root_ipc_cancels_pending_helper_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.desktop.permissions_helper import client

    monkeypatch.setattr(driver, "_helper_spawn_committed", lambda: True)
    monkeypatch.setattr(driver, "_helper_wire_ok", lambda: True)
    monkeypatch.setattr(driver, "require_root_absent", lambda: None)
    calls: list[str] = []

    def keeper() -> client.RootStatus:
        return {"state": "backoff", "seeded": True, "restarts": 1, "stop_requested": False}

    def stop() -> client.RootStatus:
        calls.append("durable stop")
        return {"state": "stopped", "seeded": True, "restarts": 1, "stop_requested": True}

    monkeypatch.setattr(client, "root_status", keeper)
    monkeypatch.setattr(client, "stop_root", stop)
    driver._stop_dormant_helper_root(float("inf"))
    assert calls == ["durable stop"]


def test_linux_root_launch_never_consults_a_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(driver.sys, "platform", "linux")
    spawned: list[Path] = []

    def spawn(
        run_dir: Path,
        _repo: Path,
        _manifests: Path,
        _env: dict[str, str],
        _runtime: object = None,
    ) -> SimpleNamespace:
        spawned.append(run_dir)
        return SimpleNamespace(pid=123)

    def helper() -> bool:
        raise AssertionError("Linux root must not consult a helper")

    def await_status(_client: object, _run_dir: Path, **_kwargs: object) -> dict[str, Any]:
        return status()

    monkeypatch.setattr(driver, "_spawn_direct", spawn)
    monkeypatch.setattr(driver, "_helper_wire_ok", helper)
    monkeypatch.setattr(driver, "_await_root_status", await_status)
    assert (
        driver._bring_up_root(tmp_path, tmp_path, tmp_path / "manifest", object(), {}) == status()
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
    monkeypatch.setattr("base.paths.root_run_dir", lambda: tmp_path)
    monkeypatch.setattr("base.paths.root_manifests_path", lambda: published)

    def tree(*_args: object, **_kwargs: object) -> dict[str, object]:
        return manifest

    def snapshot_now(_client: object) -> dict[str, Any]:
        return snapshot

    def owned(_status: object) -> None:
        pass

    def no_stop(*_args: object, **_kwargs: object) -> None:
        pytest.fail("no stop")

    def source_identity(_repo: Path) -> str:
        return "a" * 64

    monkeypatch.setattr("cli.commands.lifecycle.start_generation.source_digest", source_identity)
    monkeypatch.setattr(driver, "root_child_env", dict)
    monkeypatch.setattr(driver, "tree_manifest", tree)
    monkeypatch.setattr(driver, "root_client", object)
    monkeypatch.setattr(driver, "_root_status", snapshot_now)
    monkeypatch.setattr(driver, "_require_root_owner", owned)
    monkeypatch.setattr(driver, "_stop_root_process", no_stop)
    result = driver._ensure_root_service_tree(
        requested, tmp_path, roles=frozenset({"gateway"}), reconcile=True
    )
    assert result.failed
    assert published.read_text() == "old generation"


def test_mac_boot_tail_does_not_read_or_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver.sys, "platform", "darwin")
    monkeypatch.setattr(driver, "root_client", lambda: pytest.fail("no root read"))
    driver.complete_boot_start()


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
    driver._require_same_generation(manifest, snapshot, requested, reconcile=True)
    manifest["launch_digest"] = "0" * 64
    with pytest.raises(RuntimeError, match="root launch inputs"):
        driver._require_same_generation(manifest, snapshot, requested, reconcile=True)
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
    from services.desktop.permissions_helper import client, launchd_job

    monkeypatch.setattr(driver, "_helper_spawn_committed", lambda: True)

    def unavailable() -> client.RootStatus:
        raise client.PermissionsHelperError("AF_UNIX path too long")

    def query(_target: str, _deadline: float) -> str | None:
        return "state = spawn scheduled" if loaded else None

    monkeypatch.setattr(client, "root_status", unavailable)
    monkeypatch.setattr(launchd_job, "retirement_query", query)
    if loaded:
        with pytest.raises(RuntimeError, match="custody is unavailable"):
            driver._stop_dormant_helper_root(float("inf"))
    else:
        driver._stop_dormant_helper_root(float("inf"))


def _mock_pidfd_delivery(monkeypatch: pytest.MonkeyPatch, signals: list[str]) -> None:
    from base.native_process import ownership as proc_tree

    def open_pidfd(_pid: int) -> int:
        return os.open(os.devnull, os.O_RDONLY)

    def deliver(descriptor: int, signum: int) -> None:
        os.fstat(descriptor)  # The native handle must remain open during delivery.
        signals.append("term" if signum == signal.SIGTERM else "kill")

    monkeypatch.setattr(proc_tree, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(proc_tree.pidfd, "open_process", open_pidfd)
    monkeypatch.setattr(proc_tree.pidfd, "send_signal", deliver)


def test_signals_reject_reuse_inside_legacy_birth_tolerance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import psutil

    from base.native_process.ownership import OwnedProcess

    old = OwnedProcess(12345, 10.0, 100)
    replacement = OwnedProcess(12345, 10.01, 101)
    signals: list[str] = []
    _mock_pidfd_delivery(monkeypatch, signals)

    class NativeProcess:
        def terminate(self) -> None:
            signals.append("term")

        def kill(self) -> None:
            signals.append("kill")

        def send_signal(self, signum: int) -> None:
            signals.append("term" if signum == signal.SIGTERM else "kill")

    def process(_pid: int) -> NativeProcess:
        return NativeProcess()

    def live(_self: OwnedProcess) -> bool:
        # The old generation was alive at the pre-observation. PID reuse happens
        # before the signal's psutil handle is captured.
        return True

    def capture(_process: object) -> OwnedProcess:
        return replacement

    def legacy_match(_self: OwnedProcess, _process: object) -> bool:
        return True  # Both births fit the retired +/- 2 second compatibility window.

    def owned(_status: object) -> None:
        pass

    monkeypatch.setattr(psutil, "Process", process)
    monkeypatch.setattr(OwnedProcess, "live", live)
    monkeypatch.setattr(OwnedProcess, "capture", staticmethod(capture))
    monkeypatch.setattr(OwnedProcess, "birth_matches", legacy_match)
    monkeypatch.setattr(driver, "_require_root_owner", owned)
    monkeypatch.setattr(driver, "_helper_spawn_committed", lambda: False)
    with pytest.raises(RuntimeError, match="identity changed"):
        snapshot = {"root": {"pid": old.pid, "create_time": old.birth, "starttime": old.starttime}}
        driver._stop_root_process(tmp_path, object(), snapshot, timeout_s=0)
    assert signals == []


def test_root_signals_use_start_ticks_when_wall_birth_moves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import psutil

    from base.native_process.ownership import OwnedProcess

    captured = OwnedProcess(12345, 10.0, 100)
    observed = OwnedProcess(12345, 3610.0, 100)
    signals: list[str] = []
    _mock_pidfd_delivery(monkeypatch, signals)

    class NativeProcess:
        def terminate(self) -> None:
            signals.append("term")

        def kill(self) -> None:
            signals.append("kill")

        def send_signal(self, signum: int) -> None:
            signals.append("term" if signum == signal.SIGTERM else "kill")

    def process(_pid: int) -> NativeProcess:
        return NativeProcess()

    def capture(_process: object) -> OwnedProcess:
        return observed

    def live(_self: OwnedProcess) -> bool:
        return not signals

    def ignore(_value: object) -> None:
        return None

    monkeypatch.setattr(psutil, "Process", process)
    monkeypatch.setattr(OwnedProcess, "capture", staticmethod(capture))
    monkeypatch.setattr(OwnedProcess, "live", live)
    monkeypatch.setattr(driver, "_require_root_owner", ignore)
    monkeypatch.setattr(driver, "_helper_spawn_committed", lambda: False)
    monkeypatch.setattr(driver, "_root_status", ignore)
    snapshot = {"root": {"pid": captured.pid, "create_time": captured.birth, "starttime": 100}}
    driver._stop_root_process(tmp_path, object(), snapshot, timeout_s=1)
    assert signals == ["term"]


def test_direct_child_cannot_be_reaped_by_an_unrelated_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gc
    import subprocess
    import sys
    import time

    import psutil

    if sys.platform == "win32":
        pytest.skip("POSIX child reaping contract")

    def command(_run: Path, _manifest: Path, _runtime: object = None) -> list[str]:
        return [sys.executable, "-c", "pass"]

    monkeypatch.setattr(driver, "_root_argv", command)
    monkeypatch.setattr(driver, "_direct_root_child", None)
    process = driver._spawn_direct(tmp_path, tmp_path, tmp_path / "manifest", {})
    pid = process.pid
    del process
    gc.collect()
    deadline = time.monotonic() + 5
    try:
        while psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
            assert time.monotonic() < deadline, "child did not exit"
            time.sleep(0.01)
        # Popen cleans its abandoned-child table here. Our retained child must
        # stay unreaped, keeping this PID unavailable until the CLI exits.
        subprocess.run([sys.executable, "-c", "pass"], check=True, timeout=5)
        assert psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    finally:
        retained = driver._direct_root_child
        if retained is not None:
            retained.wait(timeout=5)
