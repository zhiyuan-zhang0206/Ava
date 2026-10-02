"""Health observations and graded owner alerts, without release mutations.

Each outage keeps one episode through recovery and deploy suppression. Alert
transport failures cannot hide health failures or break observation.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from cli.commands.cluster import health as cluster_health
from cli.commands.cluster import health_alerts

# Captured at import, before the autouse `_sent_alerts` fixture stubs the module
# attributes — the handles the unit tests use to reach the real send/ingest
# paths.
_REAL_NOTIFY_OWNER = cluster_health.notify_owner
_REAL_INGEST_ALERT = cluster_health._ingest_alert


def _write_aged_alert_state(
    home: Path, message: str, *, age: timedelta = timedelta(minutes=4), severity: str = ""
) -> datetime:
    started_at = datetime.now(UTC) - age
    (home / cluster_health.ALERT_STATE_FILE).write_text(
        f"{message}\n{started_at.isoformat()}\n{severity}"
    )
    return started_at


def _freeze_alert_clock(monkeypatch: pytest.MonkeyPatch, initial: datetime) -> list[datetime]:
    clock = [initial]

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: object | None = None) -> datetime:
            assert tz is UTC
            return clock[0]

    monkeypatch.setattr(health_alerts, "datetime", _FixedDatetime)
    return clock


def test_schema_health_db_flake_is_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient DB connection failure must NOT fail the schema check: code and
    DB may be perfectly in sync while pgbouncer blips (2026-08-03 false alert).
    The gateway-liveness + agent-population checks carry the real signals."""

    class _FlakyConnect:
        def __init__(self, *a: object, **kw: object) -> None:
            raise ConnectionError("pgbouncer blip")

    import base.db

    monkeypatch.setattr(base.db, "connect", _FlakyConnect)
    assert cluster_health._schema_health() is True


def test_schema_health_real_skew_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine code/DB migration-set disagreement still fails the check."""
    import base.db
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

    monkeypatch.setattr(base.db, "connect", _AheadConnect)
    assert cluster_health._schema_health() is False


def test_agent_population_db_error_is_environment_class(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed population query is not evidence that the running code regressed."""
    import base.db

    def _down(**_kwargs: object) -> object:
        raise ConnectionError("pgbouncer unavailable")

    monkeypatch.setattr(base.db, "connect", _down)
    assert cluster_health._agent_population_failure_class(1) == "environment"


def test_liveness_retry_recovers_before_reporting_outage(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brief data-plane restart window is healthy once liveness recovers."""
    answers = iter([False, False, True])
    monkeypatch.setattr(cluster_health, "_gateway_liveness", lambda: next(answers))

    def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(cluster_health.time, "sleep", _no_sleep)
    monkeypatch.setattr(cluster_health, "_agent_population", lambda _min: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(cluster_health, "_crash_loop_detection", lambda _m, _w: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(cluster_health, "_schema_health", lambda: True)
    monkeypatch.setattr(cluster_health, "_service_probes", list)
    monkeypatch.setattr(cluster_health, "_redis_bridge_probe", lambda: None)
    monkeypatch.setattr(cluster_health, "_disk_usage_failure", lambda: None)
    # Check 7 resolves the real prod venv through prod_source_dir() unless
    # stubbed — on a dev box with a healthy prod install this passes by luck,
    # not by hermeticity (same reasoning as the disk-usage stub above).
    monkeypatch.setattr(cluster_health, "_editable_install_failure", lambda: None)

    assert cluster_health.run_health_probe() == 0


def test_environment_liveness_failure_alerts_without_counting(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A data-plane failure alerts but cannot launch an unrelated code rollback."""
    rollback_commands: list[list[str]] = []
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: False)
    monkeypatch.setattr(cluster_health, "_data_plane_abnormal", lambda: True)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kw: rollback_commands.append(command),  # type: ignore[arg-type]
    )

    assert cluster_health.run_health_probe() == 1
    assert rollback_commands == []


def test_code_liveness_failure_is_unhealthy(_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A gateway failure with a healthy data plane remains unhealthy."""
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: False)
    monkeypatch.setattr(cluster_health, "_data_plane_abnormal", lambda: False)

    assert cluster_health.run_health_probe() == 1


def test_agent_population_classifies_db_failure_as_environment(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Population-query connection failure is environmental; a low count is code-class."""
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: True)
    monkeypatch.setattr(cluster_health, "_agent_population", lambda _min: False)  # pyright: ignore[reportUnknownArgumentType]

    def _environment(_min_agents: int) -> str:
        return "environment"

    monkeypatch.setattr(cluster_health, "_agent_population_failure_class", _environment)

    assert cluster_health.run_health_probe() == 1

    def _code(_min_agents: int) -> str:
        return "code"

    monkeypatch.setattr(cluster_health, "_agent_population_failure_class", _code)
    assert cluster_health.run_health_probe() == 1


@pytest.fixture
def _all_checks_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: True)
    monkeypatch.setattr(cluster_health, "_agent_population", lambda _min: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(cluster_health, "_crash_loop_detection", lambda _m, _w: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(cluster_health, "_schema_health", lambda: True)
    monkeypatch.setattr(cluster_health, "_service_probes", list)
    monkeypatch.setattr(cluster_health, "_redis_bridge_probe", lambda: None)
    monkeypatch.setattr(cluster_health, "_disk_usage_failure", lambda: None)
    monkeypatch.setattr(cluster_health, "_editable_install_failure", lambda: None)
    # Check 8 (source tree) is environment-dependent by construction: it reads
    # the real prod checkout, which a tmp-patched `ava_home` resolves to a
    # non-git path. Every pass-all fixture stubs it; the source-tree tests
    # below stub it themselves with specific outcomes.
    monkeypatch.setattr(cluster_health, "_source_tree_failure", lambda: None)  # pyright: ignore[reportUnknownArgumentType]


@pytest.fixture(autouse=True)
def _provider_guard_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Checks 9-10 read a live provider API and the real agent table; stub the
    guard healthy everywhere so this file's tests stay hermetic. The guard's
    own tests below re-point `run_provider_guard` at the real one to exercise
    the wiring."""

    def _ok(_home: Path, *, alert_failure: object) -> None:
        return None

    monkeypatch.setattr(cluster_health, "run_provider_guard", _ok)


@pytest.fixture
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # run_health_probe resolves the alert state home via base.paths.ava_home.
    import base.paths

    monkeypatch.setattr(base.paths, "ava_home", lambda: tmp_path)
    # Check 8 (source tree) derives its checkout from ava_home() and, on a
    # runner with no prod tree, resolves it to a non-git path — the guard
    # (correctly) reports that as "guard skipped" and fails the probe. Unit
    # tests have no prod tree by construction, so stub the lookup itself;
    # check 8's own behavior is pinned by the dedicated tests below.
    from base.deploy.git import cluster_drift

    monkeypatch.setattr(cluster_drift, "prod_source_dir", lambda: None)
    return tmp_path


@pytest.fixture(autouse=True)
def _sent_alerts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture edge-alert summaries; autouse so no test hits a real gateway.

    The probe's edge alerts now flow through `_ingest_alert` (W16); the
    captured value is the stamped summary the ingest payload would carry, so
    assertions on wording keep working. `notify_owner` is NOT stubbed here —
    its own unit tests below reach the real send path via `_REAL_NOTIFY_OWNER`,
    and the fallback tests stub it explicitly where they need to."""
    sent: list[str] = []

    def _capture(*, status: str, message: str, starts_at: object, severity: str = "error") -> None:
        sent.append(cluster_health._alert_summary(recovered=status == "resolved", message=message))

    monkeypatch.setattr(health_alerts, "_ingest_alert", _capture)
    return sent


@pytest.fixture(autouse=True)
def _no_deploy_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep alert tests independent of live deploy state."""
    from ops.deploy_window import DeployWindow

    monkeypatch.setattr(
        "ops.deploy_window.deploy_in_flight",
        lambda **_k: DeployWindow(active=False, detail="no deploy in flight"),  # pyright: ignore[reportUnknownArgumentType]
    )


# ─── per-service check (5) + owner alerts ────────────────────────────────────


def test_service_probe_failure_alerts(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _sent_alerts: list[str],
) -> None:
    """A dead selected service fails the probe and reports its outage."""
    monkeypatch.setattr(cluster_health, "_service_probes", lambda: ["ava-main-frontend"])
    _write_aged_alert_state(_home, "FAIL: service probe — not healthy: ava-main-frontend")

    rc = cluster_health.run_health_probe()

    assert rc == 1
    assert len(_sent_alerts) == 1
    assert "ava-main-frontend" in _sent_alerts[0]


def test_service_probe_deploy_window_pauses_alert_grade(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _sent_alerts: list[str],
) -> None:
    from ops.deploy_window import DeployWindow

    monkeypatch.setattr(
        "ops.deploy_window.deploy_in_flight",
        lambda **_kw: DeployWindow(active=True, detail="rollout live"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(cluster_health, "_service_probes", lambda: ["ava-main-frontend"])
    _write_aged_alert_state(
        _home,
        "FAIL: service probe — not healthy: ava-main-frontend",
        age=timedelta(minutes=11),
    )

    assert cluster_health.run_health_probe() == 1
    assert _sent_alerts == []


def test_disk_over_watermark_fails_and_alerts(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _sent_alerts: list[str],
) -> None:
    """A data volume over the 90% watermark fails the probe (exit 1) and
    alerts the owner; rolling
    back code frees no disk space (the 2026-08-08 outage class: checkpoint
    growth filled the disk and the gateway could not start)."""
    monkeypatch.setattr(
        cluster_health, "_disk_usage_failure", lambda: "data volume 92.4% used (watermark 90%)"
    )
    _write_aged_alert_state(
        _home,
        "FAIL: disk usage — data volume 92.4% used (watermark 90%)",
    )

    rc = cluster_health.run_health_probe()

    assert rc == 1
    assert len(_sent_alerts) == 1
    assert "disk usage" in _sent_alerts[0]
    assert "92.4%" in _sent_alerts[0]


def test_deploy_never_explains_full_disk(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _sent_alerts: list[str],
) -> None:
    from ops.deploy_window import DeployWindow

    monkeypatch.setattr(
        "ops.deploy_window.deploy_in_flight",
        lambda **_kw: DeployWindow(active=True, detail="rollout live"),  # pyright: ignore[reportUnknownArgumentType]
    )
    message = "FAIL: disk usage — data volume 92.4% used (watermark 90%)"
    monkeypatch.setattr(
        cluster_health, "_disk_usage_failure", lambda: "data volume 92.4% used (watermark 90%)"
    )
    _write_aged_alert_state(_home, message, age=timedelta(minutes=11))

    assert cluster_health.run_health_probe() == 1
    assert len(_sent_alerts) == 1
    assert "disk usage" in _sent_alerts[0]


def test_wal_archiving_failure_alerts_with_a_stable_message(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _sent_alerts: list[str],
) -> None:
    """A broken archiver fails the probe like the disk check does; the text is the
    episode's identity, so the state file holds exactly the message."""
    failure = "WAL archiving: the archiver is failing"
    monkeypatch.setattr(cluster_health, "_walg_archive_failure", lambda: failure)
    _write_aged_alert_state(_home, f"FAIL: {failure}")

    assert cluster_health.run_health_probe() == 1

    assert len(_sent_alerts) == 1 and failure in _sent_alerts[0]
    assert (_home / cluster_health.ALERT_STATE_FILE).read_text().splitlines()[0] == (
        f"FAIL: {failure}"
    )


def test_wal_archiving_is_never_dialed_while_off(
    _all_checks_pass: None, _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.gateway_side.walg import probe

    def explode() -> object:
        raise AssertionError("the probe dialed Postgres while WAL-G is off")

    monkeypatch.setattr(probe, "admin_connection", explode)

    assert cluster_health.run_health_probe() == 0


def test_disk_under_watermark_passes(_all_checks_pass: None, _home: Path) -> None:
    """Healthy disk usage keeps the probe green (no alert, exit 0)."""
    rc = cluster_health.run_health_probe()
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


def test_source_tree_failure_alerts(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _sent_alerts: list[str],
) -> None:
    """A tampered prod source tree fails the probe (exit 1) and alerts the
    owner; rolling back code does
    not undo an on-disk edit (the 2026-08-28 outage class: edited source broke
    `import ava` for every agent on the box)."""
    message = "prod source tree tampered: untracked outside whitelist: junk.txt"
    monkeypatch.setattr(cluster_health, "_source_tree_failure", lambda: message)  # pyright: ignore[reportUnknownArgumentType]
    _write_aged_alert_state(_home, f"FAIL: source tree — {message}")

    rc = cluster_health.run_health_probe()

    assert rc == 1
    assert len(_sent_alerts) == 1
    assert "source tree" in _sent_alerts[0]
    assert "junk.txt" in _sent_alerts[0]


def test_source_tree_clean_passes(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean tree must not fail the probe — the whitelist exists so the
    routine frontend/ build output never fires a false alarm. The check is
    patched so the test does not depend on this host's prod tree state."""
    monkeypatch.setattr(cluster_health, "_source_tree_failure", lambda: None)  # pyright: ignore[reportUnknownArgumentType]

    rc = cluster_health.run_health_probe()

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


def test_alert_edge_triggered_once_per_outage(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _sent_alerts: list[str],
) -> None:
    """The probe fires every few minutes; a persistent outage must alert once on
    the first graded transition and once on recovery — not once per run."""
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: False)
    _write_aged_alert_state(
        _home, "FAIL: gateway liveness — health endpoint unreachable or non-200"
    )
    assert cluster_health.run_health_probe() == 1
    assert cluster_health.run_health_probe() == 1
    assert len(_sent_alerts) == 1
    assert "unhealthy" in _sent_alerts[0]

    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: True)
    assert cluster_health.run_health_probe() == 0
    assert len(_sent_alerts) == 2
    assert "recovered" in _sent_alerts[1]
    assert not (_home / cluster_health.ALERT_STATE_FILE).exists()
    # Healthy again — no further alert.
    assert cluster_health.run_health_probe() == 0
    assert len(_sent_alerts) == 2


def test_alert_re_fires_when_failure_reason_changes(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _sent_alerts: list[str],
) -> None:
    """A changed failure reason starts a fresh episode and grades independently."""
    monkeypatch.setattr(cluster_health, "_service_probes", lambda: ["ava-main-frontend"])
    service_message = "FAIL: service probe — not healthy: ava-main-frontend"
    _write_aged_alert_state(_home, service_message)
    assert cluster_health.run_health_probe() == 1
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: False)
    assert cluster_health.run_health_probe() == 1

    assert len(_sent_alerts) == 1
    assert "service probe" in _sent_alerts[0]
    gateway_message = "FAIL: gateway liveness — health endpoint unreachable or non-200"
    _write_aged_alert_state(_home, gateway_message)
    assert cluster_health.run_health_probe() == 1
    assert len(_sent_alerts) == 2
    assert "gateway liveness" in _sent_alerts[1]


def test_service_probes_skips_gated_but_rejects_unknown_specs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every intended service needs positive evidence; disabled services do not."""
    import cli.commands._probe as _probe_commands
    import cli.commands._repo as _repo_commands
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

    monkeypatch.setattr(_probe_commands, "_probe_service", _probe)

    assert cluster_health._service_probes() == ["frontend", "browser-mcp"]


def test_service_probes_skips_gated_otel_collector_on_non_lgtm_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli.commands._probe as _probe_commands
    import cli.commands._repo as _repo_commands
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

    monkeypatch.setattr(_probe_commands, "_probe_service", _record_probe)

    assert cluster_health._service_probes() == []
    assert "otel-collector" not in recorded_sessions


def test_service_probes_checks_otel_collector_on_non_lgtm_gateway_with_explicit_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli.commands._probe as _probe_commands
    import cli.commands._repo as _repo_commands
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

    monkeypatch.setattr(_probe_commands, "_probe_service", _record_probe)

    assert cluster_health._service_probes() == []
    assert "otel-collector" in recorded_sessions


def test_service_probes_checks_otel_collector_on_lgtm_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli.commands._probe as _probe_commands
    import cli.commands._repo as _repo_commands
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

    monkeypatch.setattr(_probe_commands, "_probe_service", _record_probe)

    assert cluster_health._service_probes() == []
    assert "otel-collector" in recorded_sessions


def test_service_probes_carry_the_failing_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner's alert is the only thing a human sees, so it has to say WHICH
    fact failed: "answering, but its home is /home/ava/.ava" is another unit on
    this unit's port — a different incident from "nothing is listening", and one
    no amount of waiting fixes."""
    import cli.commands._probe as _probe_commands
    import cli.commands._repo as _repo_commands
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
        "_probe_service",
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
    import cli.commands._probe as _probe_commands
    import cli.commands._repo as _repo_commands
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
    monkeypatch.setattr(_probe_commands, "_probe_service", probe)
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


# ─── cluster-stamped outbound alerts ─────────────────────────────────────────


def test_notify_owner_stamps_home_label(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every ops alert carries the cluster name so the owner can tell which
    cluster is talking — a preview cluster's alert must not read like a prod
    incident. Stamping in the single send point covers every alert uniformly.

    Calls the real `notify_owner` (the autouse `_sent_alerts` fixture stubs the
    module attribute, so the captured `_REAL_NOTIFY_OWNER` is used to reach the
    actual send path). It POSTs to the im_bridge daemon's health-port `/send`
    RPC — stub `httpx.post` to capture the request."""

    import httpx

    import base.cluster
    from base.config import settings
    from base.daemon.endpoints import ServiceEndpoints

    monkeypatch.setattr(settings.alerts, "im_notify_enabled", True)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "test-secret")
    monkeypatch.setattr(base.cluster, "home_label", lambda _home: ".ava-preview-42")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("base.paths.ava_home", lambda: tmp_path / ".ava-preview-42")

    sent: list[tuple[str, dict[str, str], dict[str, str]]] = []

    class _Resp:
        def raise_for_status(self) -> None:
            pass

    def _post(url: str, *, json: dict[str, str], headers: dict[str, str], timeout: float) -> _Resp:
        sent.append((url, json, headers))
        return _Resp()

    monkeypatch.setattr(httpx, "post", _post)

    _REAL_NOTIFY_OWNER("[health-probe] cluster unhealthy: FAIL: schema health")

    assert len(sent) == 1
    url, payload, headers = sent[0]
    assert (
        url
        == f"http://127.0.0.1:{ServiceEndpoints.from_settings().of('im_bridge').health_port}/send"
    )
    assert headers["Authorization"] == "Bearer test-secret"
    assert payload == {
        "text": "[.ava-preview-42] [health-probe] cluster unhealthy: FAIL: schema health"
    }


def test_notify_owner_failed_send_does_not_leak_secret(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """A failed send must not write the cluster secret to the log. The secret
    rides in the Authorization header; httpx embeds the request (but never its
    headers) in the exception repr, so `notify_owner` must never format the
    exception itself."""

    import httpx

    import base.cluster
    from base.config import settings

    secret = "SUPERSECRET"  # noqa: S105 — test fixture
    monkeypatch.setattr(settings.alerts, "im_notify_enabled", True)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", secret)
    monkeypatch.setattr(base.cluster, "home_label", lambda _home: ".ava-main")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("base.paths.ava_home", lambda: tmp_path / ".ava-main")

    # A 401 from the daemon — raise_for_status() raises httpx.HTTPStatusError,
    # the path most likely to leak.
    req = httpx.Request("POST", "http://127.0.0.1:8111/send")
    resp = httpx.Response(401, request=req, json={"error": "unauthorized"})
    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: resp)  # pyright: ignore[reportUnknownArgumentType]

    _REAL_NOTIFY_OWNER("[health-probe] cluster unhealthy: FAIL")  # never raises

    err = capsys.readouterr().err
    assert "delivery failed" in err  # it did log the failure
    assert secret not in err  # but not the cluster secret


def test_notify_owner_im_bridge_down_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """The alert is a side channel: when the im_bridge daemon is down the probe
    must still complete — no raise, just a stderr note naming the failure
    class. A dead bridge must never break health observation."""

    import httpx

    import base.cluster
    from base.config import settings

    monkeypatch.setattr(settings.alerts, "im_notify_enabled", True)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "test-secret")
    monkeypatch.setattr(base.cluster, "home_label", lambda _home: ".ava-main")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("base.paths.ava_home", lambda: tmp_path / ".ava-main")

    def _post(*_a: object, **_k: object) -> None:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", _post)

    _REAL_NOTIFY_OWNER("[health-probe] cluster unhealthy: FAIL")  # never raises

    assert "delivery failed: ConnectError" in capsys.readouterr().err


def test_notify_owner_skips_when_im_notify_disabled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """`AVA_ALERTS_IM_NOTIFY_ENABLED=false` silences the probe's owner alerts
    too — one master switch for every IM notification (the same flag the
    gateway's ops-alerts ingest honours). No HTTP call is made."""

    import httpx

    import base.cluster
    from base.config import settings

    monkeypatch.setattr(settings.alerts, "im_notify_enabled", False)
    monkeypatch.setattr(base.cluster, "home_label", lambda _home: ".ava-main")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("base.paths.ava_home", lambda: tmp_path / ".ava-main")

    called = False

    def _post(*_a: object, **_k: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(httpx, "post", _post)

    _REAL_NOTIFY_OWNER("[health-probe] cluster unhealthy: FAIL")

    assert not called
    assert "skipped" in capsys.readouterr().err


def test_notify_owner_honours_im_bridge_health_url_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`AVA_IM_BRIDGE_HEALTH_URL` (a remote host's bridge) is honoured when
    set; the loopback health port is only the fallback."""

    import httpx

    import base.cluster
    from base.config import settings

    monkeypatch.setattr(settings.alerts, "im_notify_enabled", True)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "test-secret")
    monkeypatch.setattr(settings.services, "im_bridge_health_url", "http://10.0.0.5:9111/")
    monkeypatch.setattr(base.cluster, "home_label", lambda _home: ".ava-main")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("base.paths.ava_home", lambda: tmp_path / ".ava-main")

    sent: list[str] = []

    class _Resp:
        def raise_for_status(self) -> None:
            pass

    def _post(url: str, **_: object) -> _Resp:
        sent.append(url)
        return _Resp()

    monkeypatch.setattr(httpx, "post", _post)

    _REAL_NOTIFY_OWNER("[health-probe] cluster unhealthy: FAIL")

    assert sent == ["http://10.0.0.5:9111/send"]


# ── the gate's entry port rides in check 5 (alert-only) ───────────────────────


def test_dark_gate_fails_the_probe_without_arming_rollback(
    monkeypatch: pytest.MonkeyPatch, _all_checks_pass: None, _home: Path, _sent_alerts: list[str]
) -> None:
    """The boundary this check was placed for: a dark entry port alerts the owner and
    exits 1. The 2026-08-01 cause was a
    converge step that failed to reinstall the launchd job — rolling the cluster's
    code back would re-run that step identically, so rollback is not the remedy."""
    monkeypatch.setattr(
        cluster_health, "_service_probes", lambda: ["gate entry :3000 not answering (dark)"]
    )
    _write_aged_alert_state(
        _home,
        "FAIL: service probe — not healthy: gate entry :3000 not answering (dark)",
    )
    assert cluster_health.run_health_probe() == 1
    assert any("not answering" in a for a in _sent_alerts)


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


def test_redis_bridge_failure_alerts_without_arming_rollback(
    monkeypatch: pytest.MonkeyPatch,
    _all_checks_pass: None,
    _home: Path,
    _sent_alerts: list[str],
) -> None:
    failure = "Redis bridge 10.64.0.7:6380 failed authenticated PING (connection refused)"
    monkeypatch.setattr(cluster_health, "_redis_bridge_probe", lambda: failure)
    _write_aged_alert_state(_home, f"FAIL: service probe — not healthy: {failure}")

    assert cluster_health.run_health_probe() == 1
    assert any("Redis bridge" in alert for alert in _sent_alerts)


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
    db_conn: psycopg.Connection,
) -> None:
    """Only `audit_events` resurrect rows inside the window count (W9 A.25): the telemetry
    mirror of the fact and an older audit row must not trigger a crash-loop alert."""
    for _ in range(2):
        _record_resurrect(db_conn, 1, minutes_ago=10)
    _record_resurrect(db_conn, 1, minutes_ago=300)  # outside the window
    _record_resurrect(db_conn, 1, minutes_ago=5, table="telemetry_events")

    # At the threshold it is still healthy.
    assert cluster_health._crash_loop_detection(max_restarts=2, window_minutes=60) is True

    # A third audit resurrect in the window exceeds the threshold -> unhealthy.
    _record_resurrect(db_conn, 1, minutes_ago=3)
    assert cluster_health._crash_loop_detection(max_restarts=2, window_minutes=60) is False


def test_crash_loop_is_judged_per_agent(db_conn: psycopg.Connection) -> None:
    for agent_id in (1, 2, 3):
        for _ in range(2):
            _record_resurrect(db_conn, agent_id, minutes_ago=10)

    assert cluster_health._crash_loop_detection(max_restarts=2, window_minutes=60) is True


def test_crash_loop_is_healthy_when_the_database_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable(**_kwargs: object) -> object:
        raise OSError("database unreachable")

    monkeypatch.setattr("base.db.connect", unreachable)

    assert cluster_health._crash_loop_detection(max_restarts=0, window_minutes=60) is True
