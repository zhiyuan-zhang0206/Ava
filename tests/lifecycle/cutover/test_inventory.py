"""The read-only inventory of a legacy-born home: names only, and exactly what adoption would do."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil
import pytest

from scripts import cutover_inventory as inventory
from tests.lifecycle.cutover.conftest import CANARY, SERVICE_PATH, LegacyHome

Make = Callable[..., LegacyHome]


def _report(
    legacy: LegacyHome, capsys: pytest.CaptureFixture[str], *extra: str
) -> tuple[int, dict[str, Any], str]:
    argv = ["--home", str(legacy.home), "--registry", str(legacy.registry), *extra]
    code = inventory.main(argv, host=legacy.scheduler.host())
    out = capsys.readouterr().out
    return code, json.loads(out), out


_RUNNER_REMOVALS = [
    "AVA_CLUSTER",
    "AVA_DB_ADMIN_PASSWORD",
    "AVA_PITR_BACKUP_KEY_FILE",
    "AVA_PITR_GCS_BUCKET",
    "AVA_REDIS_ADMIN_PASSWORD",
    "AVA_REDIS_PASSWORD",
    "AVA_RESTARTER_HEALTH_PORT",
    "AVA_RUNNER_DB_PASSWORD",
]
_RUNNER_RESIDUE = {
    "backups",
    "physical-backup",
    "redis",
    "pgbouncer",
    "masked-backup-20260920",
    "secrets/gcs-uploader.json",
    "secrets/pg-backup.key",
    "run/bootstrap-snapshot.json",
}


def test_runner_inventory_names_everything_and_prints_no_secret(
    make_legacy: Make, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = make_legacy()
    before = legacy.snapshot()
    code, report, raw = _report(legacy, capsys, "--service-path", SERVICE_PATH)
    assert CANARY not in raw
    assert legacy.snapshot() == before  # read-only
    steps = {step["step"]: step["effects"] for step in report["plan"]}
    observed = {
        "code": code,
        "adoptable": report["adoptable"],
        "refusals": report["refusals"],
        "mode": report["mode"],
        "roles": report["roles"],
        "start_intent": report["start_intent"],
        "has_record": report["registry"]["record"] is not None,
        "remove": report["env"]["remove"],
        "set": report["env"]["set"],
        "residue": set(report["residue"]),
        "pidfiles": {pid["state"] for pid in report["pidfiles"]},
        "pause_owner": report["pause_owner"]["status"],
        "record_ops": [effect["op"] for effect in steps["record"]],
        "intent": steps["intent"],
    }
    assert observed == {
        "code": 0,
        "adoptable": True,
        "refusals": [],
        "mode": "remote-unit",
        "roles": ["agent-runner"],
        "start_intent": None,
        "has_record": True,
        "remove": _RUNNER_REMOVALS,
        "set": ["AVA_SERVICE_PATH"],
        "residue": _RUNNER_RESIDUE,
        "pidfiles": {"stale", "invalid"},
        "pause_owner": "resumed",
        "record_ops": ["record-retire"],
        "intent": [{"op": "intent-write", "phase": "provisioned", "roles": ["agent-runner"]}],
    }
    expected_files = {"installed_sha", "run/sessions", "state/hold-watchdog-attempt"}
    assert expected_files <= set(report["legacy_files"])


def test_inventory_without_a_service_path_explains_the_refusal(
    make_legacy: Make, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = make_legacy()
    code, report, _raw = _report(legacy, capsys)
    assert code == 2 and not report["adoptable"]
    assert report["refusals"] == [
        "AVA_SERVICE_PATH is not declared: pass the reviewed --service-path"
    ]


def test_live_legacy_service_refuses_while_the_kept_helper_is_exempt(
    make_legacy: Make, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = make_legacy()
    service = legacy.spawn("services.agent_host.daemon")
    helper = legacy.spawn("services.permissions_helper.app", cwd=legacy.home / "helper")
    legacy.scheduler.load(f"com.ava.permissions-helper.{legacy.slug}", helper.pid)
    code, report, _raw = _report(legacy, capsys, "--service-path", SERVICE_PATH)
    assert code == 2
    pids = {proc["pid"]: proc for proc in report["processes"]}
    assert pids[service.pid]["kind"] == "ava" and "cwd" in pids[service.pid]["relations"]
    assert helper.pid not in pids
    assert report["refusals"] == [
        f"live home process {service.pid} ({pids[service.pid]['name']}) must stop first"
    ]


def test_a_pidfile_naming_a_live_home_process_refuses(
    make_legacy: Make, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = make_legacy()
    service = legacy.spawn("services.watchdog.daemon")
    (legacy.home / "run" / "watchdog.pid").write_text(f"{service.pid}\n")
    _code, report, _raw = _report(legacy, capsys, "--service-path", SERVICE_PATH)
    assert {"path": "run/watchdog.pid", "pid": service.pid, "state": "live"} in report["pidfiles"]
    assert f"watchdog.pid names live home process {service.pid}" in report["refusals"]


def test_service_path_candidate_comes_from_a_live_service_without_venvs(
    make_legacy: Make, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = make_legacy()
    venv = legacy.checkout / ".venv" / "bin"
    monkeypatch.setenv(
        "PATH", f"{venv}:/opt/homebrew/bin:/usr/bin:/System/Cryptexes/App/usr/bin:/usr/bin:/bin"
    )
    service = legacy.spawn("services.agent_host.daemon")
    _code, report, _raw = _report(legacy, capsys)
    assert report["env"]["service_path_candidate"] == {
        "source_pid": service.pid,
        "value": "/opt/homebrew/bin:/usr/bin:/bin",
    }


def test_attestation_proves_absence_per_recorded_birth(
    make_legacy: Make, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    legacy = make_legacy()
    live = legacy.spawn("services.agent_host.daemon")
    birth = psutil.Process(live.pid).create_time()
    rows = [
        {"machine": "legacy-box", "pid": live.pid, "birth": birth, "agent_id": 1},
        {"machine": "legacy-box", "pid": live.pid, "birth": birth - 1, "agent_id": 2},
        {"machine": "legacy-box", "pid": 12345, "birth": psutil.boot_time() - 60, "agent_id": 3},
        {"machine": "other-box", "pid": live.pid, "birth": birth, "agent_id": 4},
    ]
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(rows))
    code, report, _raw = _report(legacy, capsys, "--attest", str(path))
    assert code == 2 and not report["all_absent"]
    assert [(row["agent_id"], row["verdict"]) for row in report["rows"]] == [
        (1, "alive"),
        (2, "absent"),
        (3, "boot_changed"),
    ]
    assert report["other_machines"] == 1
    live.kill()
    live.wait()
    code, report, _raw = _report(legacy, capsys, "--attest", str(path))
    assert code == 0 and report["all_absent"]


def test_non_canonical_home_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    real = tmp_path.resolve() / "real"
    real.mkdir()
    link = tmp_path.resolve() / "link"
    link.symlink_to(real)
    assert inventory.main(["--home", str(link)]) == 1
    assert "canonical" in capsys.readouterr().err
