"""Healthy probes stay silent; unhealthy probes signal without release mutations.
Grafana rules grade notifications from the recorded health_probe_failing event."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from base.agents.context.clients import DatabaseFactory
from base.db.tests.fakes import patch_database
from cli.commands.cluster import health as cluster_health
from cli.commands.cluster.tests.health_probe_inputs import (
    Probe,
    healthy,
    unhealthy,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    config_boot_environment as config_boot_environment,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    probe as probe,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    provider_guard_healthy as provider_guard_healthy,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    ran as ran,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    signals as signals,
)
from tests.path_scoped.cli_tests import operator_database as operator_database


def test_schema_health_db_flake_is_healthy(
    monkeypatch: pytest.MonkeyPatch, operator_database: Callable[[], Any]
) -> None:
    """A transient connection failure leaves liveness/population to grade the outage."""

    class _FlakyConnect:
        def __init__(self, *a: object, **kw: object) -> None:
            raise ConnectionError("pgbouncer blip")

    patch_database(monkeypatch, connect=_FlakyConnect)
    assert cluster_health._schema_health(database_factory=operator_database) is True


def test_schema_health_real_skew_is_unhealthy(
    monkeypatch: pytest.MonkeyPatch, operator_database: Callable[[], Any]
) -> None:
    """A genuine code/DB migration-set disagreement still fails the check."""
    from base.deploy.schema.migrations import CodeBehindSchema

    class _AheadConnect:
        def __init__(self, *a: object, **kw: object) -> None:
            pass

        def __enter__(self) -> _AheadConnect:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def cursor(self) -> _AheadCursor:
            return _AheadCursor()

    class _AheadCursor:
        def __enter__(self) -> _AheadCursor:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def execute(self, *a: object) -> None:
            raise CodeBehindSchema("DB has migrations this checkout lacks")

    patch_database(monkeypatch, connect=_AheadConnect)
    assert cluster_health._schema_health(database_factory=operator_database) is False


def test_agent_population_db_error_is_environment_class(
    monkeypatch: pytest.MonkeyPatch, operator_database: Callable[[], Any]
) -> None:
    """A failed population query is not evidence that the running code regressed."""

    def _down(**_kwargs: object) -> object:
        raise ConnectionError("pgbouncer unavailable")

    patch_database(monkeypatch, connect=_down)
    assert (
        cluster_health._agent_population_failure_class(1, database_factory=operator_database)
        == "environment"
    )


def test_liveness_retry_recovers_before_reporting_outage(
    probe: Probe, _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brief data-plane restart window is healthy once liveness recovers."""
    answers = iter([False, False, True])
    monkeypatch.setattr(cluster_health, "_gateway_liveness", lambda: next(answers))

    def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(cluster_health.time, "sleep", _no_sleep)
    monkeypatch.setattr(cluster_health, "_agent_population", healthy)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(cluster_health, "_crash_loop_detection", healthy)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(cluster_health, "_schema_health", healthy)
    monkeypatch.setattr(cluster_health, "_service_probes", list)
    monkeypatch.setattr(cluster_health, "_redis_bridge_probe", lambda: None)
    monkeypatch.setattr(cluster_health, "_disk_usage_failure", lambda: None)
    # Keep the editable-install check independent of the real prod venv.
    monkeypatch.setattr(cluster_health, "_editable_install_failure", lambda: None)

    assert probe() == 0


def test_environment_liveness_failure_signals_without_counting(
    probe: Probe, _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A data-plane failure signals but cannot launch an unrelated code rollback."""
    rollback_commands: list[list[str]] = []
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: False)
    monkeypatch.setattr(cluster_health, "_data_plane_abnormal", healthy)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kw: rollback_commands.append(command),  # type: ignore[arg-type]
    )

    assert probe() == 1
    assert rollback_commands == []


def test_code_liveness_failure_is_unhealthy(
    probe: Probe, _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway failure with a healthy data plane remains unhealthy."""
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: False)
    monkeypatch.setattr(cluster_health, "_data_plane_abnormal", unhealthy)

    assert probe() == 1


def test_agent_population_classifies_db_failure_as_environment(
    probe: Probe, _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Population-query connection failure is environmental; a low count is code-class."""
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: True)
    monkeypatch.setattr(cluster_health, "_agent_population", unhealthy)  # pyright: ignore[reportUnknownArgumentType]

    def _environment(_min_agents: int, *, database_factory: DatabaseFactory) -> str:
        assert callable(database_factory)
        return "environment"

    monkeypatch.setattr(cluster_health, "_agent_population_failure_class", _environment)

    assert probe() == 1

    def _code(_min_agents: int, *, database_factory: DatabaseFactory) -> str:
        assert callable(database_factory)
        return "code"

    monkeypatch.setattr(cluster_health, "_agent_population_failure_class", _code)
    assert probe() == 1


@pytest.fixture
def _all_checks_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: True)
    monkeypatch.setattr(cluster_health, "_agent_population", healthy)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(cluster_health, "_crash_loop_detection", healthy)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(cluster_health, "_schema_health", healthy)
    monkeypatch.setattr(cluster_health, "_service_probes", list)
    monkeypatch.setattr(cluster_health, "_redis_bridge_probe", lambda: None)
    monkeypatch.setattr(cluster_health, "_disk_usage_failure", lambda: None)
    monkeypatch.setattr(cluster_health, "_editable_install_failure", lambda: None)
    # Source-tree cases below supply their own checkout outcomes.
    monkeypatch.setattr(cluster_health, "_source_tree_failure", lambda: None)  # pyright: ignore[reportUnknownArgumentType]


@pytest.fixture
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Unit tests have no production checkout; dedicated cases pin this guard.
    from base.deploy.git import cluster_drift

    monkeypatch.setattr(cluster_drift, "prod_source_dir", lambda: None)
    return tmp_path


def test_every_run_emits_the_heartbeat_healthy_or_not(
    probe: Probe,
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    signals: list[dict[str, object]],
    ran: list[dict[str, object]],
) -> None:
    """`health_probe_ran` is the probe's dead-man heartbeat: one per run on every
    path, carrying the unhealthy-check count; the failing event is separate."""
    assert probe() == 0
    assert ran == [{"unhealthy_checks": 0}]
    assert signals == []

    monkeypatch.setattr(cluster_health, "_service_probes", lambda: ["ava-main-frontend"])
    assert probe() == 1  # the alert-only path
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: False)
    assert probe() == 1  # the primary-check path
    assert ran == [{"unhealthy_checks": 0}, {"unhealthy_checks": 1}, {"unhealthy_checks": 1}]
    assert len(signals) == 2


def test_a_refused_checkout_emits_no_heartbeat(
    probe: Probe,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    ran: list[dict[str, object]],
) -> None:
    """A wrong-checkout refusal (exit 2) never observed the cluster, so it is not a run."""
    import base.paths

    def _refuse(_root: Path) -> str:
        return "dev checkout"

    monkeypatch.setattr(base.paths, "prod_service_checkout_error", _refuse)

    assert probe() == 2
    assert ran == []


# ─── failing checks emit the signal ──────────────────────────────────────────


def test_service_probe_failure_emits_the_alert_only_signal(
    probe: Probe,
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    signals: list[dict[str, object]],
) -> None:
    """A dead selected service fails the probe and emits its failing check."""
    monkeypatch.setattr(cluster_health, "_service_probes", lambda: ["ava-main-frontend"])

    assert probe() == 1

    assert signals == [
        {
            "check": "service_probe",
            "failure_class": "alert-only",
            "message": "FAIL: service probe — not healthy: ava-main-frontend",
        }
    ]


def test_disk_over_watermark_fails_and_signals(
    probe: Probe,
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    signals: list[dict[str, object]],
) -> None:
    """A data volume over the 90% watermark fails the probe (exit 1) and emits
    the `disk_usage` signal; rolling back code frees no disk space (the
    2026-08-08 outage class: checkpoint growth filled the disk and the gateway
    could not start)."""
    monkeypatch.setattr(
        cluster_health, "_disk_usage_failure", lambda: "data volume 92.4% used (watermark 90%)"
    )

    assert probe() == 1

    assert len(signals) == 1
    assert signals[0]["check"] == "disk_usage"
    assert signals[0]["failure_class"] == "alert-only"
    assert "92.4%" in str(signals[0]["message"])


def test_wal_archiving_failure_signals_with_its_message(
    probe: Probe,
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    signals: list[dict[str, object]],
) -> None:
    """A broken archiver fails the probe like the disk check does."""
    failure = "WAL archiving: the archiver is failing"

    def failing_archiver(**_inputs: object) -> str:
        return failure

    monkeypatch.setattr(cluster_health, "_walg_archive_failure", failing_archiver)

    assert probe() == 1

    assert signals == [
        {"check": "walg_archive", "failure_class": "alert-only", "message": f"FAIL: {failure}"}
    ]


def test_wal_archiving_is_never_dialed_while_off(
    probe: Probe, _all_checks_pass: None, _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.backup.walg import probe as walg_probe

    def explode() -> object:
        raise AssertionError("the probe dialed Postgres while WAL-G is off")

    monkeypatch.setattr(walg_probe, "admin_connection", explode)

    assert probe() == 0


def test_disk_under_watermark_passes(probe: Probe, _all_checks_pass: None, _home: Path) -> None:
    """Healthy disk usage keeps the probe green (no alert, exit 0)."""
    rc = probe()
    assert rc == 0


def test_disk_usage_fraction_uses_statvfs_family(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fraction comes from shutil.disk_usage — the statvfs family shared
    with the trace disk-watermark guard and the 312 watcher — not df(1),
    whose offset to statvfs is unstable and fires late."""

    class _Usage:
        total = 200
        used = 174
        free = 26

    def _disk_usage(_path: object) -> _Usage:
        return _Usage()

    monkeypatch.setattr(cluster_health.shutil, "disk_usage", _disk_usage)
    frac = cluster_health._disk_usage_fraction()
    assert frac is not None
    assert abs(frac - 0.87) < 1e-9


def test_disk_usage_fraction_oserror_is_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken measurement must not synthesize a disk-full alarm."""

    def _raise_usage(*a: object, **kw: object) -> object:
        raise OSError("statvfs unavailable")

    monkeypatch.setattr(cluster_health.shutil, "disk_usage", _raise_usage)
    assert cluster_health._disk_usage_fraction() is None
    assert cluster_health._disk_usage_failure() is None


def test_disk_usage_failure_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exactly at the watermark is healthy; strictly over it alerts."""
    monkeypatch.setattr(cluster_health, "_disk_usage_fraction", lambda: 0.90)
    assert cluster_health._disk_usage_failure() is None
    monkeypatch.setattr(cluster_health, "_disk_usage_fraction", lambda: 0.9001)
    assert cluster_health._disk_usage_failure() is not None


def test_source_tree_failure_signals(
    probe: Probe,
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    signals: list[dict[str, object]],
) -> None:
    """A tampered prod source tree fails the probe (exit 1) and emits the
    `source_tree` signal; rolling back code does not undo an on-disk edit (the
    2026-08-28 outage class: edited source broke `import ava` for every agent on
    the box)."""
    message = "prod source tree tampered: untracked outside whitelist: junk.txt"
    monkeypatch.setattr(cluster_health, "_source_tree_failure", lambda: message)  # pyright: ignore[reportUnknownArgumentType]

    assert probe() == 1

    assert len(signals) == 1
    assert signals[0]["check"] == "source_tree"
    assert "junk.txt" in str(signals[0]["message"])


def test_source_tree_clean_passes(
    probe: Probe,
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean tree must not fail the probe — the whitelist exists so the
    routine frontend/ build output never fires a false alarm. The check is
    patched so the test does not depend on this host's prod tree state."""
    monkeypatch.setattr(cluster_health, "_source_tree_failure", lambda: None)  # pyright: ignore[reportUnknownArgumentType]

    rc = probe()

    assert rc == 0


def test_source_tree_guard_skipped_is_a_distinct_alert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A blind guard must not look like a clean tree: when
    ``source_tree_violations`` reports the guard as skipped, the probe names
    the failure 'guard skipped' (with the reason), never 'tampered'."""
    from base.deploy.git import cluster_drift, source_tree_guard

    def _violations_skipped(_repo: Path) -> tuple[str, ...]:
        return ("guard skipped: git unavailable",)

    monkeypatch.setattr(cluster_drift, "prod_source_dir", lambda: Path("/nonexistent"))
    monkeypatch.setattr(source_tree_guard, "source_tree_violations", _violations_skipped)

    failure = cluster_health._source_tree_failure()

    assert failure == "prod source tree guard skipped: git unavailable"


def test_signal_repeats_every_unhealthy_run_and_stops_when_healthy(
    probe: Probe,
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    signals: list[dict[str, object]],
) -> None:
    """The probe fires every few minutes; a persistent outage emits the signal on
    every run (a Grafana `for:` needs continuous samples), a healthy run emits
    nothing, and no state file is kept."""
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: False)
    assert probe() == 1
    assert probe() == 1
    assert [signal["check"] for signal in signals] == ["gateway_liveness"] * 2

    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: True)
    assert probe() == 0
    assert len(signals) == 2
    assert not list(_home.iterdir())


def test_signal_names_the_failed_check(
    probe: Probe,
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    signals: list[dict[str, object]],
) -> None:
    """A changed failure reason is a different check, grouped separately by Grafana."""
    monkeypatch.setattr(cluster_health, "_service_probes", lambda: ["ava-main-frontend"])
    assert probe() == 1
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: False)
    assert probe() == 1

    assert [signal["check"] for signal in signals] == ["service_probe", "gateway_liveness"]


def test_service_probes_skips_gated_but_rejects_unknown_specs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every intended service needs positive evidence; disabled services do not."""
    import cli.commands._repo as _repo_commands
    import cli.commands.probe as _probe_commands
    from ops.roster.service_spec import (
        _GATEWAY,  # typed frozenset[MachineRole]; value irrelevant (roster stubbed)
        ServiceSpec,
    )

    # requires_db is irrelevant here (no watchdog round involved), so it is uniform.
    def _spec(session: str) -> ServiceSpec:
        return ServiceSpec(session=session, cmd="x", capabilities=_GATEWAY, requires_db=True)

    dead = _spec("frontend")
    alive = _spec("gateway")
    gated = _spec("browser")
    probeless = _spec("browser-mcp")

    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(
        _repo_commands,
        "_services_for_roles_annotated",
        lambda _roles: (  # pyright: ignore[reportUnknownArgumentType]
            (dead, None),
            (alive, None),
            (gated, "disabled (AVA_BROWSER_ENABLED off)"),
            (probeless, None),
        ),
    )

    def _probe(spec: ServiceSpec) -> _probe_commands.ServiceProbe:
        alive = {"frontend": False, "gateway": True, "browser-mcp": None}[spec.session]
        return _probe_commands.ServiceProbe(alive, "probe", "")

    monkeypatch.setattr(_probe_commands, "probe_service", _probe)

    assert cluster_health._service_probes() == ["frontend", "browser-mcp"]


def test_service_probes_skips_gated_otel_collector_on_non_lgtm_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli.commands._repo as _repo_commands
    import cli.commands.probe as _probe_commands
    import ops.roster.service_spec as _service_spec

    tmp_home = tmp_path / "gateway"
    tmp_home.mkdir()
    recorded_sessions: list[str] = []

    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("ops.spec.gateway_observability_home", lambda: tmp_home)

    def _record_probe(spec: _service_spec.ServiceSpec) -> _probe_commands.ServiceProbe:
        recorded_sessions.append(spec.session)
        return _probe_commands.ServiceProbe(True, "probe", "")

    monkeypatch.setattr(_probe_commands, "probe_service", _record_probe)

    assert cluster_health._service_probes() == []
    assert "otel-collector" not in recorded_sessions


def test_service_probes_checks_otel_collector_on_non_lgtm_gateway_with_explicit_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli.commands._repo as _repo_commands
    import cli.commands.probe as _probe_commands
    import ops.roster.service_spec as _service_spec

    tmp_home = tmp_path / "gateway"
    tmp_home.mkdir()
    recorded_sessions: list[str] = []

    monkeypatch.setitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", "http://collector.invalid:4318")
    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("ops.spec.gateway_observability_home", lambda: tmp_home)

    def _record_probe(spec: _service_spec.ServiceSpec) -> _probe_commands.ServiceProbe:
        recorded_sessions.append(spec.session)
        return _probe_commands.ServiceProbe(True, "probe", "")

    monkeypatch.setattr(_probe_commands, "probe_service", _record_probe)

    assert cluster_health._service_probes() == []
    assert "otel-collector" in recorded_sessions


def test_service_probes_checks_otel_collector_on_lgtm_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli.commands._repo as _repo_commands
    import cli.commands.probe as _probe_commands
    import ops.roster.service_spec as _service_spec

    tmp_home = tmp_path / "gateway"
    tmp_home.mkdir()
    (tmp_home / "lgtm-host").touch()
    recorded_sessions: list[str] = []

    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("ops.spec.gateway_observability_home", lambda: tmp_home)

    def _record_probe(spec: _service_spec.ServiceSpec) -> _probe_commands.ServiceProbe:
        recorded_sessions.append(spec.session)
        return _probe_commands.ServiceProbe(True, "probe", "")

    monkeypatch.setattr(_probe_commands, "probe_service", _record_probe)

    assert cluster_health._service_probes() == []
    assert "otel-collector" in recorded_sessions


def test_service_probes_carry_the_failing_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong-home listener must be diagnosed distinctly from an absent listener."""
    import cli.commands._repo as _repo_commands
    import cli.commands.probe as _probe_commands
    from ops.roster.service_spec import _GATEWAY, ServiceSpec

    spec = ServiceSpec(session="ops", cmd="x", capabilities=_GATEWAY, requires_db=True)
    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(
        _repo_commands,
        "_services_for_roles_annotated",
        lambda _roles: ((spec, None),),  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _probe_commands,
        "probe_service",
        lambda _spec: _probe_commands.ServiceProbe(
            False, "identity", "home='/home/ava/.ava' != '/u/.ava'"
        ),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert cluster_health._service_probes() == ["ops (home='/home/ava/.ava' != '/u/.ava')"]


def test_service_probes_unknown_roster_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A responding gateway cannot replace the missing service inventory."""
    import cli.commands._repo as _repo_commands

    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: None)
    assert cluster_health._service_probes() == ["service roster unavailable"]


@pytest.mark.parametrize("mode,names", [("only", ["gateway"]), ("except", ["frontend"])])
def test_service_probes_respect_durable_service_intent(
    monkeypatch: pytest.MonkeyPatch, mode: str, names: list[str]
) -> None:
    import cli.commands._repo as _repo_commands
    import cli.commands.probe as _probe_commands
    import ops.roster.service_spec as _service_spec
    from base.deploy.lifecycle import service_selection

    wanted = _service_spec.ServiceSpec("gateway", "unused", frozenset({"gateway"}), True)
    excluded = _service_spec.ServiceSpec("frontend", "unused", frozenset({"gateway"}), False)

    def roster(_roles: object) -> tuple[tuple[_service_spec.ServiceSpec, None], ...]:
        return ((wanted, None), (excluded, None))

    def probe(spec: _service_spec.ServiceSpec) -> _probe_commands.ServiceProbe:
        assert spec is wanted
        return _probe_commands.ServiceProbe(True, "owned", "ready")

    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(_repo_commands, "_services_for_roles_annotated", roster)
    monkeypatch.setattr(_probe_commands, "probe_service", probe)
    monkeypatch.setattr(
        service_selection,
        "read_selection",
        lambda: service_selection.ServiceSelection(mode, frozenset(names)),
    )
    assert cluster_health._service_probes() == []


def test_service_probes_unreadable_selection_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    import cli.commands._repo as _repo_commands

    def unreadable() -> None:
        raise ValueError("corrupt selection")

    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("base.deploy.lifecycle.service_selection.read_selection", unreadable)
    assert cluster_health._service_probes() == ["service selection unavailable (corrupt selection)"]


# ── the gate's entry port rides in check 5 (alert-only) ───────────────────────


def test_dark_gate_fails_the_probe_without_arming_rollback(
    probe: Probe,
    monkeypatch: pytest.MonkeyPatch,
    _all_checks_pass: None,
    _home: Path,
    signals: list[dict[str, object]],
) -> None:
    """The boundary this check was placed for: a dark entry port signals and exits 1.
    The 2026-08-01 cause was a converge step that failed to reinstall the launchd
    job — rolling the cluster's code back would re-run that step identically, so
    rollback is not the remedy."""
    monkeypatch.setattr(
        cluster_health, "_service_probes", lambda: ["gate entry :3000 not answering (dark)"]
    )
    assert probe() == 1
    assert any("not answering" in str(signal["message"]) for signal in signals)


# ── the Redis bridge rides in check 5 (alert-only) ───────────────────────────


def _redis_bridge(**kw: object) -> object:
    import cli.commands.converge.redis_bridge as bridge

    fields: dict[str, object] = {
        "required": True,
        "endpoint": "10.64.0.7:6380",
        "serving": True,
        "supervised": True,
        "detail": "Redis PING succeeded",
    }
    fields.update(kw)
    return bridge.RedisBridgeStatus(**fields)  # type: ignore[arg-type]


def test_redis_bridge_probe_reports_running_but_dead_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loaded launchd job cannot hide a relay whose PING path is dead."""
    import cli.commands._repo as _repo_commands
    import cli.commands.converge.redis_bridge as bridge

    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(
        bridge,
        "probe_redis_bridge",
        lambda *_a: _redis_bridge(serving=False, detail="connection refused"),  # pyright: ignore[reportUnknownArgumentType]
    )

    failure = cluster_health._redis_bridge_probe()

    assert failure is not None
    assert "failed authenticated PING" in failure
    assert "connection refused" in failure


def test_redis_bridge_failure_signals_without_arming_rollback(
    probe: Probe,
    monkeypatch: pytest.MonkeyPatch,
    _all_checks_pass: None,
    _home: Path,
    signals: list[dict[str, object]],
) -> None:
    failure = "Redis bridge 10.64.0.7:6380 failed authenticated PING (connection refused)"
    monkeypatch.setattr(cluster_health, "_redis_bridge_probe", lambda: failure)

    assert probe() == 1
    assert any("Redis bridge" in str(signal["message"]) for signal in signals)


# ── crash-loop detection: category=audit only (W9 fix) ──────────────────────


def _record_resurrect(
    db: psycopg.Connection, agent_id: int, *, minutes_ago: float, table: str = "audit_events"
) -> None:
    if table == "audit_events":
        db.execute(
            "INSERT INTO audit_events (event_uid, ts, agent_id, machine, process, event_name, "
            "level, source) VALUES (%s, now() - (%s * interval '1 minute'), %s, 'm', 'p', "
            "'resurrect', 'info', 'test')",
            (uuid4().int % (1 << 62), minutes_ago, agent_id),
        )
    else:  # the telemetry mirror of the same fact must not count
        db.execute(
            "INSERT INTO telemetry_events (event_uid, ts, agent_id, machine, cluster, process, "
            "category, event_name, level, source) VALUES (%s, now() - (%s * interval '1 minute'), "
            "%s, 'm', 'c', 'p', 'telemetry', 'resurrect', 'info', 'test')",
            (uuid4().int % (1 << 62), minutes_ago, agent_id),
        )
    db.commit()


def test_crash_loop_counts_the_audit_resurrect_rows_of_the_window(
    db_conn: psycopg.Connection, operator_database: Callable[[], Any]
) -> None:
    """Only audit resurrects in the window count; telemetry mirrors and old rows do not."""
    for _ in range(2):
        _record_resurrect(db_conn, 1, minutes_ago=10)
    _record_resurrect(db_conn, 1, minutes_ago=300)  # outside the window
    _record_resurrect(db_conn, 1, minutes_ago=5, table="telemetry_events")

    assert (
        cluster_health._crash_loop_detection(
            max_restarts=2, window_minutes=60, database_factory=operator_database
        )
        is True
    )

    _record_resurrect(db_conn, 1, minutes_ago=3)
    assert (
        cluster_health._crash_loop_detection(
            max_restarts=2, window_minutes=60, database_factory=operator_database
        )
        is False
    )


def test_crash_loop_is_judged_per_agent(
    db_conn: psycopg.Connection, operator_database: Callable[[], Any]
) -> None:
    for agent_id in (1, 2, 3):
        for _ in range(2):
            _record_resurrect(db_conn, agent_id, minutes_ago=10)

    assert (
        cluster_health._crash_loop_detection(
            max_restarts=2, window_minutes=60, database_factory=operator_database
        )
        is True
    )


def test_crash_loop_is_healthy_when_the_database_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, operator_database: Callable[[], Any]
) -> None:

    def unreachable(**_kwargs: object) -> object:
        raise OSError("database unreachable")

    patch_database(monkeypatch, connect=unreachable)

    assert (
        cluster_health._crash_loop_detection(
            max_restarts=0, window_minutes=60, database_factory=operator_database
        )
        is True
    )
