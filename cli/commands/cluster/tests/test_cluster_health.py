"""Healthy probes stay silent; unhealthy probes signal without release mutations.
Grafana rules grade notifications from the recorded health_probe_failing event."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest

from base.db.tests.fakes import patch_database
from cli.commands.cluster import health as cluster_health


@pytest.fixture(autouse=True)
def config_boot_environment() -> Generator[None]:
    """Restore process delivery from the health operation's boot."""
    with patch.dict(os.environ):
        yield


def test_schema_health_db_flake_is_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient DB connection failure must NOT fail the schema check: code and
    DB may be perfectly in sync while pgbouncer blips (2026-08-03 false alert).
    The gateway-liveness + agent-population checks carry the real signals."""

    class _FlakyConnect:
        def __init__(self, *a: object, **kw: object) -> None:
            raise ConnectionError("pgbouncer blip")

    patch_database(monkeypatch, connect=_FlakyConnect)
    assert cluster_health._schema_health() is True


def test_schema_health_real_skew_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert cluster_health._schema_health() is False


def test_agent_population_db_error_is_environment_class(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed population query is not evidence that the running code regressed."""

    def _down(**_kwargs: object) -> object:
        raise ConnectionError("pgbouncer unavailable")

    patch_database(monkeypatch, connect=_down)
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


def test_environment_liveness_failure_signals_without_counting(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A data-plane failure signals but cannot launch an unrelated code rollback."""
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


def _no_init(**_kwargs: object) -> None:
    return None


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

    def _ok(*, report: object) -> None:
        return None

    monkeypatch.setattr(cluster_health, "run_provider_guard", _ok)


@pytest.fixture
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Check 8 (source tree) derives its checkout from ava_home() and, on a
    # runner with no prod tree, resolves it to a non-git path — the guard
    # (correctly) reports that as "guard skipped" and fails the probe. Unit
    # tests have no prod tree by construction, so stub the lookup itself;
    # check 8's own behavior is pinned by the dedicated tests below.
    from base.deploy.git import cluster_drift

    monkeypatch.setattr(cluster_drift, "prod_source_dir", lambda: None)
    return tmp_path


@pytest.fixture(autouse=True)
def _ran() -> list[dict[str, object]]:
    """The attributes of every `health_probe_ran` heartbeat the probe emits."""
    return []


@pytest.fixture(autouse=True)
def _signals(
    monkeypatch: pytest.MonkeyPatch, _ran: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Capture the attributes of every `health_probe_failing` event the probe emits
    (heartbeats go to `_ran`).

    Autouse so no test reaches the real event pipeline."""
    emitted: list[dict[str, object]] = []

    def _emit(category: str, event_name: str, **kwargs: object) -> None:
        assert category == "telemetry"
        attributes = kwargs["attributes"]
        assert isinstance(attributes, dict)
        if event_name == "health_probe_ran":
            assert "level" not in kwargs  # info
            _ran.append(dict(attributes))  # pyright: ignore[reportUnknownArgumentType]
            return
        assert event_name == "health_probe_failing"
        assert kwargs["level"] == "warning"
        emitted.append(dict(attributes))  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(cluster_health.telemetry, "emit", _emit)
    monkeypatch.setattr(cluster_health.telemetry, "init_telemetry", _no_init)
    return emitted


def test_every_run_emits_the_heartbeat_healthy_or_not(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _signals: list[dict[str, object]],
    _ran: list[dict[str, object]],
) -> None:
    """`health_probe_ran` is the probe's dead-man heartbeat: one per run on every
    path, carrying the unhealthy-check count; the failing event is separate."""
    assert cluster_health.run_health_probe() == 0
    assert _ran == [{"unhealthy_checks": 0}]
    assert _signals == []

    monkeypatch.setattr(cluster_health, "_service_probes", lambda: ["ava-main-frontend"])
    assert cluster_health.run_health_probe() == 1  # the alert-only path
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: False)
    assert cluster_health.run_health_probe() == 1  # the primary-check path
    assert _ran == [{"unhealthy_checks": 0}, {"unhealthy_checks": 1}, {"unhealthy_checks": 1}]
    assert len(_signals) == 2


def test_a_refused_checkout_emits_no_heartbeat(
    _home: Path, monkeypatch: pytest.MonkeyPatch, _ran: list[dict[str, object]]
) -> None:
    """A wrong-checkout refusal (exit 2) never observed the cluster, so it is not a run."""
    import base.paths

    def _refuse(_root: Path) -> str:
        return "dev checkout"

    monkeypatch.setattr(base.paths, "prod_service_checkout_error", _refuse)

    assert cluster_health.run_health_probe() == 2
    assert _ran == []


# ─── failing checks emit the signal ──────────────────────────────────────────


def test_service_probe_failure_emits_the_alert_only_signal(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _signals: list[dict[str, object]],
) -> None:
    """A dead selected service fails the probe and emits its failing check."""
    monkeypatch.setattr(cluster_health, "_service_probes", lambda: ["ava-main-frontend"])

    assert cluster_health.run_health_probe() == 1

    assert _signals == [
        {
            "check": "service_probe",
            "failure_class": "alert-only",
            "message": "FAIL: service probe — not healthy: ava-main-frontend",
        }
    ]


def test_disk_over_watermark_fails_and_signals(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _signals: list[dict[str, object]],
) -> None:
    """A data volume over the 90% watermark fails the probe (exit 1) and emits
    the `disk_usage` signal; rolling back code frees no disk space (the
    2026-08-08 outage class: checkpoint growth filled the disk and the gateway
    could not start)."""
    monkeypatch.setattr(
        cluster_health, "_disk_usage_failure", lambda: "data volume 92.4% used (watermark 90%)"
    )

    assert cluster_health.run_health_probe() == 1

    assert len(_signals) == 1
    assert _signals[0]["check"] == "disk_usage"
    assert _signals[0]["failure_class"] == "alert-only"
    assert "92.4%" in str(_signals[0]["message"])


def test_wal_archiving_failure_signals_with_its_message(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _signals: list[dict[str, object]],
) -> None:
    """A broken archiver fails the probe like the disk check does."""
    failure = "WAL archiving: the archiver is failing"

    def failing_archiver(**_inputs: object) -> str:
        return failure

    monkeypatch.setattr(cluster_health, "_walg_archive_failure", failing_archiver)

    assert cluster_health.run_health_probe() == 1

    assert _signals == [
        {"check": "walg_archive", "failure_class": "alert-only", "message": f"FAIL: {failure}"}
    ]


def test_wal_archiving_is_never_dialed_while_off(
    _all_checks_pass: None, _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.backup.walg import probe

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


def test_source_tree_failure_signals(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _signals: list[dict[str, object]],
) -> None:
    """A tampered prod source tree fails the probe (exit 1) and emits the
    `source_tree` signal; rolling back code does not undo an on-disk edit (the
    2026-08-28 outage class: edited source broke `import ava` for every agent on
    the box)."""
    message = "prod source tree tampered: untracked outside whitelist: junk.txt"
    monkeypatch.setattr(cluster_health, "_source_tree_failure", lambda: message)  # pyright: ignore[reportUnknownArgumentType]

    assert cluster_health.run_health_probe() == 1

    assert len(_signals) == 1
    assert _signals[0]["check"] == "source_tree"
    assert "junk.txt" in str(_signals[0]["message"])


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


def test_signal_repeats_every_unhealthy_run_and_stops_when_healthy(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _signals: list[dict[str, object]],
) -> None:
    """The probe fires every few minutes; a persistent outage emits the signal on
    every run (a Grafana `for:` needs continuous samples), a healthy run emits
    nothing, and no state file is kept."""
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: False)
    assert cluster_health.run_health_probe() == 1
    assert cluster_health.run_health_probe() == 1
    assert [signal["check"] for signal in _signals] == ["gateway_liveness"] * 2

    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: True)
    assert cluster_health.run_health_probe() == 0
    assert len(_signals) == 2
    assert not list(_home.iterdir())


def test_signal_names_the_failed_check(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _signals: list[dict[str, object]],
) -> None:
    """A changed failure reason is a different check, grouped separately by Grafana."""
    monkeypatch.setattr(cluster_health, "_service_probes", lambda: ["ava-main-frontend"])
    assert cluster_health.run_health_probe() == 1
    monkeypatch.setattr(cluster_health, "_gateway_liveness_with_retry", lambda: False)
    assert cluster_health.run_health_probe() == 1

    assert [signal["check"] for signal in _signals] == ["service_probe", "gateway_liveness"]


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
    """The signal's message is the only diagnostic a human sees, so it has to say WHICH
    fact failed: "answering, but its home is /home/ava/.ava" is another unit on
    this unit's port — a different incident from "nothing is listening", and one
    no amount of waiting fixes."""
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
    monkeypatch: pytest.MonkeyPatch,
    _all_checks_pass: None,
    _home: Path,
    _signals: list[dict[str, object]],
) -> None:
    """The boundary this check was placed for: a dark entry port signals and exits 1.
    The 2026-08-01 cause was a converge step that failed to reinstall the launchd
    job — rolling the cluster's code back would re-run that step identically, so
    rollback is not the remedy."""
    monkeypatch.setattr(
        cluster_health, "_service_probes", lambda: ["gate entry :3000 not answering (dark)"]
    )
    assert cluster_health.run_health_probe() == 1
    assert any("not answering" in str(signal["message"]) for signal in _signals)


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
    monkeypatch: pytest.MonkeyPatch,
    _all_checks_pass: None,
    _home: Path,
    _signals: list[dict[str, object]],
) -> None:
    failure = "Redis bridge 10.64.0.7:6380 failed authenticated PING (connection refused)"
    monkeypatch.setattr(cluster_health, "_redis_bridge_probe", lambda: failure)

    assert cluster_health.run_health_probe() == 1
    assert any("Redis bridge" in str(signal["message"]) for signal in _signals)


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

    patch_database(monkeypatch, connect=unreachable)

    assert cluster_health._crash_loop_detection(max_restarts=0, window_minutes=60) is True
