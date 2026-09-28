"""The read-only inventory of a legacy-born home: names only, and exactly what adoption would do."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil
import pytest

from scripts import cutover_inventory as inventory
from shared.native_process.ownership import stable_create_time
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
    "AVA_CLUSTER_SECRET",  # a remote unit never holds the human bearer
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


def _gone_pid() -> int:
    pid = 4_000_000
    while psutil.pid_exists(pid):
        pid += 1
    return pid


def test_attestation_proves_absence_per_recorded_birth(
    make_legacy: Make, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    legacy = make_legacy()
    live = legacy.spawn("services.agent_host.daemon")
    birth = stable_create_time(psutil.Process(live.pid))
    rows = [
        {"machine": "legacy-box", "pid": live.pid, "birth": birth, "agent_id": 1},
        # A reading one second off (a clock step, the public macOS correction)
        # is still the recorded process: never proof that it is gone.
        {"machine": "legacy-box", "pid": live.pid, "birth": birth - 1, "agent_id": 2},
        {
            "machine": "legacy-box",
            "pid": live.pid,
            "birth": psutil.boot_time() - 600,
            "agent_id": 3,
        },
        {"machine": "legacy-box", "pid": _gone_pid(), "birth": birth, "agent_id": 4},
        {"machine": "other-box", "pid": live.pid, "birth": birth, "agent_id": 5},
    ]
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(rows))
    code, report, _raw = _report(legacy, capsys, "--attest", str(path))
    assert code == 2 and not report["all_absent"]
    assert [(row["agent_id"], row["verdict"]) for row in report["rows"]] == [
        (1, "alive"),
        (2, "alive"),
        (3, "boot_changed"),
        (4, "absent"),
    ]
    assert report["other_machines"] == 1
    live.kill()
    live.wait()
    code, report, _raw = _report(legacy, capsys, "--attest", str(path))
    assert code == 0 and report["all_absent"]


def test_a_live_survivor_with_a_shifted_birth_never_proves_closure(
    make_legacy: Make, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The review's reproduction: user code the old stop left running (cwd under
    the home, no Ava module in its argv) whose recorded birth reads one second
    off must deny closure, through its row and through the census."""
    legacy = make_legacy()
    survivor = subprocess.Popen(["/bin/sleep", "120"], cwd=legacy.home)
    legacy.children.append(survivor)
    birth = stable_create_time(psutil.Process(survivor.pid)) + 1.0
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([{"machine": "legacy-box", "pid": survivor.pid, "birth": birth}]))
    code, report, raw = _report(legacy, capsys, "--attest", str(rows))
    document = tmp_path / "attestation.json"
    document.write_text(raw)
    attestation, _data = inventory.load_attestation(document)
    assert code == 2 and not attestation.proves_closure
    assert [row.verdict for row in attestation.rows] == ["alive"]
    assert [(p["pid"], p["kind"]) for p in report["processes"]] == [(survivor.pid, "other")]
    assert not report["census_empty"]


@pytest.mark.parametrize(("platform", "verdict"), [("darwin", "absent"), ("linux", "unknown")])
def test_a_reused_pid_is_absent_only_where_the_birth_reading_is_stable(
    make_legacy: Make, platform: str, verdict: str
) -> None:
    """A live pid whose birth reads far from the record, within this boot: the
    macOS kernel start time never moves, so it is another process; a Linux
    reading moves with wall-clock steps, so the record stays unproven."""
    legacy = make_legacy()
    live = legacy.spawn("services.agent_host.daemon")
    birth = stable_create_time(psutil.Process(live.pid)) - 60
    facts = inventory.Facts(legacy.home, legacy.slug, legacy.registry, legacy.checkout, {}, {})
    row = {"machine": "legacy-box", "pid": live.pid, "birth": birth}
    report = inventory.attest([row], "legacy-box", facts, platform=platform)
    assert [row["verdict"] for row in report["rows"]] == [verdict]


def test_attestation_is_one_closure_document_with_the_home_census(
    make_legacy: Make, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A live Ava process of the home that no row records still denies closure;
    the database-records repair accepts only a consistent, closing document."""
    legacy = make_legacy()
    birth = psutil.boot_time() - 60
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([{"machine": "legacy-box", "pid": 12345, "birth": birth}]))
    straggler = legacy.spawn("services.agent_host.daemon")
    code, report, _raw = _report(legacy, capsys, "--attest", str(rows))
    assert code == 2 and report["all_absent"] and not report["census_empty"]
    assert [p["pid"] for p in report["processes"] if p["kind"] == "ava"] == [straggler.pid]
    straggler.kill()
    straggler.wait()

    code, report, raw = _report(legacy, capsys, "--attest", str(rows))
    assert code == 0 and report["census_empty"] and report["home"] == str(legacy.home)
    document = tmp_path / "attestation.json"
    document.write_text(raw)
    attestation, data = inventory.load_attestation(document)
    assert data == raw.encode()
    assert attestation.proves_closure and attestation.absent() == {(12345, birth)}
    document.write_text(json.dumps(json.loads(raw) | {"census_empty": False}))
    with pytest.raises(inventory.RefusedError, match="census_empty contradicts"):
        inventory.load_attestation(document)


def test_non_canonical_home_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    real = tmp_path.resolve() / "real"
    real.mkdir()
    link = tmp_path.resolve() / "link"
    link.symlink_to(real)
    assert inventory.main(["--home", str(link)]) == 1
    assert "canonical" in capsys.readouterr().err


def test_attesting_without_a_persisted_machine_name_names_the_fix(
    make_legacy: Make, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    legacy = make_legacy()
    (legacy.home / "machine_name").unlink()
    rows = tmp_path / "rows.json"
    rows.write_text("[]")
    assert (
        inventory.main(
            ["--home", str(legacy.home), "--registry", str(legacy.registry), "--attest", str(rows)],
            host=legacy.scheduler.host(),
        )
        == 1
    )
    assert "no persisted machine name" in capsys.readouterr().err
