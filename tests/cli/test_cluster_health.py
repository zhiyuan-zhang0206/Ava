"""Health-probe failure counting + auto-rollback gating (`ava cluster health-probe`).

The probe owns the consecutive-failure counter and the rollback trigger (there
is no shell wrapper on either platform). These tests pin: the counter file
arithmetic, that a healthy run clears the counter, that rollback fires only at
the threshold, and that the counter is reset only when rollback succeeds.

The edge-triggered owner alerts (W16) are pinned here too: the probe posts
each healthy<->unhealthy edge to the gateway's alerts ingest
(source='health-probe'; stubbed in most tests), and when the gateway is
unreachable it falls back to running the same ingest logic locally — still
exactly one IM notification and one alerts row per transition.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cli.commands import _cluster_health, _health_alerts

# Captured at import, before the autouse `_sent_alerts` fixture stubs the module
# attributes — the handles the unit tests use to reach the real send/ingest
# paths.
_REAL_NOTIFY_OWNER = _cluster_health._notify_owner
_REAL_INGEST_ALERT = _cluster_health._ingest_alert


def _read_count(home: Path) -> str:
    return (home / _cluster_health.FAILURE_COUNT_FILE).read_text()


def _count_record(count: int) -> str:
    return f"{count}\ncode\nprior failure\n{datetime.now(UTC).isoformat()}"


def _write_aged_alert_state(
    home: Path, message: str, *, age: timedelta = timedelta(minutes=4), severity: str = ""
) -> datetime:
    started_at = datetime.now(UTC) - age
    (home / _cluster_health.ALERT_STATE_FILE).write_text(
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

    monkeypatch.setattr(_health_alerts, "datetime", _FixedDatetime)
    return clock


def test_schema_health_db_flake_is_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient DB connection failure must NOT fail the schema check: code and
    DB may be perfectly in sync while pgbouncer blips (2026-08-03 false alert).
    The gateway-liveness + agent-population checks carry the real signals."""

    class _FlakyConnect:
        def __init__(self, *a: object, **kw: object) -> None:
            raise ConnectionError("pgbouncer blip")

    import shared.db

    monkeypatch.setattr(shared.db, "connect", _FlakyConnect)
    assert _cluster_health._schema_health() is True


def test_schema_health_real_skew_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine code/DB migration-set disagreement still fails the check."""
    import shared.db
    from shared.migrations import CodeBehindSchema

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

    monkeypatch.setattr(shared.db, "connect", _AheadConnect)
    assert _cluster_health._schema_health() is False


def test_agent_population_db_error_is_environment_class(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed population query is not evidence that the running code regressed."""
    import shared.db

    def _down(**_kwargs: object) -> object:
        raise ConnectionError("pgbouncer unavailable")

    monkeypatch.setattr(shared.db, "connect", _down)
    assert _cluster_health._agent_population_failure_class(1) == "environment"


def test_increment_from_missing_file(tmp_path: Path) -> None:
    # No counter file yet -> first increment yields 1.
    assert _cluster_health._increment_failure_count(tmp_path) == 1
    lines = _read_count(tmp_path).splitlines()
    assert lines[:3] == ["1", "code", ""]
    datetime.fromisoformat(lines[3])
    assert _cluster_health._increment_failure_count(tmp_path) == 2


def test_increment_recovers_from_garbage(tmp_path: Path) -> None:
    (tmp_path / _cluster_health.FAILURE_COUNT_FILE).write_text("not-a-number")
    # A corrupt counter is treated as 0, so the next increment yields 1.
    assert _cluster_health._increment_failure_count(tmp_path) == 1


def test_increment_treats_legacy_plain_counter_as_zero(tmp_path: Path) -> None:
    """A pre-classification counter cannot certify a code-failure streak."""
    (tmp_path / _cluster_health.FAILURE_COUNT_FILE).write_text("2")

    assert _cluster_health._increment_failure_count(tmp_path) == 1


def test_reset_writes_zero(tmp_path: Path) -> None:
    (tmp_path / _cluster_health.FAILURE_COUNT_FILE).write_text("7")
    _cluster_health._reset_failure_count(tmp_path)
    lines = _read_count(tmp_path).splitlines()
    assert lines[:3] == ["0", "code", ""]
    datetime.fromisoformat(lines[3])


def test_liveness_retry_recovers_before_the_counter_is_armed(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brief data-plane restart window is healthy once liveness recovers."""
    answers = iter([False, False, True])
    monkeypatch.setattr(_cluster_health, "_gateway_liveness", lambda: next(answers))

    def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_cluster_health.time, "sleep", _no_sleep)
    monkeypatch.setattr(_cluster_health, "_agent_population", lambda _min: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cluster_health, "_crash_loop_detection", lambda _m, _w: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cluster_health, "_schema_health", lambda: True)
    monkeypatch.setattr(_cluster_health, "_service_probes", list)
    monkeypatch.setattr(_cluster_health, "_gate_probe", lambda: None)
    monkeypatch.setattr(_cluster_health, "_redis_bridge_probe", lambda: None)
    monkeypatch.setattr(_cluster_health, "_disk_usage_failure", lambda: None)
    # Check 7 resolves the real prod venv through prod_source_dir() unless
    # stubbed — on a dev box with a healthy prod install this passes by luck,
    # not by hermeticity (same reasoning as the disk-usage stub above).
    monkeypatch.setattr(_cluster_health, "_editable_install_failure", lambda: None)

    assert _cluster_health.run_health_probe(auto_rollback=True) == 0
    assert _read_count(_home).splitlines()[0] == "0"


def test_environment_liveness_failure_alerts_without_counting(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A data-plane failure alerts but cannot launch an unrelated code rollback."""
    rollback_commands: list[list[str]] = []
    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: False)
    monkeypatch.setattr(_cluster_health, "_data_plane_abnormal", lambda: True)
    monkeypatch.setattr(
        _health_alerts.subprocess,
        "run",
        lambda command, **_kw: rollback_commands.append(command),  # type: ignore[arg-type]
    )

    assert _cluster_health.run_health_probe(auto_rollback=True, threshold=1) == 1
    assert not (_home / _cluster_health.FAILURE_COUNT_FILE).exists()
    assert rollback_commands == []


def test_code_liveness_failure_counts_toward_rollback(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway failure with a healthy data plane remains rollback evidence."""
    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: False)
    monkeypatch.setattr(_cluster_health, "_data_plane_abnormal", lambda: False)

    assert _cluster_health.run_health_probe(auto_rollback=True, threshold=3) == 1
    assert _read_count(_home).splitlines()[0] == "1"


def test_agent_population_classifies_db_failure_as_environment(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Population-query connection failure is environmental; a low count is code-class."""
    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: True)
    monkeypatch.setattr(_cluster_health, "_agent_population", lambda _min: False)  # pyright: ignore[reportUnknownArgumentType]

    def _environment(_min_agents: int) -> str:
        return "environment"

    monkeypatch.setattr(_cluster_health, "_agent_population_failure_class", _environment)

    assert _cluster_health.run_health_probe(auto_rollback=True) == 1
    assert not (_home / _cluster_health.FAILURE_COUNT_FILE).exists()

    def _code(_min_agents: int) -> str:
        return "code"

    monkeypatch.setattr(_cluster_health, "_agent_population_failure_class", _code)
    assert _cluster_health.run_health_probe(auto_rollback=True) == 1
    assert _read_count(_home).splitlines()[0] == "1"


def test_environment_failure_keeps_the_previous_code_failure_count(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a passing gating run resets the code-failure streak (code, env, code = 2)."""
    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: False)
    monkeypatch.setattr(_cluster_health, "_data_plane_abnormal", lambda: False)
    assert _cluster_health.run_health_probe(auto_rollback=True, threshold=3) == 1

    monkeypatch.setattr(_cluster_health, "_data_plane_abnormal", lambda: True)
    assert _cluster_health.run_health_probe(auto_rollback=True, threshold=3) == 1

    monkeypatch.setattr(_cluster_health, "_data_plane_abnormal", lambda: False)
    assert _cluster_health.run_health_probe(auto_rollback=True, threshold=3) == 1
    assert _read_count(_home).splitlines()[0] == "2"


def test_pending_lkg_advances_after_two_gating_passes(
    _all_checks_pass: None, _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new pin becomes LKG only after two consecutive healthy observations."""
    promoted: list[float] = []
    monkeypatch.setattr(
        "shared.cluster_pin.get_pending_known_good", lambda: ("PENDINGSHA", datetime.now(UTC))
    )

    def _promote(*, min_age_s: float) -> bool:
        promoted.append(min_age_s)
        return True

    monkeypatch.setattr("shared.cluster_pin.promote_pending_known_good_if_ready", _promote)

    assert _cluster_health.run_health_probe() == 0
    marker = _home / _cluster_health.PENDING_LKG_PASSES_FILE
    assert marker.read_text().splitlines() == ["PENDINGSHA", "1"]

    assert _cluster_health.run_health_probe() == 0
    assert promoted == [_cluster_health.PENDING_LKG_MIN_AGE_S]
    assert not marker.exists()


def test_gating_failure_resets_pending_lkg_streak(
    _all_checks_pass: None, _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any gating failure restarts the observation window, including environmental ones."""
    monkeypatch.setattr(
        "shared.cluster_pin.get_pending_known_good", lambda: ("PENDINGSHA", datetime.now(UTC))
    )

    def _not_ready(*, min_age_s: float) -> bool:
        return False

    monkeypatch.setattr("shared.cluster_pin.promote_pending_known_good_if_ready", _not_ready)

    assert _cluster_health.run_health_probe() == 0
    marker = _home / _cluster_health.PENDING_LKG_PASSES_FILE
    assert marker.exists()

    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: False)
    monkeypatch.setattr(_cluster_health, "_data_plane_abnormal", lambda: True)
    assert _cluster_health.run_health_probe() == 1
    assert not marker.exists()

    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: True)
    assert _cluster_health.run_health_probe() == 0
    assert marker.read_text().splitlines() == ["PENDINGSHA", "1"]


@pytest.fixture
def _all_checks_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: True)
    monkeypatch.setattr(_cluster_health, "_agent_population", lambda _min: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cluster_health, "_crash_loop_detection", lambda _m, _w: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cluster_health, "_schema_health", lambda: True)
    monkeypatch.setattr(_cluster_health, "_service_probes", list)
    monkeypatch.setattr(_cluster_health, "_gate_probe", lambda: None)
    monkeypatch.setattr(_cluster_health, "_redis_bridge_probe", lambda: None)
    monkeypatch.setattr(_cluster_health, "_disk_usage_failure", lambda: None)
    monkeypatch.setattr(_cluster_health, "_editable_install_failure", lambda: None)
    # Check 8 (source tree) is environment-dependent by construction: it reads
    # the real prod checkout, which a tmp-patched `ava_home` resolves to a
    # non-git path. Every pass-all fixture stubs it; the source-tree tests
    # below stub it themselves with specific outcomes.
    monkeypatch.setattr(_cluster_health, "_source_tree_failure", lambda: None)


@pytest.fixture
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # run_health_probe resolves the counter dir via shared.paths.ava_home.
    import shared.paths

    monkeypatch.setattr(shared.paths, "ava_home", lambda: tmp_path)
    # Check 8 (source tree) derives its checkout from ava_home() and, on a
    # runner with no prod tree, resolves it to a non-git path — the guard
    # (correctly) reports that as "guard skipped" and fails the probe. Unit
    # tests have no prod tree by construction, so stub the lookup itself;
    # check 8's own behavior is pinned by the dedicated tests below.
    import shared.cluster_drift

    monkeypatch.setattr(shared.cluster_drift, "prod_source_dir", lambda: None)
    return tmp_path


@pytest.fixture(autouse=True)
def _sent_alerts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture edge-alert summaries; autouse so no test hits a real gateway.

    The probe's edge alerts now flow through `_ingest_alert` (W16); the
    captured value is the stamped summary the ingest payload would carry, so
    assertions on wording keep working. `_notify_owner` is NOT stubbed here —
    its own unit tests below reach the real send path via `_REAL_NOTIFY_OWNER`,
    and the fallback tests stub it explicitly where they need to."""
    sent: list[str] = []

    def _capture(*, status: str, message: str, starts_at: object, severity: str = "error") -> None:
        sent.append(_cluster_health._alert_summary(recovered=status == "resolved", message=message))

    monkeypatch.setattr(_health_alerts, "_ingest_alert", _capture)
    return sent


@pytest.fixture(autouse=True)
def _no_deploy_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """An idle cluster, so these tests keep testing the COUNTER.

    The auto-rollback path now asks whether a deploy is in flight before counting
    a failure (`ops.deploy_window`) — a real question that shells out to git and
    dials every machine. Pinning it idle here keeps this file about the counter
    arithmetic; the suppression behaviour itself is covered in
    `tests/cli/test_deploy_mutex.py`."""
    from ops.deploy_window import DeployWindow

    monkeypatch.setattr(
        "ops.deploy_window.deploy_in_flight",
        lambda **_k: DeployWindow(active=False, detail="no deploy in flight"),  # pyright: ignore[reportUnknownArgumentType]
    )


def test_healthy_run_resets_counter(_all_checks_pass: None, _home: Path) -> None:
    (_home / _cluster_health.FAILURE_COUNT_FILE).write_text(_count_record(2))
    rc = _cluster_health.run_health_probe(auto_rollback=True, threshold=3)
    assert rc == 0
    assert _read_count(_home).splitlines()[0] == "0"


def test_failure_below_threshold_no_rollback(_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: False)
    called: list[list[str]] = []
    monkeypatch.setattr(
        _health_alerts.subprocess,
        "run",
        lambda cmd, **_kw: called.append(cmd),  # type: ignore[arg-type]
    )
    rc = _cluster_health.run_health_probe(auto_rollback=True, threshold=3)
    assert rc == 1
    assert _read_count(_home).splitlines()[0] == "1"
    assert called == []  # threshold not reached -> rollback not invoked


def test_failure_at_threshold_triggers_rollback_and_resets(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (_home / _cluster_health.FAILURE_COUNT_FILE).write_text(_count_record(2))
    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: False)
    called: list[list[str]] = []

    def _fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[bytes]:
        called.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(_health_alerts.subprocess, "run", _fake_run)
    rc = _cluster_health.run_health_probe(auto_rollback=True, threshold=3)
    assert rc == 1
    # Rollback invoked with the same ava that runs the probe (sys.argv[0]).
    assert called and called[0][1:] == ["cluster", "rollback", "--yes"]
    # Successful rollback resets the counter.
    assert _read_count(_home).splitlines()[0] == "0"
    # The emitted flag must be one `ava cluster rollback` actually accepts —
    # a stale flag name here would exit 2 (argparse "unrecognized arguments")
    # on every real auto-rollback run without a unit test ever catching it.
    from cli.main import _build_parser

    _build_parser().parse_args(called[0][1:])


def test_failed_rollback_keeps_counter(_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (_home / _cluster_health.FAILURE_COUNT_FILE).write_text(_count_record(2))
    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: False)

    def _fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 1)  # rollback fails

    monkeypatch.setattr(_health_alerts.subprocess, "run", _fake_run)
    rc = _cluster_health.run_health_probe(auto_rollback=True, threshold=3)
    assert rc == 1
    # A failed rollback keeps the count (retried next run), not reset.
    assert _read_count(_home).splitlines()[0] == "3"


def test_no_auto_rollback_leaves_counter_untouched(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: False)
    ran = False

    def _fake_run(*a: object, **kw: object) -> None:
        nonlocal ran
        ran = True

    monkeypatch.setattr(_health_alerts.subprocess, "run", _fake_run)
    rc = _cluster_health.run_health_probe(auto_rollback=False, threshold=3)
    assert rc == 1
    assert not ran
    assert not (_home / _cluster_health.FAILURE_COUNT_FILE).exists()


# ─── per-service check (5) + owner alerts ────────────────────────────────────


def test_service_probe_failure_alerts_without_rollback_counter(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _sent_alerts: list[str],
) -> None:
    """A dead service fails the probe (exit 1) and alerts the owner, but never
    feeds the auto-rollback counter — a dead frontend session is an outage, not
    proof the cluster code is bad, and a rollback would not necessarily fix it."""
    monkeypatch.setattr(_cluster_health, "_service_probes", lambda: ["ava-main-frontend"])
    _write_aged_alert_state(_home, "FAIL: service probe — not healthy: ava-main-frontend")

    rc = _cluster_health.run_health_probe(auto_rollback=True, threshold=3)

    assert rc == 1
    # The counter is reset (checks 1-4 all passed), never advanced.
    assert _read_count(_home).splitlines()[0] == "0"
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
    monkeypatch.setattr(_cluster_health, "_service_probes", lambda: ["ava-main-frontend"])
    _write_aged_alert_state(
        _home,
        "FAIL: service probe — not healthy: ava-main-frontend",
        age=timedelta(minutes=11),
    )

    assert _cluster_health.run_health_probe() == 1
    assert _sent_alerts == []


def test_disk_over_watermark_fails_and_alerts(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _sent_alerts: list[str],
) -> None:
    """A data volume over the 90% watermark fails the probe (exit 1) and
    alerts the owner, but never feeds the auto-rollback counter — rolling
    back code frees no disk space (the 2026-08-08 outage class: checkpoint
    growth filled the disk and the gateway could not start)."""
    monkeypatch.setattr(
        _cluster_health, "_disk_usage_failure", lambda: "data volume 92.4% used (watermark 90%)"
    )
    _write_aged_alert_state(
        _home,
        "FAIL: disk usage — data volume 92.4% used (watermark 90%)",
    )

    rc = _cluster_health.run_health_probe(auto_rollback=True, threshold=3)

    assert rc == 1
    assert _read_count(_home).splitlines()[0] == "0"  # alert-only: counter reset, never advanced
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
        _cluster_health, "_disk_usage_failure", lambda: "data volume 92.4% used (watermark 90%)"
    )
    _write_aged_alert_state(_home, message, age=timedelta(minutes=11))

    assert _cluster_health.run_health_probe() == 1
    assert len(_sent_alerts) == 1
    assert "disk usage" in _sent_alerts[0]


def test_disk_under_watermark_passes(_all_checks_pass: None, _home: Path) -> None:
    """Healthy disk usage keeps the probe green (no alert, exit 0)."""
    rc = _cluster_health.run_health_probe(auto_rollback=True, threshold=3)
    assert rc == 0


def test_disk_usage_fraction_parses_df_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """The df-style fraction is used/(used+free) — the same Capacity column
    operators read, not the APFS container share statvfs reports."""

    def _fake_df(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                "/dev/disk3s5 239965624 180572268 37021744 83% /System/Volumes/Data\n"
            ),
        )

    monkeypatch.setattr(_cluster_health.subprocess, "run", _fake_df)
    frac = _cluster_health._disk_usage_fraction()
    assert frac is not None
    assert abs(frac - 180572268 / (180572268 + 37021744)) < 1e-9


def test_disk_usage_fraction_unparsable_is_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken df measurement must not synthesize a disk-full alarm."""

    def _raise_df(*a: object, **kw: object) -> subprocess.CompletedProcess[str]:
        raise OSError("df missing")

    monkeypatch.setattr(_cluster_health.subprocess, "run", _raise_df)
    assert _cluster_health._disk_usage_fraction() is None
    assert _cluster_health._disk_usage_failure() is None


def test_disk_usage_failure_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exactly at the watermark is healthy; strictly over it alerts."""
    monkeypatch.setattr(_cluster_health, "_disk_usage_fraction", lambda: 0.90)
    assert _cluster_health._disk_usage_failure() is None
    monkeypatch.setattr(_cluster_health, "_disk_usage_fraction", lambda: 0.9001)
    assert _cluster_health._disk_usage_failure() is not None


def test_source_tree_failure_alerts_without_rollback_counter(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _sent_alerts: list[str],
) -> None:
    """A tampered prod source tree fails the probe (exit 1) and alerts the
    owner, but never feeds the auto-rollback counter — rolling back code does
    not undo an on-disk edit (the 2026-08-28 outage class: edited source broke
    `import ava` for every agent on the box)."""
    message = "prod source tree tampered: untracked outside whitelist: junk.txt"
    monkeypatch.setattr(_cluster_health, "_source_tree_failure", lambda: message)
    _write_aged_alert_state(_home, f"FAIL: source tree — {message}")

    rc = _cluster_health.run_health_probe(auto_rollback=True, threshold=3)

    assert rc == 1
    assert _read_count(_home).splitlines()[0] == "0"  # alert-only: counter reset, never advanced
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
    monkeypatch.setattr(_cluster_health, "_source_tree_failure", lambda: None)

    rc = _cluster_health.run_health_probe()

    assert rc == 0


def test_source_tree_guard_skipped_is_a_distinct_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blind guard must not look like a clean tree: when
    ``source_tree_violations`` reports the guard as skipped, the probe names
    the failure 'guard skipped' (with the reason), never 'tampered'."""
    import shared.cluster_drift
    import shared.source_tree_guard

    def _violations_skipped(_repo: Path) -> tuple[str, ...]:
        return ("guard skipped: git unavailable",)

    monkeypatch.setattr(shared.cluster_drift, "prod_source_dir", lambda: Path("/nonexistent"))
    monkeypatch.setattr(shared.source_tree_guard, "source_tree_violations", _violations_skipped)

    failure = _cluster_health._source_tree_failure()

    assert failure == "prod source tree guard skipped: git unavailable"


def test_service_probe_failure_resets_stale_counter_before_alerting(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A check-5-only failure must not freeze a stale count: gating checks 1-4
    all passed, so any failures before this run are not adjacent to the next
    gating failure. Left unfixed, run1+run2 gating failures, a run3 that is
    healthy in every gating dimension but fails check 5, and a run4 gating
    failure would reach the threshold of 3 and roll production back on the
    strength of non-consecutive evidence — the exact 'Consecutive has to mean
    consecutive' reasoning the deploy suppression already applies."""
    (_home / _cluster_health.FAILURE_COUNT_FILE).write_text("2")
    monkeypatch.setattr(_cluster_health, "_service_probes", lambda: ["ava-main-frontend"])

    rc = _cluster_health.run_health_probe(auto_rollback=True, threshold=3)

    assert rc == 1
    assert _read_count(_home).splitlines()[0] == "0"
    # The next gating failure starts the count fresh: 1, not 3.
    monkeypatch.setattr(_cluster_health, "_service_probes", list)
    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: False)
    assert _cluster_health.run_health_probe(auto_rollback=True, threshold=3) == 1
    assert _read_count(_home).splitlines()[0] == "1"


def test_alert_edge_triggered_once_per_outage(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _sent_alerts: list[str],
) -> None:
    """The probe fires every few minutes; a persistent outage must alert once on
    the first graded transition and once on recovery — not once per run."""
    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: False)
    _write_aged_alert_state(
        _home, "FAIL: gateway liveness — health endpoint unreachable or non-200"
    )
    assert _cluster_health.run_health_probe() == 1
    assert _cluster_health.run_health_probe() == 1
    assert len(_sent_alerts) == 1
    assert "unhealthy" in _sent_alerts[0]

    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: True)
    assert _cluster_health.run_health_probe() == 0
    assert len(_sent_alerts) == 2
    assert "recovered" in _sent_alerts[1]
    assert not (_home / _cluster_health.ALERT_STATE_FILE).exists()
    # Healthy again — no further alert.
    assert _cluster_health.run_health_probe() == 0
    assert len(_sent_alerts) == 2


def test_alert_re_fires_when_failure_reason_changes(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _sent_alerts: list[str],
) -> None:
    """A changed failure reason starts a fresh episode and grades independently."""
    monkeypatch.setattr(_cluster_health, "_service_probes", lambda: ["ava-main-frontend"])
    service_message = "FAIL: service probe — not healthy: ava-main-frontend"
    _write_aged_alert_state(_home, service_message)
    assert _cluster_health.run_health_probe() == 1
    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: False)
    assert _cluster_health.run_health_probe() == 1

    assert len(_sent_alerts) == 1
    assert "service probe" in _sent_alerts[0]
    gateway_message = "FAIL: gateway liveness — health endpoint unreachable or non-200"
    _write_aged_alert_state(_home, gateway_message)
    assert _cluster_health.run_health_probe() == 1
    assert len(_sent_alerts) == 2
    assert "gateway liveness" in _sent_alerts[1]


def test_service_probes_skips_gated_and_probeless_specs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_service_probes probes only role-wanted, non-gated specs and collects
    the sessions whose probe is False (None = no probe available, skipped)."""
    import cli.commands as _ns
    from cli.commands import ServiceSpec
    from ops.service_spec import (
        _GATEWAY,  # typed frozenset[MachineRole]; value irrelevant (roster stubbed)
    )

    # requires_db is irrelevant here (no watchdog round involved), so it is uniform.
    def _spec(session: str) -> ServiceSpec:
        return ServiceSpec(session=session, cmd="x", capabilities=_GATEWAY, requires_db=True)

    dead = _spec("frontend")
    alive = _spec("gateway")
    gated = _spec("browser")
    probeless = _spec("browser-mcp")

    monkeypatch.setattr(_ns, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(
        _ns,
        "_services_for_roles_annotated",
        lambda _roles: (  # pyright: ignore[reportUnknownArgumentType]
            (dead, None),
            (alive, None),
            (gated, "disabled (AVA_BROWSER_ENABLED off)"),
            (probeless, None),
        ),
    )

    def _probe(spec: ServiceSpec) -> _ns.ServiceProbe:
        alive = {"frontend": False, "gateway": True, "browser-mcp": None}[spec.session]
        return _ns.ServiceProbe(alive, "probe", "")

    monkeypatch.setattr(_ns, "_probe_service", _probe)

    assert _cluster_health._service_probes() == ["frontend"]


def test_service_probes_skips_gated_otel_collector_on_non_lgtm_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli.commands as _ns

    tmp_home = tmp_path / "gateway"
    tmp_home.mkdir()
    recorded_sessions: list[str] = []

    monkeypatch.delitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(_ns, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("ops.spec.gateway_observability_home", lambda: tmp_home)

    def _record_probe(spec: _ns.ServiceSpec) -> _ns.ServiceProbe:
        recorded_sessions.append(spec.session)
        return _ns.ServiceProbe(True, "probe", "")

    monkeypatch.setattr(_ns, "_probe_service", _record_probe)

    assert _cluster_health._service_probes() == []
    assert "otel-collector" not in recorded_sessions


def test_service_probes_checks_otel_collector_on_non_lgtm_gateway_with_explicit_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli.commands as _ns

    tmp_home = tmp_path / "gateway"
    tmp_home.mkdir()
    recorded_sessions: list[str] = []

    monkeypatch.setitem(os.environ, "AVA_TELEMETRY_OTLP_ENDPOINT", "http://collector.invalid:4318")
    monkeypatch.setattr(_ns, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("ops.spec.gateway_observability_home", lambda: tmp_home)

    def _record_probe(spec: _ns.ServiceSpec) -> _ns.ServiceProbe:
        recorded_sessions.append(spec.session)
        return _ns.ServiceProbe(True, "probe", "")

    monkeypatch.setattr(_ns, "_probe_service", _record_probe)

    assert _cluster_health._service_probes() == []
    assert "otel-collector" in recorded_sessions


def test_service_probes_checks_otel_collector_on_lgtm_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli.commands as _ns

    tmp_home = tmp_path / "gateway"
    tmp_home.mkdir()
    (tmp_home / "lgtm-host").touch()
    recorded_sessions: list[str] = []

    monkeypatch.setattr(_ns, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("ops.spec.gateway_observability_home", lambda: tmp_home)

    def _record_probe(spec: _ns.ServiceSpec) -> _ns.ServiceProbe:
        recorded_sessions.append(spec.session)
        return _ns.ServiceProbe(True, "probe", "")

    monkeypatch.setattr(_ns, "_probe_service", _record_probe)

    assert _cluster_health._service_probes() == []
    assert "otel-collector" in recorded_sessions


def test_service_probes_carry_the_failing_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner's alert is the only thing a human sees, so it has to say WHICH
    fact failed: "answering, but its home is /home/ava/.ava" is another unit on
    this unit's port — a different incident from "nothing is listening", and one
    no amount of waiting fixes."""
    import cli.commands as _ns
    from cli.commands import ServiceSpec
    from ops.service_spec import _GATEWAY

    spec = ServiceSpec(session="ops", cmd="x", capabilities=_GATEWAY, requires_db=True)
    monkeypatch.setattr(_ns, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(_ns, "_services_for_roles_annotated", lambda _roles: ((spec, None),))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _ns,
        "_probe_service",
        lambda _spec: _ns.ServiceProbe(False, "identity", "home='/home/ava/.ava' != '/u/.ava'"),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _cluster_health._service_probes() == ["ops (home='/home/ava/.ava' != '/u/.ava')"]


def test_service_probes_no_roles_probes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Role not resolvable (setup unfinished) -> no roster to probe; the
    core checks are the fallback signals."""
    import cli.commands as _ns

    monkeypatch.setattr(_ns, "_roles_or_none", lambda: None)
    assert _cluster_health._service_probes() == []


# ─── cluster-stamped outbound alerts ─────────────────────────────────────────


def test_notify_owner_stamps_home_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every ops alert carries the cluster name so the owner can tell which
    cluster is talking — a preview cluster's alert must not read like a prod
    incident. Stamping in the single send point covers every alert uniformly.

    Calls the real `_notify_owner` (the autouse `_sent_alerts` fixture stubs the
    module attribute, so the captured `_REAL_NOTIFY_OWNER` is used to reach the
    actual send path). It POSTs to the im_bridge daemon's health-port `/send`
    RPC — stub `httpx.post` to capture the request."""
    from pathlib import Path

    import httpx

    import shared.cluster
    from shared.config import settings
    from shared.daemon_health import health_port

    monkeypatch.setattr(settings.alerts, "im_notify_enabled", True)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "test-secret")
    monkeypatch.setattr(shared.cluster, "home_label", lambda _home: ".ava-preview-42")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.paths.ava_home", lambda: Path("/x/.ava-preview-42"))

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
    assert url == f"http://127.0.0.1:{health_port('im_bridge')}/send"
    assert headers["Authorization"] == "Bearer test-secret"
    assert payload == {
        "text": "[.ava-preview-42] [health-probe] cluster unhealthy: FAIL: schema health"
    }


def test_notify_owner_failed_send_does_not_leak_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed send must not write the cluster secret to the log. The secret
    rides in the Authorization header; httpx embeds the request (but never its
    headers) in the exception repr, so `_notify_owner` must never format the
    exception itself."""
    from pathlib import Path

    import httpx

    import shared.cluster
    from shared.config import settings

    secret = "SUPERSECRET"  # noqa: S105 — test fixture
    monkeypatch.setattr(settings.alerts, "im_notify_enabled", True)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", secret)
    monkeypatch.setattr(shared.cluster, "home_label", lambda _home: ".ava-main")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.paths.ava_home", lambda: Path("/x/.ava-main"))

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
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The alert is a side channel: when the im_bridge daemon is down the probe
    must still complete — no raise, just a stderr note naming the failure
    class. A dead bridge must never break the probe or the auto-rollback path
    it gates."""
    from pathlib import Path

    import httpx

    import shared.cluster
    from shared.config import settings

    monkeypatch.setattr(settings.alerts, "im_notify_enabled", True)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "test-secret")
    monkeypatch.setattr(shared.cluster, "home_label", lambda _home: ".ava-main")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.paths.ava_home", lambda: Path("/x/.ava-main"))

    def _post(*_a: object, **_k: object) -> None:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", _post)

    _REAL_NOTIFY_OWNER("[health-probe] cluster unhealthy: FAIL")  # never raises

    assert "delivery failed: ConnectError" in capsys.readouterr().err


def test_notify_owner_skips_when_im_notify_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`AVA_OPS_ALERTS_IM_NOTIFY_ENABLED=false` silences the probe's owner
    alerts too — one master switch for every IM notification (the same flag
    the gateway's ops-alerts ingest honours). No HTTP call is made."""
    from pathlib import Path

    import httpx

    import shared.cluster
    from shared.config import settings

    monkeypatch.setattr(settings.alerts, "im_notify_enabled", False)
    monkeypatch.setattr(shared.cluster, "home_label", lambda _home: ".ava-main")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.paths.ava_home", lambda: Path("/x/.ava-main"))

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
) -> None:
    """`AVA_IM_BRIDGE_HEALTH_URL` (a remote host's bridge) is honoured when
    set; the loopback health port is only the fallback."""
    from pathlib import Path

    import httpx

    import shared.cluster
    from shared.config import settings

    monkeypatch.setattr(settings.alerts, "im_notify_enabled", True)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "test-secret")
    monkeypatch.setattr(settings.services, "im_bridge_health_url", "http://10.0.0.5:9111/")
    monkeypatch.setattr(shared.cluster, "home_label", lambda _home: ".ava-main")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.paths.ava_home", lambda: Path("/x/.ava-main"))

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


def _gate(**kw: object) -> object:
    import cli.commands._converge_gate as cg

    fields: dict[str, object] = {
        "entry_port": 3000,
        "app_port": 3001,
        "serving": True,
        "supervised": True,
        "supervisor": "launchd job com.ava.gate.x",
    }
    fields.update(kw)
    return cg.GateStatus(**fields)  # type: ignore[arg-type]


def test_gate_probe_is_silent_on_a_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pure agent-runner owns no entry port, so it has nothing to report."""
    import cli.commands as _ns

    monkeypatch.setattr(_ns, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    assert _cluster_health._gate_probe() is None


def test_gate_probe_reports_a_dark_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    import cli.commands as _ns
    import cli.commands._converge_gate as cg

    monkeypatch.setattr(_ns, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(cg, "probe_gate", lambda *_a: _gate(serving=False))  # pyright: ignore[reportUnknownArgumentType]
    failure = _cluster_health._gate_probe()
    assert failure is not None
    assert "not answering" in failure


def test_gate_probe_reports_a_serving_but_unsupervised_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serving now, but nothing left to restart it — invisible to a user and to
    every other probe, which is why it is worth a line of its own."""
    import cli.commands as _ns
    import cli.commands._converge_gate as cg

    monkeypatch.setattr(_ns, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(cg, "probe_gate", lambda *_a: _gate(supervised=False))  # pyright: ignore[reportUnknownArgumentType]
    failure = _cluster_health._gate_probe()
    assert failure is not None
    assert "unsupervised" in failure


def test_dark_gate_fails_the_probe_without_arming_rollback(
    monkeypatch: pytest.MonkeyPatch, _all_checks_pass: None, _home: Path, _sent_alerts: list[str]
) -> None:
    """The boundary this check was placed for: a dark entry port alerts the owner and
    exits 1, but never advances the auto-rollback counter. The 2026-08-01 cause was a
    converge step that failed to reinstall the launchd job — rolling the cluster's
    code back would re-run that step identically, so rollback is not the remedy."""
    monkeypatch.setattr(
        _cluster_health, "_gate_probe", lambda: "gate entry :3000 not answering (dark)"
    )
    _write_aged_alert_state(
        _home,
        "FAIL: service probe — not healthy: gate entry :3000 not answering (dark)",
    )
    assert _cluster_health.run_health_probe(auto_rollback=True, threshold=1) == 1
    assert any("not answering" in a for a in _sent_alerts)
    # Counter reset (all gating checks passed), never advanced.
    assert _read_count(_home).splitlines()[0] == "0"


# ── the Redis bridge rides in check 5 (alert-only) ───────────────────────────


def _redis_bridge(**kw: object) -> object:
    import cli.commands._converge_redis_bridge as bridge

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
    import cli.commands as _ns
    import cli.commands._converge_redis_bridge as bridge

    monkeypatch.setattr(_ns, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(
        bridge,
        "probe_redis_bridge",
        lambda *_a: _redis_bridge(serving=False, detail="connection refused"),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )

    failure = _cluster_health._redis_bridge_probe()

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
    monkeypatch.setattr(_cluster_health, "_redis_bridge_probe", lambda: failure)
    _write_aged_alert_state(_home, f"FAIL: service probe — not healthy: {failure}")

    assert _cluster_health.run_health_probe(auto_rollback=True, threshold=1) == 1
    assert any("Redis bridge" in alert for alert in _sent_alerts)
    assert _read_count(_home).splitlines()[0] == "0"


# ── crash-loop detection: category=audit only (W9 fix) ──────────────────────


def test_crash_loop_counts_audit_resurrect_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_crash_loop_detection` must count category='audit' resurrect rows only
    (W9 A.25): a telemetry/log resurrect row must not trigger a crash-loop
    alert. Reads the Loki event stream since the LGTM cutover (task #1197)."""

    import httpx

    from cli.commands._cluster_health import _crash_loop_detection

    def _fake_get(url: str, **kw: Any) -> httpx.Response:
        # The LogQL query filters category=audit server-side; the fake answer
        # holds only audit resurrect rows (telemetry/log rows never reach it).
        resp = httpx.Response(
            200, json={"data": {"result": [{"metric": {"agent_id": "1"}, "value": [0, "3"]}]}}
        )
        resp.request = httpx.Request("GET", url)  # type: ignore[attr-defined]
        return resp

    monkeypatch.setattr(httpx, "get", _fake_get)
    # 3 audit resurrects in the window exceed the threshold -> unhealthy.
    assert _crash_loop_detection(max_restarts=2, window_minutes=60) is False

    # At the threshold it is still healthy.
    def _fake_get_two(url: str, **kw: Any) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {"result": [{"metric": {"agent_id": "1"}, "value": [0, "2"]}]}}
        )

    monkeypatch.setattr(httpx, "get", _fake_get_two)
    assert _crash_loop_detection(max_restarts=2, window_minutes=60) is True


def test_crash_loop_merges_disjoint_cutover_eras(monkeypatch: pytest.MonkeyPatch) -> None:
    """A straddling health window adds per-agent counts without reading a
    promoted boundary row through both selectors."""
    import httpx

    from shared.loki_index_labels import LokiReadEra, LokiReadSlice

    start = datetime(2026, 8, 10, tzinfo=UTC)
    cutover = start.replace(hour=1)
    end = start.replace(hour=2)
    slices = (
        LokiReadSlice(LokiReadEra.LEGACY, start, cutover),
        LokiReadSlice(LokiReadEra.INDEXED, cutover, end),
    )
    queries: list[str] = []

    def _fake_get(url: str, **kw: Any) -> httpx.Response:
        queries.append(kw["params"]["query"])
        value = "2" if len(queries) == 1 else "1"
        resp = httpx.Response(
            200, json={"data": {"result": [{"metric": {"agent_id": "1"}, "value": [0, value]}]}}
        )
        resp.request = httpx.Request("GET", url)  # type: ignore[attr-defined]
        return resp

    def _slices(_start: datetime, _end: datetime) -> tuple[LokiReadSlice, ...]:
        return slices

    monkeypatch.setattr(_cluster_health, "split_index_label_window", _slices)
    monkeypatch.setattr(httpx, "get", _fake_get)

    assert _cluster_health._crash_loop_detection(max_restarts=2, window_minutes=60) is False
    assert 'event_name=""' not in queries[0]
    assert 'event_name!=""' in queries[1]
    assert 'event_name="resurrect"' in queries[1]


def test_crash_loop_queries_the_current_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """The instant query evaluates at now, covering exactly (now-window, now]."""
    import httpx

    from shared.loki_index_labels import LokiReadEra, LokiReadSlice

    fixed_now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    captured_windows: list[tuple[datetime, datetime]] = []
    captured_params: list[dict[str, Any]] = []

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: object | None = None) -> datetime:
            assert tz is UTC
            return fixed_now

    def _slices(start: datetime, end: datetime) -> tuple[LokiReadSlice, ...]:
        captured_windows.append((start, end))
        return (LokiReadSlice(LokiReadEra.LEGACY, start, end),)

    def _fake_get(url: str, **kw: Any) -> httpx.Response:
        captured_params.append(kw["params"])
        response = httpx.Response(200, json={"data": {"result": []}})
        response.request = httpx.Request("GET", url)  # type: ignore[attr-defined]
        return response

    monkeypatch.setattr(_cluster_health, "datetime", _FixedDatetime)
    monkeypatch.setattr(_cluster_health, "split_index_label_window", _slices)
    monkeypatch.setattr(httpx, "get", _fake_get)

    assert _cluster_health._crash_loop_detection(max_restarts=2, window_minutes=60) is True
    assert captured_windows == [(fixed_now - timedelta(minutes=60), fixed_now)]
    assert captured_params[0]["time"] == fixed_now.timestamp()
    assert "[3600s]" in captured_params[0]["query"]


# ─── edge alerts → alerts ingest (W16) ──────────────────────────────────────


def test_alert_summary_stamps_cluster_and_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every health alert carries the cluster label + the edge wording, so a
    preview cluster's alert never reads like a prod incident."""
    import shared.cluster

    monkeypatch.setattr(shared.cluster, "home_label", lambda _h: ".ava-preview-42")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.paths.ava_home", lambda: Path("/x/.ava-preview-42"))
    assert _cluster_health._alert_summary(recovered=False, message="FAIL: x") == (
        "[.ava-preview-42] [health-probe] cluster unhealthy: FAIL: x"
    )
    assert _cluster_health._alert_summary(recovered=True, message="all checks passing") == (
        "[.ava-preview-42] [health-probe] cluster recovered: all checks passing"
    )


def test_ingest_alert_posts_health_probe_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """The firing edge POSTs an Alertmanager-webhook-shaped payload to the
    gateway ingest with the graded severity and stable instance identity."""
    import httpx

    import shared.machine
    from shared.alerts import fingerprint
    from shared.config import settings

    monkeypatch.setattr(settings.data_plane, "cluster_secret", "test-secret")
    monkeypatch.setattr(shared.machine, "gateway_api_base", lambda: "http://127.0.0.1:8123")
    monkeypatch.setattr(_health_alerts, "_alert_summary", lambda **_: "SUMMARY")  # pyright: ignore[reportUnknownArgumentType]

    sent: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, int]:
            return {"processed": 1, "inserted": 1, "updated": 0, "notified": 1}

    def _post(url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float) -> _Resp:
        sent.append((url, json, headers))
        return _Resp()

    monkeypatch.setattr(httpx, "post", _post)

    starts_at = datetime(2026, 8, 5, 0, 10, tzinfo=UTC)
    _REAL_INGEST_ALERT(
        status="firing",
        message="FAIL: gateway liveness",
        starts_at=starts_at,
        severity="warning",
    )

    assert len(sent) == 1
    url, payload, headers = sent[0]
    assert url == "http://127.0.0.1:8123/api/alerts"
    assert headers["Authorization"] == "Bearer test-secret"
    assert payload["source"] == "health-probe"
    assert "status" not in payload  # per-alert status, Alertmanager shape
    alert = payload["alerts"][0]
    assert alert["status"] == "firing"
    assert alert["labels"] == {
        "alertname": "cluster health",
        "severity": "warning",
    }
    assert alert["annotations"] == {"summary": "SUMMARY"}
    assert alert["startsAt"] == "2026-08-05T00:10:00+00:00"
    assert alert["endsAt"] == ""
    assert alert["fingerprint"] == fingerprint({"alertname": "cluster health"})


def test_ingest_alert_unreachable_gateway_falls_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Gateway down (the alert's most important case) must not lose the alert:
    a transport failure routes to the local ingest fallback, never raises."""
    import httpx

    import shared.machine
    from shared.config import settings

    monkeypatch.setattr(settings.data_plane, "cluster_secret", "s")
    monkeypatch.setattr(shared.machine, "gateway_api_base", lambda: "http://127.0.0.1:8123")
    monkeypatch.setattr(_health_alerts, "_alert_summary", lambda **_: "SUMMARY")  # pyright: ignore[reportUnknownArgumentType]

    def _post(*_a: object, **_k: object) -> None:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", _post)
    calls: list[tuple[object, object, object, object, object]] = []

    def _fallback(**kw: object) -> None:
        calls.append(
            (
                kw["status"],
                kw["message"],
                kw["starts_at"],
                kw["severity"],
                kw["fingerprint"],
            )
        )

    monkeypatch.setattr(_health_alerts, "_ingest_alert_fallback", _fallback)

    starts_at = datetime(2026, 8, 5, 0, 10, tzinfo=UTC)
    _REAL_INGEST_ALERT(
        status="firing",
        message="FAIL",
        starts_at=starts_at,
        fingerprint="pre-convention-fingerprint",
    )
    assert calls == [("firing", "FAIL", starts_at, "error", "pre-convention-fingerprint")]
    assert "falling back to local ingest" in capsys.readouterr().err


def test_ingest_alert_http_error_falls_back_without_leaking_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-2xx ingest response (e.g. 401) also routes to the fallback; the
    status code + body are logged, never the exception or the secret."""
    import httpx

    import shared.machine
    from shared.config import settings

    secret = "SUPERSECRET"  # noqa: S105 — test fixture
    monkeypatch.setattr(settings.data_plane, "cluster_secret", secret)
    monkeypatch.setattr(shared.machine, "gateway_api_base", lambda: "http://127.0.0.1:8123")
    monkeypatch.setattr(_health_alerts, "_alert_summary", lambda **_: "SUMMARY")  # pyright: ignore[reportUnknownArgumentType]

    req = httpx.Request("POST", "http://127.0.0.1:8123/api/alerts")
    resp = httpx.Response(401, request=req, json={"detail": "unauthorized webhook caller"})
    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: resp)  # pyright: ignore[reportUnknownArgumentType]
    called: list[object] = []
    monkeypatch.setattr(_health_alerts, "_ingest_alert_fallback", lambda **kw: called.append(kw))  # pyright: ignore[reportUnknownArgumentType]

    _REAL_INGEST_ALERT(status="firing", message="FAIL", starts_at=datetime(2026, 8, 5, tzinfo=UTC))
    assert len(called) == 1
    err = capsys.readouterr().err
    assert "401" in err
    assert secret not in err


class _FakeCur:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row

    def __enter__(self) -> _FakeCur:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def execute(self, *a: object) -> None:
        pass

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _FakeConn:
    def __init__(self, notified_row: tuple[object, ...] | None) -> None:
        self._notified_row = notified_row
        self.committed = False

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def cursor(self) -> _FakeCur:
        return _FakeCur(self._notified_row)

    def commit(self) -> None:
        self.committed = True


def test_ingest_alert_fallback_persists_and_notifies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway down but DB up: the fallback upserts the row and sends the IM
    itself (same ingest code), stamps notified_at, commits."""
    import shared.db

    conn = _FakeConn(notified_row=None)
    monkeypatch.setattr(shared.db, "connect", lambda **_: conn)  # pyright: ignore[reportUnknownArgumentType]
    key = ("health-probe", datetime(2026, 8, 5, 0, 10, tzinfo=UTC))
    upserted: list[tuple[Any, str]] = []
    notified: list[str] = []
    stamped: list[object] = []
    monkeypatch.setattr(
        "shared.alerts.upsert_alert",
        lambda _c, a, source="grafana": (
            upserted.append((a, source))  # pyright: ignore[reportUnknownArgumentType]
            or (key, True, True, {"notified_at": None})
        ),
    )
    monkeypatch.setattr("shared.alerts.notify_im", lambda t: notified.append(t) or True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.alerts.stamp_notified", lambda _c, keys: stamped.append(keys))  # pyright: ignore[reportUnknownArgumentType]

    _cluster_health._ingest_alert_fallback(
        status="firing",
        message="FAIL: x",
        starts_at=key[1],
        severity="warning",
        fingerprint="pre-convention-fingerprint",
    )

    assert len(upserted) == 1
    alert, source = upserted[0]
    assert source == "health-probe"
    assert alert["status"] == "firing"
    assert alert["labels"]["severity"] == "warning"
    assert alert["fingerprint"] == "pre-convention-fingerprint"
    assert alert["starts_at"] == key[1].isoformat()  # instance key survives
    assert len(notified) == 1
    assert "cluster health" in notified[0]
    assert stamped == [[key]]
    assert conn.committed


def test_ingest_alert_fallback_skips_im_when_already_notified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gateway that processed the POST but lost the response must not cause
    a second IM: the row's notified_at is set -> the fallback stays silent."""
    import shared.db

    conn = _FakeConn(notified_row=None)
    monkeypatch.setattr(shared.db, "connect", lambda **_: conn)  # pyright: ignore[reportUnknownArgumentType]
    key = ("health-probe", datetime(2026, 8, 5, 0, 10, tzinfo=UTC))
    monkeypatch.setattr(
        "shared.alerts.upsert_alert",
        lambda *_a, **_k: (key, False, False, {"notified_at": datetime(2026, 8, 5, tzinfo=UTC)}),  # pyright: ignore[reportUnknownArgumentType]
    )
    notified: list[str] = []
    stamped: list[object] = []
    monkeypatch.setattr("shared.alerts.notify_im", lambda t: notified.append(t) or True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.alerts.stamp_notified", lambda _c, keys: stamped.append(keys))  # pyright: ignore[reportUnknownArgumentType]

    _cluster_health._ingest_alert_fallback(status="firing", message="FAIL: x", starts_at=key[1])
    assert notified == []
    assert stamped == []
    assert conn.committed


def test_ingest_alert_fallback_direct_im_when_db_down(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Gateway AND DB down: the fallback degrades to the legacy direct-IM path —
    the owner still hears, even though no row can be persisted."""
    import shared.db

    def _boom(**_: object) -> None:
        raise ConnectionError("pg down")

    monkeypatch.setattr(shared.db, "connect", _boom)
    direct: list[str] = []
    monkeypatch.setattr(_health_alerts, "_notify_owner", direct.append)

    _cluster_health._ingest_alert_fallback(
        status="firing", message="FAIL: x", starts_at=datetime(2026, 8, 5, tzinfo=UTC)
    )
    assert len(direct) == 1
    assert "unhealthy" in direct[0]
    assert "direct IM only" in capsys.readouterr().err


def test_alert_failure_tracks_unfired_episode_in_three_line_state(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.config import settings

    started_at = datetime(2026, 8, 26, tzinfo=UTC)
    _freeze_alert_clock(monkeypatch, started_at)
    monkeypatch.setattr(settings.alerts, "transition_warning_seconds", 180.0)
    monkeypatch.setattr(settings.alerts, "transition_error_seconds", 600.0)
    edges: list[dict[str, object]] = []
    monkeypatch.setattr(_health_alerts, "_ingest_alert", lambda **kw: edges.append(kw))  # pyright: ignore[reportUnknownArgumentType]

    _health_alerts._alert_failure(_home, "FAIL: gateway liveness")

    assert (_home / _cluster_health.ALERT_STATE_FILE).read_text().split("\n") == [
        "FAIL: gateway liveness",
        started_at.isoformat(),
        "",
    ]
    assert edges == []


def test_alert_failure_warns_then_escalates_once(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.config import settings

    started_at = datetime(2026, 8, 26, tzinfo=UTC)
    clock = _freeze_alert_clock(monkeypatch, started_at)
    monkeypatch.setattr(settings.alerts, "transition_warning_seconds", 180.0)
    monkeypatch.setattr(settings.alerts, "transition_error_seconds", 600.0)
    edges: list[dict[str, object]] = []
    monkeypatch.setattr(_health_alerts, "_ingest_alert", lambda **kw: edges.append(kw))  # pyright: ignore[reportUnknownArgumentType]

    _health_alerts._alert_failure(_home, "FAIL: gateway liveness")
    clock[0] = started_at + timedelta(seconds=180)
    _health_alerts._alert_failure(_home, "FAIL: gateway liveness")
    clock[0] = started_at + timedelta(seconds=600)
    _health_alerts._alert_failure(_home, "FAIL: gateway liveness")
    _health_alerts._alert_failure(_home, "FAIL: gateway liveness")

    assert [(edge["severity"], edge["starts_at"]) for edge in edges] == [
        ("warning", started_at),
        ("error", started_at),
    ]
    assert (_home / _cluster_health.ALERT_STATE_FILE).read_text().splitlines()[-1] == "error"


def test_unfired_episode_recovers_without_resolve(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started_at = datetime(2026, 8, 26, tzinfo=UTC)
    (_home / _cluster_health.ALERT_STATE_FILE).write_text(
        f"FAIL: gateway liveness\n{started_at.isoformat()}\n"
    )
    edges: list[dict[str, object]] = []
    monkeypatch.setattr(_health_alerts, "_ingest_alert", lambda **kw: edges.append(kw))  # pyright: ignore[reportUnknownArgumentType]

    _health_alerts._alert_recovery(_home)

    assert edges == []
    assert not (_home / _cluster_health.ALERT_STATE_FILE).exists()


def test_fired_episode_recovery_reuses_start_and_severity(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A current marker with no open row falls back to its stable instance key."""
    started_at = datetime(2026, 8, 26, tzinfo=UTC)
    (_home / _cluster_health.ALERT_STATE_FILE).write_text(
        f"FAIL: gateway liveness\n{started_at.isoformat()}\nwarning"
    )
    edges: list[dict[str, object]] = []
    monkeypatch.setattr(_health_alerts, "_ingest_alert", lambda **kw: edges.append(kw))  # pyright: ignore[reportUnknownArgumentType]

    _health_alerts._alert_recovery(_home)

    assert edges == [
        {
            "status": "resolved",
            "message": "all checks passing",
            "starts_at": started_at,
            "severity": "warning",
        }
    ]


def test_fired_episode_recovery_replays_open_row_fingerprint(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery finds pre-convention rows by identity and replays their key."""
    import shared.db

    marker_start = datetime(2026, 8, 26, tzinfo=UTC)
    row_start = datetime(2026, 8, 5, tzinfo=UTC)
    (_home / _cluster_health.ALERT_STATE_FILE).write_text(
        f"FAIL: gateway liveness\n{marker_start.isoformat()}\nwarning"
    )
    queries: list[tuple[str, tuple[object, ...]]] = []

    class _Cursor:
        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
            queries.append((query, params))

        def fetchall(self) -> list[tuple[str, datetime]]:
            return [("pre-convention-fingerprint", row_start)]

    class _Connection:
        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def cursor(self) -> _Cursor:
            return _Cursor()

    monkeypatch.setattr(shared.db, "connect", _Connection)
    edges: list[dict[str, object]] = []
    monkeypatch.setattr(_health_alerts, "_ingest_alert", lambda **kw: edges.append(kw))  # pyright: ignore[reportUnknownArgumentType]

    _health_alerts._alert_recovery(_home)

    assert len(queries) == 1
    assert "labels->>'alertname' = 'cluster health'" in queries[0][0]
    assert edges == [
        {
            "status": "resolved",
            "message": "all checks passing",
            "starts_at": row_start,
            "severity": "warning",
            "fingerprint": "pre-convention-fingerprint",
        }
    ]


def test_legacy_two_line_recovery_replays_open_row_fingerprint(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-upgrade marker still closes its severity-in-fingerprint row."""
    import shared.db

    marker_start = datetime(2026, 8, 26, tzinfo=UTC)
    row_start = datetime(2026, 8, 5, tzinfo=UTC)
    (_home / _cluster_health.ALERT_STATE_FILE).write_text(
        f"FAIL: old persisted alert\n{marker_start.isoformat()}"
    )
    queries: list[str] = []

    class _Cursor:
        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, query: str) -> None:
            queries.append(query)

        def fetchall(self) -> list[tuple[str, datetime]]:
            return [("pre-convention-fingerprint", row_start)]

    class _Connection:
        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def cursor(self) -> _Cursor:
            return _Cursor()

    monkeypatch.setattr(shared.db, "connect", _Connection)
    edges: list[dict[str, object]] = []
    monkeypatch.setattr(_health_alerts, "_ingest_alert", lambda **kw: edges.append(kw))  # pyright: ignore[reportUnknownArgumentType]

    _health_alerts._alert_recovery(_home)

    assert len(queries) == 1
    assert "labels->>'alertname' = 'cluster health'" in queries[0]
    assert edges == [
        {
            "status": "resolved",
            "message": "all checks passing",
            "starts_at": row_start,
            "severity": "error",
            "fingerprint": "pre-convention-fingerprint",
        }
    ]


def test_deploy_explanation_preserves_episode_start_for_later_grade(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.config import settings

    started_at = datetime(2026, 8, 26, tzinfo=UTC)
    clock = _freeze_alert_clock(monkeypatch, started_at)
    monkeypatch.setattr(settings.alerts, "transition_warning_seconds", 180.0)
    monkeypatch.setattr(settings.alerts, "transition_error_seconds", 600.0)
    edges: list[dict[str, object]] = []
    monkeypatch.setattr(_health_alerts, "_ingest_alert", lambda **kw: edges.append(kw))  # pyright: ignore[reportUnknownArgumentType]

    _health_alerts._alert_failure(_home, "FAIL: gateway liveness", deploy_explains=True)
    clock[0] = started_at + timedelta(seconds=600)
    _health_alerts._alert_failure(_home, "FAIL: gateway liveness", deploy_explains=True)
    assert edges == []

    _health_alerts._alert_failure(_home, "FAIL: gateway liveness", deploy_explains=False)
    assert [(edge["severity"], edge["starts_at"]) for edge in edges] == [("error", started_at)]


def test_alert_failure_state_file_carries_instance_key(
    _all_checks_pass: None, _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state file holds the failure message AND the instance's starts_at —
    the alerts dedup key the recovery edge must replay."""
    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: False)
    _cluster_health.run_health_probe()
    lines = (_home / _cluster_health.ALERT_STATE_FILE).read_text().split("\n")
    assert lines[0].startswith("FAIL: gateway liveness")
    datetime.fromisoformat(lines[1])  # parses -> the instance key
    assert lines[2] == ""


def test_alert_recovery_reuses_the_firing_instance(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recovery edge must reference the exact (fingerprint, starts_at) the
    firing edge created — otherwise the resolved POST would insert a second
    row and the firing one would stay 'unresolved' forever."""
    edges: list[tuple[str, object]] = []
    monkeypatch.setattr(
        _health_alerts,
        "_ingest_alert",
        lambda **kw: edges.append((kw["status"], kw["starts_at"])),  # pyright: ignore[reportUnknownArgumentType]
    )
    _write_aged_alert_state(
        _home, "FAIL: gateway liveness — health endpoint unreachable or non-200"
    )
    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: False)
    assert _cluster_health.run_health_probe() == 1
    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: True)
    assert _cluster_health.run_health_probe() == 0
    assert [e[0] for e in edges] == ["firing", "resolved"]
    assert edges[0][1] == edges[1][1]  # same starts_at -> same alerts row


def test_alert_recovery_pre_w16_state_file_goes_direct(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-W16 state file (message only, no instance key) means no alerts
    row exists — the recovery is IM'd directly, like the firing was back then."""
    (_home / _cluster_health.ALERT_STATE_FILE).write_text("FAIL: old-style")
    direct: list[str] = []
    monkeypatch.setattr(_health_alerts, "_notify_owner", direct.append)
    ingest_calls: list[object] = []
    monkeypatch.setattr(_health_alerts, "_ingest_alert", lambda **kw: ingest_calls.append(kw))  # pyright: ignore[reportUnknownArgumentType]

    assert _cluster_health.run_health_probe() == 0
    assert ingest_calls == []
    assert len(direct) == 1 and "recovered" in direct[0]
    assert not (_home / _cluster_health.ALERT_STATE_FILE).exists()


def test_alert_recovery_legacy_two_line_state_resolves(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy marker with no open row uses the same stable-key fallback."""
    started_at = datetime(2026, 8, 5, tzinfo=UTC)
    (_home / _cluster_health.ALERT_STATE_FILE).write_text(
        f"FAIL: old persisted alert\n{started_at.isoformat()}"
    )
    edges: list[dict[str, object]] = []
    monkeypatch.setattr(_health_alerts, "_ingest_alert", lambda **kw: edges.append(kw))  # pyright: ignore[reportUnknownArgumentType]

    _health_alerts._alert_recovery(_home)

    assert edges == [
        {
            "status": "resolved",
            "message": "all checks passing",
            "starts_at": started_at,
            "severity": "error",
        }
    ]
    assert not (_home / _cluster_health.ALERT_STATE_FILE).exists()


def test_ingest_recovery_self_heals_when_instance_never_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolved POST that the ingest inserts as a fresh row (inserted=1,
    notified=0) means the firing half never landed anywhere — the probe sends
    the recovery note directly so the owner isn't left hanging on a firing
    alert that will never resolve in the panel."""
    import httpx

    import shared.machine
    from shared.config import settings

    monkeypatch.setattr(settings.data_plane, "cluster_secret", "s")
    monkeypatch.setattr(shared.machine, "gateway_api_base", lambda: "http://127.0.0.1:8123")
    monkeypatch.setattr(_health_alerts, "_alert_summary", lambda **_: "SUMMARY")  # pyright: ignore[reportUnknownArgumentType]
    direct: list[str] = []
    monkeypatch.setattr(_health_alerts, "_notify_owner", direct.append)

    class _Resp:
        def __init__(self, body: dict[str, int]) -> None:
            self._body = body

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, int]:
            return self._body

    monkeypatch.setattr(
        httpx,
        "post",
        lambda *_a, **_k: _Resp({"processed": 1, "inserted": 1, "updated": 0, "notified": 0}),  # pyright: ignore[reportUnknownArgumentType]
    )
    _REAL_INGEST_ALERT(
        status="resolved", message="all checks passing", starts_at=datetime(2026, 8, 5, tzinfo=UTC)
    )
    assert len(direct) == 1 and "recovered" in direct[0]

    # Control: a normal resolved (existing row flipped, IM notified) → no direct note.
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *_a, **_k: _Resp({"processed": 1, "inserted": 0, "updated": 1, "notified": 1}),  # pyright: ignore[reportUnknownArgumentType]
    )
    _REAL_INGEST_ALERT(
        status="resolved", message="all checks passing", starts_at=datetime(2026, 8, 5, tzinfo=UTC)
    )
    assert len(direct) == 1


# ── checkout guard: worktree code must not drive the probe (Task #1025) ──────


def test_run_health_probe_refused_from_worktree_checkout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The 2026-08-07 accident: a probe launched from a worktree checkout
    (prod home) misjudged schema health against prod data and auto-rolled-back
    the cluster. The probe now refuses with exit 2 and never runs a check."""
    monkeypatch.setattr("shared.paths.ava_home", lambda: Path("~/.ava").expanduser())
    monkeypatch.setattr(
        "shared.paths.repo_root",
        lambda: Path("~/Ava/.worktrees/ava-2890-r4").expanduser(),
    )

    rc = _cluster_health.run_health_probe(auto_rollback=True, threshold=3)

    assert rc == 2
    err = capsys.readouterr().err
    assert "refused" in err
    assert "worktree" in err


def test_run_health_probe_allowed_from_prod_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prod anchored checkout runs the probe normally (exit 0 healthy)."""
    monkeypatch.setattr("shared.paths.ava_home", lambda: Path("~/.ava").expanduser())
    monkeypatch.setattr(
        "shared.paths.repo_root",
        lambda: Path("~/.ava/source").expanduser(),
    )

    def _ok(*_args: object, **_kwargs: object) -> object:
        return True

    # Healthy path: every check passes. Check 6 (disk usage) reads the real
    # data volume via `_disk_usage_failure` unless stubbed — on a dev box
    # whose disk happens to be over the watermark, an unstubbed check here
    # makes the verdict track the developer's disk rather than the code
    # (issue #76).
    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", _ok)
    monkeypatch.setattr(_cluster_health, "_agent_population", _ok)
    monkeypatch.setattr(_cluster_health, "_crash_loop_detection", _ok)
    monkeypatch.setattr(_cluster_health, "_schema_health", _ok)
    monkeypatch.setattr(_cluster_health, "_service_probes", list)
    monkeypatch.setattr(_cluster_health, "_gate_probe", lambda: None)
    monkeypatch.setattr(_cluster_health, "_redis_bridge_probe", lambda: None)
    monkeypatch.setattr(_cluster_health, "_disk_usage_failure", lambda: None)
    monkeypatch.setattr(_cluster_health, "_editable_install_failure", lambda: None)
    # Check 8 reads the real anchored checkout — whose git state varies by
    # host (the CI runner has no prod tree). Stub it like the other checks;
    # the source-tree behavior is pinned by the dedicated tests below.
    monkeypatch.setattr(_cluster_health, "_source_tree_failure", lambda: None)

    rc = _cluster_health.run_health_probe()

    assert rc == 0


def test_agent_min_defaults_to_settings_when_unset(
    _all_checks_pass: None, _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """agent_min=None resolves to AVA_HEALTH_PROBE_AGENT_MIN — the knob a
    test/QA cluster sets to 0 so its empty agent population never trips the
    check and --auto-rollback never cycles the checkout (2026-08-10 preview)."""
    seen: list[int] = []

    def _fake_population(minimum: int) -> bool:
        seen.append(minimum)
        return True

    from cli.commands import _cluster_health
    from shared.config import settings

    monkeypatch.setattr(_cluster_health, "_agent_population", _fake_population)
    monkeypatch.setattr(settings.daemon, "health_probe_agent_min", 0)

    rc = _cluster_health.run_health_probe(agent_min=None)
    assert rc == 0
    assert seen == [0]


def test_agent_min_explicit_overrides_settings(
    _all_checks_pass: None, _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []

    def _fake_population(minimum: int) -> bool:
        seen.append(minimum)
        return True

    from cli.commands import _cluster_health
    from shared.config import settings

    monkeypatch.setattr(_cluster_health, "_agent_population", _fake_population)
    monkeypatch.setattr(settings.daemon, "health_probe_agent_min", 0)

    rc = _cluster_health.run_health_probe(agent_min=2)
    assert rc == 0
    assert seen == [2]


# ─── editable-install records check (7): read-only venv-pointer probe ────────


@pytest.fixture
def _prod_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the probe at a throwaway prod checkout, never the real venv."""
    import shared.cluster_drift

    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)
    monkeypatch.setattr(shared.cluster_drift, "prod_source_dir", lambda: source_root)
    return source_root


def _write_editable_records(
    source_root: Path,
    *,
    pth_target: str | None = None,
    direct_url: str | None = None,
) -> tuple[Path, Path]:
    """Create a fake prod venv's editable-install records; None = leave absent."""
    site = source_root / ".venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    pth = site / "_editable_impl_ava.pth"
    if pth_target is not None:
        pth.write_text(f"{pth_target}\n")
    record = site / "ava-0.1.5.dist-info" / "direct_url.json"
    if direct_url is not None:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(direct_url)
    return pth, record


def _legal_direct_url(source_root: Path) -> str:
    return json.dumps({"url": source_root.resolve().as_uri(), "dir_info": {"editable": True}})


def test_editable_install_healthy_records_are_silent(_prod_source: Path) -> None:
    """Legal pointers (prod root + editable URL) keep the probe green."""
    _write_editable_records(
        _prod_source,
        pth_target=str(_prod_source.resolve()),
        direct_url=_legal_direct_url(_prod_source),
    )
    assert _cluster_health._editable_install_failure() is None


def test_editable_install_allowlisted_dev_clone_is_silent(_prod_source: Path) -> None:
    """~/Ava is a legal pointer target for the prod venv, mirroring the guard."""
    _write_editable_records(
        _prod_source,
        pth_target=str((Path.home() / "Ava").resolve()),
        direct_url=_legal_direct_url(_prod_source),
    )
    assert _cluster_health._editable_install_failure() is None


def test_editable_install_missing_records_are_silent(_prod_source: Path) -> None:
    """A venv with no editable records has nothing to assert (guard: no-op)."""
    assert _cluster_health._editable_install_failure() is None


def test_editable_install_no_prod_source_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No installed prod checkout → nothing to probe (runner-only hosts)."""
    import shared.cluster_drift

    monkeypatch.setattr(shared.cluster_drift, "prod_source_dir", lambda: None)
    assert _cluster_health._editable_install_failure() is None


def test_editable_install_poisoned_pth_alerts(_prod_source: Path, tmp_path: Path) -> None:
    """A pointer naming a worktree is the poison class — alert, listing the record."""
    worktree = tmp_path / "deleted-worktree"
    _write_editable_records(
        _prod_source,
        pth_target=str(worktree),
        direct_url=_legal_direct_url(_prod_source),
    )
    failure = _cluster_health._editable_install_failure()
    assert failure is not None
    assert "_editable_impl_ava.pth" in failure
    assert str(worktree) in failure
    assert "direct_url" not in failure  # the healthy record is not noise


def test_editable_install_poisoned_direct_url_alerts(_prod_source: Path, tmp_path: Path) -> None:
    """A direct_url naming a worktree is the same poison recorded elsewhere."""
    worktree = tmp_path / "deleted-worktree"
    _write_editable_records(
        _prod_source,
        pth_target=str(_prod_source.resolve()),
        direct_url=json.dumps({"url": worktree.resolve().as_uri(), "dir_info": {"editable": True}}),
    )
    failure = _cluster_health._editable_install_failure()
    assert failure is not None
    assert "direct_url.json" in failure
    assert "deleted-worktree" in failure
    assert "_editable_impl_ava.pth" not in failure


def test_editable_install_both_poisoned_records_are_listed(
    _prod_source: Path, tmp_path: Path
) -> None:
    """The two records drift independently (2026-08-27 QA finding); both report."""
    worktree = tmp_path / "deleted-worktree"
    _write_editable_records(
        _prod_source,
        pth_target=str(worktree),
        direct_url=json.dumps({"url": worktree.resolve().as_uri(), "dir_info": {"editable": True}}),
    )
    failure = _cluster_health._editable_install_failure()
    assert failure is not None
    assert "_editable_impl_ava.pth" in failure and "direct_url.json" in failure


def test_editable_install_unparsable_direct_url_alerts(_prod_source: Path) -> None:
    """A record that cannot be parsed cannot be verified — alert."""
    _write_editable_records(
        _prod_source,
        pth_target=str(_prod_source.resolve()),
        direct_url="{not json",
    )
    failure = _cluster_health._editable_install_failure()
    assert failure is not None and "direct_url.json" in failure


def test_editable_install_non_editable_direct_url_alerts(_prod_source: Path) -> None:
    """A record not marked editable disagrees with the pointer — alert."""
    _write_editable_records(
        _prod_source,
        pth_target=str(_prod_source.resolve()),
        direct_url=json.dumps(
            {"url": _prod_source.resolve().as_uri(), "dir_info": {"editable": False}}
        ),
    )
    failure = _cluster_health._editable_install_failure()
    assert failure is not None and "direct_url.json" in failure


def test_editable_install_empty_pth_alerts(_prod_source: Path) -> None:
    """An empty pointer is not a legal target."""
    _write_editable_records(_prod_source, pth_target="")
    failure = _cluster_health._editable_install_failure()
    assert failure is not None and "(empty)" in failure


def test_editable_install_missing_console_script_alerts(_prod_source: Path) -> None:
    """The read-only probe also catches a half-uninstalled ava launcher."""

    _write_editable_records(
        _prod_source,
        pth_target=str(_prod_source.resolve()),
        direct_url=_legal_direct_url(_prod_source),
    )
    interpreter = _prod_source / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.touch()

    failure = _cluster_health._editable_install_failure()

    assert failure is not None and "editable console script missing" in failure


def test_editable_install_unreadable_record_alerts(_prod_source: Path) -> None:
    """A record that cannot be read cannot be verified — alert, never crash."""
    pth, _record = _write_editable_records(_prod_source, pth_target=str(_prod_source.resolve()))
    pth.unlink()
    pth.mkdir()  # read_text() on a directory raises OSError on every platform
    failure = _cluster_health._editable_install_failure()
    assert failure is not None and "unreadable" in failure


def test_editable_install_failure_alerts_without_rollback_counter(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _sent_alerts: list[str],
) -> None:
    """A poisoned venv record fails the probe (exit 1) and alerts the owner, but
    never feeds the auto-rollback counter — rolling back code does not fix a
    venv record; the converge guard repairs it (the 2026-08-27 outage class)."""
    detail = (
        "prod venv editable install violation: "
        ".../_editable_impl_ava.pth names '.../deleted-worktree'"
    )
    monkeypatch.setattr(_cluster_health, "_editable_install_failure", lambda: detail)
    _write_aged_alert_state(_home, f"FAIL: editable install — {detail}")

    rc = _cluster_health.run_health_probe(auto_rollback=True, threshold=3)

    assert rc == 1
    assert _read_count(_home).splitlines()[0] == "0"  # alert-only: reset, never advanced
    assert len(_sent_alerts) == 1
    assert "editable install" in _sent_alerts[0]


def test_editable_install_deploy_pauses_alert_grade(
    _all_checks_pass: None,
    _home: Path,
    monkeypatch: pytest.MonkeyPatch,
    _sent_alerts: list[str],
) -> None:
    """A live deploy runs the converge guard that repairs the records, so its
    window pauses grading (unlike disk pressure, which no deploy explains)."""
    from ops.deploy_window import DeployWindow

    monkeypatch.setattr(
        "ops.deploy_window.deploy_in_flight",
        lambda **_kw: DeployWindow(active=True, detail="rollout live"),  # pyright: ignore[reportUnknownArgumentType]
    )
    detail = "prod venv editable install names non-allowlisted source: x"
    monkeypatch.setattr(_cluster_health, "_editable_install_failure", lambda: detail)
    _write_aged_alert_state(_home, f"FAIL: editable install — {detail}", age=timedelta(minutes=11))

    assert _cluster_health.run_health_probe() == 1
    assert _sent_alerts == []


def test_editable_install_healthy_records_keep_probe_green(
    _home: Path, _prod_source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: legal records run the real check inside the probe and pass."""
    _write_editable_records(
        _prod_source,
        pth_target=str(_prod_source.resolve()),
        direct_url=_legal_direct_url(_prod_source),
    )
    monkeypatch.setattr(_cluster_health, "_gateway_liveness_with_retry", lambda: True)
    monkeypatch.setattr(_cluster_health, "_agent_population", lambda _min: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cluster_health, "_crash_loop_detection", lambda _m, _w: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cluster_health, "_schema_health", lambda: True)
    monkeypatch.setattr(_cluster_health, "_service_probes", list)
    monkeypatch.setattr(_cluster_health, "_gate_probe", lambda: None)
    monkeypatch.setattr(_cluster_health, "_redis_bridge_probe", lambda: None)
    monkeypatch.setattr(_cluster_health, "_disk_usage_failure", lambda: None)
    # The throwaway `_prod_source` is not a git checkout; check 8 is not this
    # test's subject (the editable-install check is), so stub it rather than
    # depend on the host's real prod tree.
    monkeypatch.setattr(_cluster_health, "_source_tree_failure", lambda: None)

    assert _cluster_health.run_health_probe() == 0
