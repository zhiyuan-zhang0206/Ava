"""Healthy probes stay silent; unhealthy probes signal without release mutations.
Grafana rules grade notifications from the recorded health_probe_failing event."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from base.agents.context.clients import DatabaseFactory
from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.db.tests.fakes import patch_database
from cli.commands.cluster import health as cluster_health
from cli.commands.cluster.tests.health_probe_inputs import (
    Probe,
    bridge_status,
    healthy,
    redis_bridge_verdict,
    service_probe_verdict,
    unhealthy,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    all_checks_pass as all_checks_pass,
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
from cli.database.runtime import CommandRuntime
from cli.tests.bootstrap.command_inputs import command_runtime as command_runtime
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
    monkeypatch.setattr(cluster_health, "_schema_health", healthy)
    monkeypatch.setattr(
        cluster_health, "_service_probes", partial(service_probe_verdict, failures=[])
    )
    monkeypatch.setattr(
        cluster_health, "_redis_bridge_probe", partial(redis_bridge_verdict, result=None)
    )
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
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Unit tests have no production checkout; dedicated cases pin this guard.
    from base.deploy.git import cluster_drift

    monkeypatch.setattr(cluster_drift, "prod_source_dir", lambda: None)
    return tmp_path


def test_every_run_emits_the_heartbeat_healthy_or_not(
    db_url: str,
    redis_url: str,
    probe: Probe,
    all_checks_pass: None,
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

    monkeypatch.setattr(
        cluster_health,
        "_service_probes",
        partial(service_probe_verdict, failures=["ava-main-frontend"]),
    )
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
    all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    signals: list[dict[str, object]],
) -> None:
    """A dead selected service fails the probe and emits its failing check."""
    monkeypatch.setattr(
        cluster_health,
        "_service_probes",
        partial(service_probe_verdict, failures=["ava-main-frontend"]),
    )

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
    all_checks_pass: None,
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
    all_checks_pass: None,
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
    probe: Probe, all_checks_pass: None, _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.backup.walg import probe as walg_probe

    def explode() -> object:
        raise AssertionError("the probe dialed Postgres while WAL-G is off")

    monkeypatch.setattr(walg_probe, "admin_connection", explode)

    assert probe() == 0


def test_disk_under_watermark_passes(probe: Probe, all_checks_pass: None, _home: Path) -> None:
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
    all_checks_pass: None,
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
    all_checks_pass: None,
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
    db_url: str,
    redis_url: str,
    probe: Probe,
    all_checks_pass: None,
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
    db_url: str,
    redis_url: str,
    probe: Probe,
    all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    signals: list[dict[str, object]],
) -> None:
    """A changed failure reason is a different check, grouped separately by Grafana."""
    monkeypatch.setattr(
        cluster_health,
        "_service_probes",
        partial(service_probe_verdict, failures=["ava-main-frontend"]),
    )
    assert probe() == 1
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: False)
    assert probe() == 1

    assert [signal["check"] for signal in signals] == ["service_probe", "gateway_liveness"]


# ── the gate's entry port rides in check 5 (alert-only) ───────────────────────


def test_dark_gate_fails_the_probe_without_arming_rollback(
    probe: Probe,
    monkeypatch: pytest.MonkeyPatch,
    all_checks_pass: None,
    _home: Path,
    signals: list[dict[str, object]],
) -> None:
    """The boundary this check was placed for: a dark entry port signals and exits 1.
    The 2026-08-01 cause was a converge step that failed to reinstall the launchd
    job — rolling the cluster's code back would re-run that step identically, so
    rollback is not the remedy."""
    monkeypatch.setattr(
        cluster_health,
        "_service_probes",
        partial(service_probe_verdict, failures=["gate entry :3000 not answering (dark)"]),
    )
    assert probe() == 1
    assert any("not answering" in str(signal["message"]) for signal in signals)


# ── the Redis bridge rides in check 5 (alert-only) ───────────────────────────


def test_redis_bridge_probe_reports_running_but_dead_listener(
    monkeypatch: pytest.MonkeyPatch,
    *,
    command_runtime: CommandRuntime,
) -> None:
    """A relay whose PING path is dead is reported even when its process is up."""

    def read_roles(*, machine: MachineIdentity) -> frozenset[str] | None:
        del machine
        return frozenset({"gateway"})

    import cli.commands._repo as _repo_commands
    import cli.commands.converge.redis_bridge as bridge

    monkeypatch.setattr(_repo_commands, "_roles_or_none", read_roles)

    def probe(*, machine: MachineIdentity, config: ConfigBoot) -> bridge.RedisBridgeStatus:
        assert machine is command_runtime.machine
        assert config is command_runtime.config
        return bridge_status(serving=False, detail="connection refused")

    monkeypatch.setattr(bridge, "probe_redis_bridge", probe)

    failure = cluster_health._redis_bridge_probe(
        machine=command_runtime.machine, config=command_runtime.config
    )

    assert failure is not None
    assert "failed authenticated PING" in failure
    assert "connection refused" in failure


def test_redis_bridge_failure_signals_without_arming_rollback(
    probe: Probe,
    monkeypatch: pytest.MonkeyPatch,
    all_checks_pass: None,
    _home: Path,
    signals: list[dict[str, object]],
) -> None:
    failure = "Redis bridge 10.64.0.7:6380 failed authenticated PING (connection refused)"
    monkeypatch.setattr(
        cluster_health, "_redis_bridge_probe", partial(redis_bridge_verdict, result=failure)
    )

    assert probe() == 1
    assert any("Redis bridge" in str(signal["message"]) for signal in signals)


# ── resurrect audit rows are no health signal ────────────────────────────────


def test_resurrect_audit_rows_do_not_make_the_probe_unhealthy(
    db_conn: psycopg.Connection,
    probe: Probe,
    all_checks_pass: None,
    _home: Path,
    signals: list[dict[str, object]],
) -> None:
    """The probe no longer counts `resurrect` audit events: after the automatic
    crash resurrection was removed they only record explicit resurrects and new
    messages to a terminated agent, so a burst of them is not a crash loop."""
    for _ in range(10):
        db_conn.execute(
            "INSERT INTO audit_events (event_uid, ts, agent_id, machine, process, event_name, "
            "level, source) VALUES (%s, now(), 1, 'm', 'p', 'resurrect', 'info', 'test')",
            (uuid4().int % (1 << 62),),
        )
    db_conn.commit()

    assert probe() == 0
    assert signals == []
