"""Pre-rollout evidence stays useful when individual observation sources fail."""

from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli.commands import _update_orchestration as orch
from shared.api_contracts.status import MachineStatus

NOW = datetime(2026, 9, 12, 4, 5, 6, tzinfo=UTC)
SHA = "abcdef0123456789"
FILES = {
    "health_probe_failures": f"1\ncode\ngateway unavailable\n{NOW.isoformat()}",
    "health_probe_pending_lkg_passes": f"{SHA}\n1",
    "health_probe_alert": "gateway unavailable\nalert metadata",
}


def machine(**changes: object) -> MachineStatus:
    fields: dict[str, object] = {
        "name": "gateway",
        "serve_gateway": True,
        "serve_agent_runner": True,
        "gateway_url": "http://gateway:8000",
        "up_since_at": NOW,
        "online": True,
        "paused": False,
        "head_sha": SHA,
        "on_pin": True,
    }
    fields.update(changes)
    return MachineStatus.model_validate(fields)


@pytest.fixture
def sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Replace every external read; run the real collectors and parsers."""
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    for name, content in FILES.items():
        (tmp_path / name).write_text(content)
    monkeypatch.setattr("shared.cluster_pin.get_cluster_target_sha", lambda: SHA)
    monkeypatch.setattr("shared.cluster_pin.get_last_known_good_sha", lambda: "1234567890")
    monkeypatch.setattr("shared.cluster_pin.get_pending_known_good", lambda: (SHA, NOW))
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gateway:8000")
    monkeypatch.setattr("shared.machine.gateway_auth_headers", lambda: {"Authorization": "test"})

    def get(url: str, *, timeout: float, headers: dict[str, str]) -> SimpleNamespace:
        assert url == "http://gateway:8000/api/cluster/roster"
        assert timeout == 10.0
        assert headers == {"Authorization": "test"}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: [machine().model_dump()])

    monkeypatch.setattr("shared.http_dial.get", get)
    monkeypatch.setattr(
        "shared.host_deploy_state.read_all",
        lambda: {"gateway": SimpleNamespace(posture="idle")},
    )

    def execute(sql: str) -> SimpleNamespace:
        assert sql == "SELECT pg_database_size(current_database())"
        return SimpleNamespace(fetchone=lambda: (13207024435,))

    def connect(*, autocommit: bool) -> nullcontext[SimpleNamespace]:
        assert autocommit is True
        return nullcontext(SimpleNamespace(execute=execute))

    monkeypatch.setattr("shared.db.connect", connect)
    return tmp_path


def test_collect_happy_path(sources: Path) -> None:
    baseline = orch._collect_health_baseline(target_sha=SHA)
    assert baseline.target_sha == SHA
    assert baseline.captured_at.tzinfo == UTC
    assert baseline.pin == orch._BaselineValue((SHA, "1234567890", (SHA, NOW)))
    assert baseline.machines == orch._BaselineValue([machine()])
    assert baseline.postures == orch._BaselineValue({"gateway": "idle"})
    assert baseline.health_probe.failures == orch._BaselineValue(
        (1, "code", "gateway unavailable", NOW.isoformat())
    )
    assert baseline.health_probe.pending_lkg == orch._BaselineValue((SHA, 1))
    assert baseline.health_probe.alert == orch._BaselineValue("gateway unavailable")
    assert baseline.db_size == orch._BaselineValue(13207024435)


def fail(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("source unavailable")


@pytest.mark.parametrize(
    ("seam", "field"),
    [
        ("shared.cluster_pin.get_cluster_target_sha", "pin"),
        ("shared.cluster_pin.get_last_known_good_sha", "pin"),
        ("shared.cluster_pin.get_pending_known_good", "pin"),
        ("shared.http_dial.get", "machines"),
        ("shared.machine.gateway_api_base", "machines"),
        ("shared.machine.gateway_auth_headers", "machines"),
        ("shared.host_deploy_state.read_all", "postures"),
        ("shared.db.connect", "db_size"),
    ],
)
def test_source_failure_is_local(
    sources: Path, monkeypatch: pytest.MonkeyPatch, seam: str, field: str
) -> None:
    expected = orch._collect_health_baseline(target_sha=SHA)
    monkeypatch.setattr(seam, fail)
    actual = orch._collect_health_baseline(target_sha=SHA)
    assert getattr(actual, field) == orch._BaselineValue(reason="RuntimeError: source unavailable")
    assert (
        replace(actual, captured_at=expected.captured_at, **{field: getattr(expected, field)})
        == expected
    )


@pytest.mark.parametrize("stage", ["http_status", "json", "validation", "db_query", "db_empty"])
def test_response_failures(sources: Path, monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    if stage.startswith("db_"):
        execute = (
            fail if stage == "db_query" else lambda _sql: SimpleNamespace(fetchone=lambda: None)
        )
        monkeypatch.setattr(
            "shared.db.connect",
            lambda **_kw: nullcontext(SimpleNamespace(execute=execute)),  # pyright: ignore[reportUnknownArgumentType]
        )
        field = "db_size"
    else:
        response = SimpleNamespace(
            raise_for_status=fail if stage == "http_status" else lambda: None,
            json=fail if stage == "json" else lambda: [{}],
        )
        monkeypatch.setattr("shared.http_dial.get", lambda *_a, **_kw: response)  # pyright: ignore[reportUnknownArgumentType]
        field = "machines"
    baseline = orch._collect_health_baseline(target_sha=SHA)
    result = getattr(baseline, field)
    assert result.value is None
    assert result.reason
    assert baseline.health_probe.alert.value == "gateway unavailable"
    assert baseline.pin.value is not None


@pytest.mark.parametrize(
    ("name", "field"),
    [
        ("health_probe_failures", "failures"),
        ("health_probe_pending_lkg_passes", "pending_lkg"),
        ("health_probe_alert", "alert"),
    ],
)
@pytest.mark.parametrize("state", ["missing", "malformed", "unreadable"])
def test_file_failure_is_local(sources: Path, name: str, field: str, state: str) -> None:
    expected = orch._collect_health_baseline(target_sha=SHA)
    path = sources / name
    path.unlink()
    if state == "malformed":
        path.write_text("")
    elif state == "unreadable":
        path.mkdir()
    actual = orch._collect_health_baseline(target_sha=SHA)
    result = getattr(actual.health_probe, field)
    assert result.value is None
    assert (result.reason is None) == (state == "missing")
    restored = replace(actual.health_probe, **{field: getattr(expected.health_probe, field)})
    assert restored == expected.health_probe
    assert actual.db_size == expected.db_size


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("health_probe_failures", f"bad\ncode\nreason\n{NOW.isoformat()}"),
        ("health_probe_failures", f"-1\ncode\nreason\n{NOW.isoformat()}"),
        ("health_probe_failures", "1\ncode\nreason\nnot-a-timestamp"),
        ("health_probe_pending_lkg_passes", f"{SHA}\nbad"),
        ("health_probe_pending_lkg_passes", f"{SHA}\n-1"),
        ("health_probe_pending_lkg_passes", "\n1"),
    ],
)
def test_invalid_file_fields(sources: Path, name: str, content: str) -> None:
    (sources / name).write_text(content)
    probe = orch._collect_health_baseline(target_sha=None).health_probe
    result = probe.failures if name.endswith("failures") else probe.pending_lkg
    assert result.value is None
    assert result.reason is not None
    assert result.reason.startswith("ValueError:")


def test_format_complete(sources: Path) -> None:
    baseline = replace(orch._collect_health_baseline(target_sha=SHA), captured_at=NOW)
    assert orch._format_health_baseline(baseline) == [
        "── pre-rollout health baseline (2026-09-12T04:05:06+00:00; target=abcdef0) ──",
        "cluster pin: target=abcdef0 last-known-good=1234567 pending=abcdef0 (at 2026-09-12T04:05:06+00:00)",
        "machines:",
        "  gateway  gateway + agent-runner  posture=idle  on-pin=✓ abcdef0  status=online  paused=no",
        "health-probe (this host): failures=1/3 (code, recorded-at 2026-09-12T04:05:06+00:00, "
        "reason='gateway unavailable'); pending-lkg=1/2 (abcdef0); alert-episode='gateway unavailable'",
        "db: size=12.3 GiB (13207024435 bytes)",
        "── end pre-rollout health baseline ──",
    ]


def test_format_unavailable(sources: Path) -> None:
    missing: orch._BaselineValue[Any] = orch._BaselineValue(reason="RuntimeError: down\nmore")
    baseline = replace(
        orch._collect_health_baseline(target_sha=None),
        pin=missing,
        machines=missing,
        postures=missing,
        db_size=missing,
        health_probe=orch._HealthProbeBaseline(missing, missing, missing),
    )
    lines = orch._format_health_baseline(baseline)
    assert "target=restart-only" in lines[0]
    assert lines[1] == "cluster pin: (unavailable: RuntimeError: down more)"
    assert lines[2] == "machines: (unavailable: RuntimeError: down more)"
    assert lines[3].count("(unavailable: RuntimeError: down more)") == 3
    assert lines[4] == "db: (unavailable: RuntimeError: down more)"
    baseline = replace(baseline, machines=orch._BaselineValue([machine()]))
    assert (
        "posture=(unavailable: RuntimeError: down more)"
        in orch._format_health_baseline(baseline)[3]
    )


def test_format_absent_and_empty(sources: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in FILES:
        (sources / name).unlink()
    for getter in ("get_cluster_target_sha", "get_last_known_good_sha", "get_pending_known_good"):
        monkeypatch.setattr(f"shared.cluster_pin.{getter}", lambda: None)
    monkeypatch.setattr(
        "shared.http_dial.get",
        lambda *_a, **_kw: SimpleNamespace(  # pyright: ignore[reportUnknownArgumentType]
            raise_for_status=lambda: None,
            json=list,
        ),
    )
    baseline = orch._collect_health_baseline(target_sha=None)
    assert baseline.machines.value == []
    assert orch._format_health_baseline(baseline)[1:4] == [
        "cluster pin: target=(none) last-known-good=(none) pending=(none)",
        "machines: (none)",
        "health-probe (this host): failures=(none); pending-lkg=(none); alert-episode=(none)",
    ]


def test_format_machine_states(sources: Path) -> None:
    baseline = replace(
        orch._collect_health_baseline(target_sha=SHA),
        machines=orch._BaselineValue(
            [
                machine(name="z", online=False, paused=None, head_sha=None, on_pin=None),
                machine(
                    name="b",
                    online=False,
                    stopped_at=NOW,
                    paused=True,
                    on_pin=False,
                    is_staging=True,
                ),
                machine(
                    name="a",
                    serve_gateway=False,
                    serve_agent_runner=False,
                    serve_observability_station=True,
                ),
            ]
        ),
        postures=orch._BaselineValue({"a": "idle", "b": "paused"}),
    )
    lines = orch._format_health_baseline(baseline)
    assert "a  observability-station  posture=idle" in lines[3]
    assert "b (staging)" in lines[4]
    assert "posture=paused  on-pin=✗ abcdef0  status=stopped  paused=yes" in lines[4]
    assert "posture=(no row)  on-pin=(unknown)  status=offline  paused=?" in lines[5]


def test_record_prints_real_snapshot(sources: Path, capsys: pytest.CaptureFixture[str]) -> None:
    orch._record_health_baseline(target_sha=SHA)
    output = capsys.readouterr()
    assert "target=abcdef0" in output.out
    assert "db: size=12.3 GiB (13207024435 bytes)" in output.out
    assert output.out.endswith("── end pre-rollout health baseline ──\n")
    assert output.err == ""


@pytest.mark.parametrize("seam", ["_collect_health_baseline", "_format_health_baseline"])
def test_record_unexpected_exception(
    sources: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], seam: str
) -> None:
    monkeypatch.setattr(orch, seam, fail)
    orch._record_health_baseline(target_sha=SHA)
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "⚠ pre-rollout health baseline failed: RuntimeError: source unavailable\n"


def test_home_failure_keeps_other_sources(sources: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.paths.ava_home", fail)
    baseline = orch._collect_health_baseline(target_sha=SHA)
    for result in (
        baseline.health_probe.failures,
        baseline.health_probe.pending_lkg,
        baseline.health_probe.alert,
    ):
        assert result.value is None
        assert result.reason == "RuntimeError: source unavailable"
    assert baseline.pin.value is not None
    assert baseline.db_size.value == 13207024435


def test_reset_failure_file_is_valid(sources: Path) -> None:
    (sources / "health_probe_failures").write_text(f"0\ncode\n\n{NOW.isoformat()}")
    baseline = orch._collect_health_baseline(target_sha=None)
    assert baseline.health_probe.failures == orch._BaselineValue((0, "code", "", NOW.isoformat()))
    assert "failures=0/3" in orch._format_health_baseline(baseline)[4]
