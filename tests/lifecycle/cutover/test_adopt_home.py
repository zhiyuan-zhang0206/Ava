"""One-time adoption of legacy-born homes: plan, execute, repeat, resume and refuse."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from cli.start_identity import read_intent
from scripts import cutover_adopt_home as adopt
from shared import pause_owner
from shared.cluster import home_slug
from tests.lifecycle.cutover.conftest import (
    CANARY,
    MACHINE_KEY,
    SERVICE_PATH,
    LegacyHome,
    arm_health_probe,
)

Make = Callable[..., LegacyHome]


def _mode(legacy: LegacyHome) -> str:
    return "gateway" if legacy.gateway else "remote-unit"


def _run(legacy: LegacyHome, *extra: str, service_path: bool = True) -> int:
    argv = ["--home", str(legacy.home), "--registry", str(legacy.registry), *extra]
    if service_path:
        argv += ["--service-path", SERVICE_PATH]
    if "--execute" in extra:
        argv += ["--expect-mode", _mode(legacy)]
    return adopt.main(argv, host=legacy.scheduler.host(), checkout=legacy.checkout)


def _journal(legacy: LegacyHome) -> dict[str, Any]:
    return json.loads((legacy.home / "cutover-rollback" / "adopt-home.json").read_text())


def _hold(legacy: LegacyHome) -> pause_owner.PauseOwnerSnapshot:
    return pause_owner.read_for_home(legacy.home)


def test_dry_run_prints_the_plan_and_changes_nothing(
    make_legacy: Make, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = make_legacy()
    before = legacy.snapshot()
    assert _run(legacy) == 0
    out = capsys.readouterr().out
    assert legacy.snapshot() == before
    assert "[dry-run] no changes made." in out
    assert "record-retire" in out and "hold-create" in out and CANARY not in out
    assert not (legacy.home / "cutover-rollback").exists()


def _assert_runner_intent(legacy: LegacyHome) -> None:
    intent = read_intent(legacy.home)
    assert intent is not None
    shape = (intent["phase"], intent["roles"], intent["record"], intent["worktree"])
    assert shape == ("provisioned", ["agent-runner"], None, False)
    assert (intent["checkout"], intent["config_digest"]) == (str(legacy.checkout), None)
    assert intent["env"] == {
        "AVA_MACHINE_NAME": "legacy-box",
        "AVA_MACHINE_HOST": "10.0.0.7",
        "AVA_GATEWAY_URL": "http://gateway.test:8000",
        "AVA_MACHINE_SERVE_GATEWAY": "false",
        "AVA_MACHINE_SERVE_AGENT_RUNNER": "true",
        "AVA_MACHINE_SERVE_OBSERVABILITY_STATION": "false",
        "AVA_SERVICE_PATH": SERVICE_PATH,
    }


def _moved(home: Path, relative: str, area: str) -> bool:
    return not (home / relative).exists() and (home / "cutover-rollback" / area / relative).exists()


def _assert_runner_files(legacy: LegacyHome) -> None:
    home = legacy.home
    # The human bearer went with the gateway-only keys.
    assert set(legacy.env()) == {MACHINE_KEY, "AVA_GATEWAY_URL", "AVA_SERVICE_PATH"}
    residue = ("backups", "redis", "pgbouncer", "physical-backup", "masked-backup-20260920")
    assert all(_moved(home, name, "residue") for name in residue)
    assert _moved(home, "run/bootstrap-snapshot.json", "residue")
    assert list((home / "secrets").iterdir()) == []
    # The pre-edit .env snapshots went with backups/, off the runner home.
    assert (home / "cutover-rollback" / "residue" / "backups" / "env").is_dir()
    legacy_files = ("installed_sha", "run/sessions", "run/agent-host.pid", "disabled_services")
    assert all(_moved(home, name, "home-files") for name in legacy_files)
    assert not (home / "service-selection.json").exists()


def _assert_fresh_hold(legacy: LegacyHome, holder: str) -> None:
    hold = _hold(legacy)
    assert (hold.status, hold.holder) == ("paused", holder)
    assert hold.maintenance is not None
    assert hold.maintenance.phase == "stopped"
    archived = legacy.home / "cutover-rollback" / "home-files" / "run" / "deploy-pause-owner.json"
    assert json.loads(archived.read_text())["holder"] == "ubuntu:pid2669750"


def _assert_only_own_jobs_retired(legacy: LegacyHome) -> None:
    slug, sibling = legacy.slug, home_slug(legacy.sibling)
    loaded = legacy.scheduler.loaded()
    assert {label for label in loaded if f".{slug}." in label} == set()
    assert f"com.ava.{sibling}.watchdog-probe.agent-runner" in loaded
    agents = {path.name for path in (legacy.scheduler.root / "LaunchAgents").iterdir()}
    assert agents == {
        f"com.ava.permissions-helper.{slug}.plist",
        f"com.ava.{sibling}.watchdog-probe.agent-runner.plist",
        f"com.ava.{sibling}.health-probe.plist",
    }
    assert len(list((legacy.home / "cutover-rollback" / "os-jobs").glob("*.plist"))) == 5
    remaining = legacy.scheduler.crontab()
    assert len(remaining) == 3
    assert [line for line in remaining if slug in line] == [
        line for line in remaining if sibling in line
    ]


def test_runner_adoption_converts_the_home_and_touches_only_its_own_jobs(
    make_legacy: Make, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = make_legacy()
    sibling_record = legacy.registry_records()[str(legacy.sibling)]
    assert _run(legacy, "--execute", "--cutover-id", "c1") == 0
    assert "cutover hold cutover:c1" in capsys.readouterr().out
    _assert_runner_intent(legacy)
    assert legacy.registry_records() == {str(legacy.sibling): sibling_record}
    _assert_runner_files(legacy)
    _assert_fresh_hold(legacy, "cutover:c1")
    _assert_only_own_jobs_retired(legacy)
    journal_path = legacy.home / "cutover-rollback" / "adopt-home.json"
    states = {name: step["state"] for name, step in _journal(legacy)["steps"].items()}
    assert states == dict.fromkeys(adopt.STEPS, "done")
    assert journal_path.stat().st_mode & 0o777 == 0o600
    assert CANARY not in journal_path.read_text()


def _assert_gateway_jobs(legacy: LegacyHome) -> None:
    root, slug, sibling = legacy.scheduler.root, legacy.slug, home_slug(legacy.sibling)
    user_units = [path.name for path in (root / "user-units").iterdir()]
    assert user_units == [f"com.ava.loki.{sibling}.service"]
    system_units = [path.name for path in (root / "system-units").iterdir()]
    assert system_units == [f"ava-boot.{sibling}.service"]
    remaining = legacy.scheduler.crontab()
    assert [line for line in remaining if slug in line] == [
        line for line in remaining if sibling in line
    ]
    calls = legacy.scheduler.calls()
    units = [i for i, call in enumerate(calls) if call.startswith(("systemctl", "sudo"))]
    assert calls.index("crontab -") < min(units)  # the watchdog lines go first


def test_gateway_adoption_completes_the_port_block_and_translates_selection(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared import service_selection
    from shared.config import settings

    legacy = make_legacy(roles=("gateway", "agent-runner"), platform="linux")
    records = legacy.registry_records()
    added = records[str(legacy.home)]["ports"].pop("agent_runner_watchdog")  # a later key
    legacy.registry.write_text(json.dumps(records))
    dead = {"AVA_CLUSTER", "AVA_RESTARTER_HEALTH_PORT"}
    expected_env = {k: v for k, v in legacy.env().items() if k not in dead}

    assert _run(legacy, "--execute") == 0

    record = legacy.registry_records()[str(legacy.home)]
    assert record["ports"]["agent_runner_watchdog"] == added
    intent = read_intent(legacy.home)
    assert intent is not None
    assert (intent["record"], intent["roles"]) == (record, ["agent-runner", "gateway"])
    assert legacy.env() == {**expected_env, "AVA_SERVICE_PATH": SERVICE_PATH}
    assert (legacy.home / "service-selection.json").read_text() == (
        '{"version": 1, "mode": "except", "names": ["memory-indexer"]}\n'
    )
    monkeypatch.setattr(settings.general, "ava_home", str(legacy.home))
    selection = service_selection.read_selection()
    assert (selection.mode, selection.names) == ("except", frozenset({"memory-indexer"}))
    _assert_gateway_jobs(legacy)
    assert _journal(legacy)["steps"]["residue"]["effects"] == []  # no residue on a gateway


def test_the_cutover_hold_is_a_standing_maintenance_hold(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = make_legacy()
    assert _run(legacy, "--execute", "--cutover-id", "c2") == 0
    from shared import maintenance
    from shared.config import settings

    monkeypatch.setattr(settings.general, "ava_home", str(legacy.home))
    hold = _journal(legacy)["hold"]
    current = maintenance.require_operation(
        hold["holder"], datetime.fromisoformat(hold["acquired_at"])
    )
    assert current.maintenance is not None and current.maintenance.phase == "stopped"
    assert maintenance.held() and maintenance.business_paused() and maintenance.in_stop_leg()


def test_repeat_is_a_verified_no_op(make_legacy: Make) -> None:
    legacy = make_legacy()
    assert _run(legacy, "--execute") == 0
    after = legacy.snapshot()
    calls = len(legacy.scheduler.calls())
    assert _run(legacy, "--execute") == 0
    assert _run(legacy, "--execute", service_path=False) == 0
    assert legacy.snapshot() == after
    assert len(legacy.scheduler.calls()) == calls


def test_a_crash_between_steps_resumes_from_the_journal(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = make_legacy()
    real = adopt._record_retire

    def crash(*_args: object) -> None:
        raise RuntimeError("injected crash")

    monkeypatch.setattr(adopt, "_record_retire", crash)
    assert _run(legacy, "--execute") == 1
    states = {name: step["state"] for name, step in _journal(legacy)["steps"].items()}
    assert states == {**dict.fromkeys(adopt.STEPS[:6], "done"), "record": "started"}
    assert read_intent(legacy.home) is None and str(legacy.home) in legacy.registry_records()

    monkeypatch.setattr(adopt, "_record_retire", real)
    assert _run(legacy, "--execute", service_path=False) == 0
    assert str(legacy.home) not in legacy.registry_records()
    assert read_intent(legacy.home) is not None
    assert set(_journal(legacy)["steps"]) == set(adopt.STEPS)


def test_a_crash_inside_a_step_resumes_without_repeating_effects(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = make_legacy()
    real = adopt._archive
    moved: list[str] = []

    def crash_after_two(home: Path, effect: dict[str, Any]) -> None:
        if effect["area"] == "residue" and len(moved) == 2:
            raise OSError("injected crash")
        real(home, effect)
        if effect["area"] == "residue":
            moved.append(effect["src"])

    monkeypatch.setattr(adopt, "_archive", crash_after_two)
    assert _run(legacy, "--execute") == 1
    assert _journal(legacy)["steps"]["residue"]["state"] == "started"
    assert all((legacy.home / "cutover-rollback" / "residue" / src).exists() for src in moved)

    monkeypatch.setattr(adopt, "_archive", real)
    assert _run(legacy, "--execute") == 0
    residue = legacy.home / "cutover-rollback" / "residue"
    assert {"backups", "redis", "pgbouncer", "physical-backup"} <= {
        p.name for p in residue.iterdir()
    }
    assert _journal(legacy)["steps"]["residue"]["state"] == "done"


def test_a_remote_unit_loses_the_human_bearer(make_legacy: Make) -> None:
    """A remote unit authenticates with its capability's machine API token, so
    the adoption always removes the human bearer from its `.env`."""
    legacy = make_legacy()
    assert "AVA_CLUSTER_SECRET" in legacy.env()
    assert _run(legacy, "--execute") == 0
    assert "AVA_CLUSTER_SECRET" not in legacy.env() and MACHINE_KEY in legacy.env()


def test_a_remote_units_held_start_installs_its_capability_bundle(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli.start_intent
    from shared import start_serving
    from shared.config import settings

    legacy = make_legacy()
    assert _run(legacy, "--execute") == 0
    monkeypatch.setattr(settings.general, "ava_home", str(legacy.home))
    seen: list[object] = []

    def fake_start(args: object) -> int:
        seen.append(getattr(args, "db_capability", None))
        return 0

    monkeypatch.setattr(cli.start_intent, "run_start", fake_start)
    monkeypatch.setattr(start_serving, "is_serving", lambda: True)
    bundle = str(legacy.home.parent / "mini.bundle")
    argv = ["--home", str(legacy.home), "--start", "--db-capability", bundle]
    assert adopt.main(argv, checkout=legacy.checkout) == 0
    assert seen == [bundle]
    with pytest.raises(SystemExit):
        adopt.main(["--home", str(legacy.home), "--db-capability", bundle])


def test_a_continuation_refuses_different_inputs(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = make_legacy()

    def crash(*_args: object) -> None:
        raise OSError("injected crash")

    monkeypatch.setattr(adopt, "_record_retire", crash)
    assert _run(legacy, "--execute") == 1
    argv = ["--home", str(legacy.home), "--registry", str(legacy.registry), "--execute"]
    argv += ["--expect-mode", "remote-unit"]
    assert (
        adopt.main(
            [*argv, "--service-path", "/usr/bin:/bin"],
            host=legacy.scheduler.host(),
            checkout=legacy.checkout,
        )
        == 1
    )
    assert (
        adopt.main(
            [*argv, "--keep-secret", "other"],
            host=legacy.scheduler.host(),
            checkout=legacy.checkout,
        )
        == 1
    )


def test_main_refuses_a_checkout_that_does_not_own_the_home(
    make_legacy: Make, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = make_legacy()
    argv = ["--home", str(legacy.home), "--execute", "--expect-mode", "remote-unit"]
    assert adopt.main(argv) == 1
    assert "checkout that owns" in capsys.readouterr().err
    assert not (legacy.home / "cutover-rollback").exists()


def test_a_throwaway_checkout_may_plan_but_never_adopt_a_home_with_its_own_source(
    make_legacy: Make, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A T-3 dry-run needs a throwaway worktree whose `.ava_home` names the
    home; an `--execute` from it would bind the production intent to that
    disposable path, which a later release start uses."""
    legacy = make_legacy()
    throwaway = tmp_path / "throwaway"
    throwaway.mkdir()
    (throwaway / ".ava_home").write_text(f"{legacy.home}\n")
    argv = ["--home", str(legacy.home), "--registry", str(legacy.registry)]
    argv += ["--service-path", SERVICE_PATH]
    host = legacy.scheduler.host()
    assert adopt.main(argv, host=host, checkout=throwaway) == 0
    capsys.readouterr()
    execute = [*argv, "--execute", "--expect-mode", "remote-unit"]
    assert adopt.main(execute, host=host, checkout=throwaway) == 1
    assert str(legacy.home / "source") in capsys.readouterr().err
    assert adopt.main([*argv, "--start"], host=host, checkout=throwaway) == 1
    assert not (legacy.home / "cutover-rollback").exists()


@pytest.mark.parametrize(
    ("roles", "wrong"), [(("agent-runner",), "gateway"), (("gateway",), "remote-unit")]
)
def test_execute_requires_the_expected_mode(
    make_legacy: Make, capsys: pytest.CaptureFixture[str], roles: tuple[str, ...], wrong: str
) -> None:
    """The mode is inferred from capability files alone, and a remote unit's
    adoption strips credentials and moves `pg/`, `backups/` and `secrets/`."""
    legacy = make_legacy(roles=roles)
    argv = ["--home", str(legacy.home), "--registry", str(legacy.registry)]
    argv += ["--service-path", SERVICE_PATH, "--execute"]
    host = legacy.scheduler.host()
    with pytest.raises(SystemExit):
        adopt.main(argv, host=host, checkout=legacy.checkout)
    before = legacy.snapshot()
    assert adopt.main([*argv, "--expect-mode", wrong], host=host, checkout=legacy.checkout) == 1
    assert f"--expect-mode {wrong}" in capsys.readouterr().err
    assert legacy.snapshot() == before
    assert not (legacy.home / "cutover-rollback").exists()


def _listen(port: int) -> socket.socket:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", port))
    server.listen()
    return server


def _intent_without_journal(legacy: LegacyHome) -> None:
    doc: dict[str, object] = {
        "version": 1,
        "home": str(legacy.home),
        "checkout": str(legacy.checkout),
        "worktree": False,
        "roles": ["agent-runner"],
        "config_digest": None,
        "phase": "ready",
        "record": None,
        "env": {},
    }
    (legacy.home / "start-intent.json").write_text(json.dumps(doc))
    (legacy.home / "start-intent.json").chmod(0o600)


def _env_edit(legacy: LegacyHome, key: str, value: str | None) -> None:
    lines = [
        line
        for line in (legacy.home / ".env").read_text().splitlines()
        if not line.startswith(f"{key}=")
    ]
    lines += [f"{key}={value}"] if value is not None else []
    (legacy.home / ".env").write_text("".join(f"{line}\n" for line in lines))


def _foreign_hold(legacy: LegacyHome) -> None:
    doc = {"state": "paused", "holder": "someone-else", "acquired_at": "2026-09-27T00:00:00+00:00"}
    (legacy.home / "run" / "deploy-pause-owner.json").write_text(json.dumps(doc))


def _legacy_stop_hold(legacy: LegacyHome, phase: str = "stopped") -> dict[str, Any]:
    """The maintenance hold a legacy `ava stop` leaves after draining agent 7."""
    doc: dict[str, Any] = {
        "state": "paused",
        "holder": "local-stop:legacy-box:4242",
        "acquired_at": "2026-09-28T01:02:03+00:00",
        "maintenance": {
            "phase": phase,
            "commands": {"7": 11},
            "drained": [7],
            "failures": {},
            "parked": [],
        },
        "driver": None,
    }
    (legacy.home / "run" / "deploy-pause-owner.json").write_text(json.dumps(doc))
    return doc


def _drop_record(legacy: LegacyHome) -> None:
    records = legacy.registry_records()
    del records[str(legacy.home)]
    legacy.registry.write_text(json.dumps(records))


def _colliding_new_port(legacy: LegacyHome) -> None:
    records = legacy.registry_records()
    ours: dict[str, int] = records[str(legacy.home)]["ports"]
    port = ours.pop("agent_runner_watchdog")
    records[str(legacy.sibling)]["ports"]["memory_search"] = port
    legacy.registry.write_text(json.dumps(records))


_REFUSALS: dict[str, tuple[tuple[str, ...], Callable[[LegacyHome], object], str]] = {
    "live-service": (
        ("agent-runner",),
        lambda h: h.spawn("services.agent_host.daemon"),
        "must stop first",
    ),
    "declared-path-differs": (
        ("agent-runner",),
        lambda h: _env_edit(h, "AVA_SERVICE_PATH", "/usr/bin"),
        "different AVA_SERVICE_PATH",
    ),
    "unattributable-cron": (
        ("agent-runner",),
        lambda h: (
            (h.scheduler.state / "crontab")
            .open("a")
            .write("* * * * * /x/ava cluster hold-watchdog\n")
        ),
        "nobody can attribute",
    ),
    "foreign-hold": (("agent-runner",), _foreign_hold, "not a completed stop's maintenance hold"),
    "unfinished-legacy-stop": (
        ("agent-runner",),
        lambda h: _legacy_stop_hold(h, phase="stopping"),
        "phase stopping",
    ),
    "destroy-intent": (
        ("agent-runner",),
        lambda h: (h.home / "destroy-intent.json").write_text("{}"),
        "destroy intent",
    ),
    "existing-intent": (("agent-runner",), _intent_without_journal, "did not write"),
    "no-gateway-url": (
        ("agent-runner",),
        lambda h: _env_edit(h, "AVA_GATEWAY_URL", None),
        "no gateway URL",
    ),
    "gateway-without-record": (("gateway", "agent-runner"), _drop_record, "no registry record"),
    # Plan 1.2: company-air and company-mini carry no machine_name file.
    "no-machine-name": (
        ("agent-runner",),
        lambda h: (h.home / "machine_name").unlink(),
        "no persisted machine name",
    ),
    "data-plane-bound": (
        ("gateway", "agent-runner"),
        lambda h: _listen(h.ports["redis"]),
        "is bound",
    ),
    "archive-collision": (
        ("agent-runner",),
        lambda h: (
            (h.home / "cutover-rollback" / "home-files").mkdir(parents=True),
            (h.home / "cutover-rollback" / "home-files" / "installed_sha").write_text("x"),
        ),
        "archive already holds",
    ),
    "port-conflict": (
        ("gateway", "agent-runner"),
        lambda h: _env_edit(h, "AVA_GATEWAY_PORT", "1"),
        "port conflict",
    ),
    "capability-disagreement": (
        ("agent-runner",),
        lambda h: _env_edit(h, "AVA_MACHINE_SERVE_GATEWAY", "true"),
        "disagree",
    ),
    "new-port-collision": (("gateway", "agent-runner"), _colliding_new_port, "collides"),
    # W1 unregistered it; every start of the old gateway registers it again.
    "rearmed-health-probe": (
        ("gateway", "agent-runner"),
        arm_health_probe,
        "legacy health probe is registered",
    ),
    "rearmed-health-probe-launchd": (
        ("gateway", "agent-runner"),
        lambda h: (
            h.scheduler.root / "LaunchAgents" / f"com.ava.{h.slug}.health-probe.plist"
        ).write_text("<plist/>"),
        "health-probe): unregister it",
    ),
}


@pytest.mark.parametrize("case", sorted(_REFUSALS))
def test_ambiguous_or_live_state_refuses_before_any_effect(
    make_legacy: Make, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    roles, mutate, message = _REFUSALS[case]
    legacy = make_legacy(roles=roles)
    kept = mutate(legacy)
    before = legacy.snapshot()
    try:
        assert _run(legacy, "--execute") == 1
    finally:
        if isinstance(kept, socket.socket):
            kept.close()
    err = capsys.readouterr().err
    assert "adoption refused, nothing changed" in err and message in err
    assert legacy.snapshot() == before
    assert not (legacy.home / "cutover-rollback" / "adopt-home.json").exists()


def test_the_new_codes_probe_after_the_jobs_step_never_refuses(
    make_legacy: Make, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Once the `jobs` step retired the legacy jobs, the probe name is the new
    converge's registration: neither the attestation nor the plan refuses it."""
    from scripts import cutover_inventory as inventory

    legacy = make_legacy(roles=("gateway", "agent-runner"), platform="linux")
    assert _run(legacy, "--execute") == 0
    capsys.readouterr()
    arm_health_probe(legacy)
    rows = tmp_path / "rows.json"
    rows.write_text("[]")
    argv = ["--home", str(legacy.home), "--registry", str(legacy.registry), "--attest", str(rows)]
    assert inventory.main(argv, host=legacy.scheduler.host()) == 0
    assert json.loads(capsys.readouterr().out)["census_empty"]


def test_missing_service_path_refuses_and_is_never_inferred(
    make_legacy: Make, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = make_legacy()
    assert _run(legacy, "--execute", service_path=False) == 1
    assert "pass the reviewed --service-path" in capsys.readouterr().err
    assert "AVA_SERVICE_PATH" not in legacy.env()


def test_held_start_runs_start_inside_the_hold_and_leaves_it_closed(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import cli.start_intent
    from shared import maintenance, start_serving
    from shared.config import settings

    legacy = make_legacy(roles=("gateway", "agent-runner"))
    argv = ["--home", str(legacy.home), "--start"]
    assert adopt.main(argv, checkout=legacy.checkout) == 1
    assert "adoption is not complete" in capsys.readouterr().err
    assert _run(legacy, "--execute", "--cutover-id", "c3") == 0
    monkeypatch.setattr(settings.general, "ava_home", str(legacy.home))
    seen: list[tuple[bool, str]] = []

    def fake_start(_args: object) -> int:
        snapshot = maintenance.snapshot()
        assert snapshot is not None and snapshot.maintenance is not None
        seen.append((maintenance.start_authorized(), snapshot.maintenance.phase))
        return 0

    monkeypatch.setattr(cli.start_intent, "run_start", fake_start)
    monkeypatch.setattr(start_serving, "is_serving", lambda: True)
    capsys.readouterr()
    assert adopt.main(argv, checkout=legacy.checkout) == 0
    assert seen == [(True, "starting")]
    hold = _hold(legacy)
    assert hold.status == "paused" and hold.maintenance is not None
    assert hold.maintenance.phase == "ready" and maintenance.business_paused()
    assert f"cutover_adopt_home.py --home {legacy.home} --resume" in capsys.readouterr().out
    assert _journal(legacy)["hold"]["holder"] == "cutover:c3"
    assert adopt.main(argv, checkout=legacy.checkout) == 0  # already ready: no second start
    assert len(seen) == 1


def test_held_start_does_not_mark_ready_when_the_start_fails(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli.start_intent
    from shared.config import settings

    legacy = make_legacy(roles=("gateway", "agent-runner"))
    assert _run(legacy, "--execute") == 0
    monkeypatch.setattr(settings.general, "ava_home", str(legacy.home))

    def failed_start(_args: object) -> int:
        return 7

    monkeypatch.setattr(cli.start_intent, "run_start", failed_start)
    assert adopt.main(["--home", str(legacy.home), "--start"], checkout=legacy.checkout) == 7
    hold = _hold(legacy)
    assert hold.maintenance is not None and hold.maintenance.phase == "starting"
    assert dotenv_values(legacy.home / ".env")["AVA_SERVICE_PATH"] == SERVICE_PATH


def test_a_completed_legacy_stop_hold_becomes_the_cutover_hold(
    make_legacy: Make, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import cli.start_intent
    from shared import maintenance, start_serving
    from shared.config import settings

    legacy = make_legacy()
    doc = _legacy_stop_hold(legacy)
    assert _run(legacy) == 0
    assert '"holder": "local-stop:legacy-box:4242"' in capsys.readouterr().out  # hold-adopt
    assert _run(legacy, "--execute", "--cutover-id", "c4") == 0
    hold = _journal(legacy)["hold"]
    assert hold == {
        "holder": doc["holder"],
        "acquired_at": doc["acquired_at"],
        "origin": "legacy-stop",
    }
    assert json.loads((legacy.home / "run" / "deploy-pause-owner.json").read_text()) == doc
    assert (
        not (legacy.home / "cutover-rollback" / "home-files" / "run")
        .joinpath("deploy-pause-owner.json")
        .exists()
    )

    def started(_args: object) -> int:
        return 0

    monkeypatch.setattr(settings.general, "ava_home", str(legacy.home))
    monkeypatch.setattr(cli.start_intent, "run_start", started)
    monkeypatch.setattr(start_serving, "is_serving", lambda: True)
    capsys.readouterr()
    assert adopt.main(["--home", str(legacy.home), "--start"], checkout=legacy.checkout) == 0
    assert f"cutover_adopt_home.py --home {legacy.home} --resume" in capsys.readouterr().out
    current = maintenance.snapshot()
    assert current is not None and current.maintenance is not None
    assert (current.maintenance.phase, current.maintenance.commands) == ("ready", {7: 11})
