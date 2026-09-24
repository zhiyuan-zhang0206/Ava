# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import psycopg
import pytest
from pydantic import SecretStr, ValidationError

from cli.commands import _release_inventory as inventory
from cli.commands import _update_bootstrap as bootstrap
from shared.managed_writer_observation import (
    ExpectedLauncher,
    ExpectedProcess,
    ExpectedSession,
    ExpectedUnitWriters,
)
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease


def _expected(process: ExpectedProcess) -> ExpectedUnitWriters:
    session = ExpectedSession(name="ava-ops", process=process)
    return ExpectedUnitWriters(
        machine="machine",
        home="/unit",
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        processes=(process,),
        sessions=(session,),
        launchers=(ExpectedLauncher(kind="crontab", name="job", definition_digest="c" * 64),),
    )


def _inventory(expected: ExpectedUnitWriters) -> dict[str, object]:
    return {
        "version": 1,
        "expected": expected.model_dump(mode="json"),
        "services": [{"session": "ava-ops", "requires_db": True, "gate": None}],
        "excluded_registrations": [],
        "inventory_digest": expected.unit().inventory_digest,
        "closure": "unknown",
        "unresolved": ["retained"],
    }


def test_resume_inventory_allows_only_the_verified_observer_substitution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    before = _expected(ExpectedProcess(pid=11, create_time=1.0, starttime=1))
    live = ExpectedSession(
        name="ava-ops", process=ExpectedProcess(pid=22, create_time=2.0, starttime=2)
    )
    prepared = _inventory(before)
    encoded = json.dumps(prepared, sort_keys=True, separators=(",", ":")).encode()
    receipt = tmp_path / "run" / f"release-inventory-{hashlib.sha256(encoded).hexdigest()}.json"
    receipt.parent.mkdir()
    receipt.write_bytes(encoded)
    current = _inventory(
        before.model_copy(update={"processes": (live.process,), "sessions": (live,)})
    )
    monkeypatch.setattr(inventory, "collect_inventory", lambda *_args, **_kwargs: current)

    result = inventory.revalidate_bootstrap_inventory(
        cast("psycopg.Connection", None),
        cast("VerifiedRelease", object()),
        tmp_path,
        "machine",
        receipt,
        current_session=live,
        schema_digest="d" * 64,
    )
    assert result == before

    current["services"] = []
    with pytest.raises(ReleaseRejectedError, match="static facts"):
        inventory.revalidate_bootstrap_inventory(
            cast("psycopg.Connection", None),
            cast("VerifiedRelease", object()),
            tmp_path,
            "machine",
            receipt,
            current_session=live,
            schema_digest="d" * 64,
        )


def test_resume_inventory_accepts_only_the_accounted_launcher_quiescence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    initial = _expected(ExpectedProcess(pid=11, create_time=1.0, starttime=1))
    before = initial.model_copy(
        update={
            "launchers": (
                *initial.launchers,
                ExpectedLauncher(kind="launchd", name="com.ava.second", definition_digest="a" * 64),
            )
        }
    )
    live = ExpectedSession(
        name="ava-ops", process=ExpectedProcess(pid=22, create_time=2.0, starttime=2)
    )
    prepared = _inventory(before)
    encoded = json.dumps(prepared, sort_keys=True, separators=(",", ":")).encode()
    receipt = tmp_path / "run" / f"release-inventory-{hashlib.sha256(encoded).hexdigest()}.json"
    receipt.parent.mkdir()
    receipt.write_bytes(encoded)
    current_expected = before.model_copy(
        update={"processes": (live.process,), "sessions": (live,), "launchers": ()}
    )
    current = _inventory(current_expected)
    allow_empty = False

    def collect(*_args: object, **kwargs: object) -> dict[str, object]:
        nonlocal allow_empty
        allow_empty = kwargs.get("allow_empty_launchers") is True
        return current

    monkeypatch.setattr(inventory, "collect_inventory", collect)

    result = inventory.revalidate_bootstrap_inventory(
        cast("psycopg.Connection", None),
        cast("VerifiedRelease", object()),
        tmp_path,
        "machine",
        receipt,
        current_session=live,
        schema_digest="d" * 64,
    )
    assert result == before
    assert allow_empty

    # A crash between native definition removals leaves an exact subset.
    partial = current_expected.model_copy(update={"launchers": before.launchers[:1]})
    current = _inventory(partial)
    assert (
        inventory.revalidate_bootstrap_inventory(
            cast("psycopg.Connection", None),
            cast("VerifiedRelease", object()),
            tmp_path,
            "machine",
            receipt,
            current_session=live,
            schema_digest="d" * 64,
        )
        == before
    )

    changed = current_expected.model_copy(
        update={
            "launchers": (
                ExpectedLauncher(kind="crontab", name="other", definition_digest="e" * 64),
            )
        }
    )
    current = _inventory(changed)
    with pytest.raises(ReleaseRejectedError, match="observer substitution"):
        inventory.revalidate_bootstrap_inventory(
            cast("psycopg.Connection", None),
            cast("VerifiedRelease", object()),
            tmp_path,
            "machine",
            receipt,
            current_session=live,
            schema_digest="d" * 64,
        )


def test_phase_evidence_is_bounded() -> None:
    phase = {
        "stage": "prepared",
        "observed_at": datetime.now(UTC).isoformat(),
        "monotonic_s": 1.0,
        "pid": 1,
        "elapsed_s": None,
    }
    with pytest.raises(ValidationError):
        bootstrap.BootstrapJournal.model_validate(
            {
                "request": "/unit/run/request",
                "request_digest": "a" * 64,
                "inventory_digest": "b" * 64,
                "candidate_context_digest": "c" * 64,
                "recovery_context_digest": "d" * 64,
                "stage": "prepared",
                "cron": "",
                "phases": tuple([phase] * 65),
            }
        )


def test_journal_round_trips_the_strict_phase_tuple_as_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    request = tmp_path / "request.json"
    receipt = tmp_path / "inventory.json"
    candidate = tmp_path / "candidate.json"
    recovery = tmp_path / "recovery.json"
    for path in (request, receipt, candidate, recovery):
        path.write_text("{}")
    launcher = "e" * 64
    plan = SimpleNamespace(
        request_path=request,
        request=SimpleNamespace(
            inventory_receipt=str(receipt),
            candidate_context=str(candidate),
            recovery_context=str(recovery),
            normal_release_path=str(tmp_path / "normal.json"),
        ),
        candidate=SimpleNamespace(
            expected=SimpleNamespace(
                launchers=(
                    ExpectedLauncher(kind="crontab", name=launcher, definition_digest=launcher),
                )
            )
        ),
        validation_seconds=0.0,
        launchd=(),
    )
    envelope: dict[str, object] | None = None

    def read() -> dict[str, object] | None:
        return envelope

    def write(generation: str, journal: dict[str, object]) -> None:
        nonlocal envelope
        envelope = {"version": 1, "generation": generation, "journal": journal}

    monkeypatch.setattr(bootstrap.updater_handoff, "read_bootstrap_recovery", read)
    monkeypatch.setattr(bootstrap.updater_handoff, "write_bootstrap_recovery", write)

    bootstrap._journal(cast("bootstrap.PreparedBootstrapHop", plan), "g", "prepared", b"")
    assert envelope is not None
    journal = cast("dict[str, object]", envelope["journal"])
    assert journal["launcher_terminals"] == []

    bootstrap._journal(cast("bootstrap.PreparedBootstrapHop", plan), "g", "cron_quiesced", b"")
    journal = cast("dict[str, object]", envelope["journal"])
    assert journal["launcher_terminals"] == [
        {"label": launcher, "kind": "removed", "new_digest": None}
    ]

    bootstrap._journal(cast("bootstrap.PreparedBootstrapHop", plan), "g", "candidate_started", b"")
    journal = cast("dict[str, object]", envelope["journal"])
    assert journal["launcher_terminals"] == [
        {"label": launcher, "kind": "removed", "new_digest": None}
    ]

    assert journal["normal_release_planned"] is True
    phases = cast("list[dict[str, object]]", journal["phases"])
    assert [phase["stage"] for phase in phases] == [
        "prepared",
        "cron_quiesced",
        "candidate_started",
    ]


def test_journal_launcher_terminals_are_optional_and_validated() -> None:
    phase = bootstrap.BootstrapPhase(
        stage="prepared", observed_at=datetime.now(UTC), monotonic_s=1.0, pid=1, elapsed_s=None
    )
    base = {
        "request": "/unit/run/request",
        "request_digest": "a" * 64,
        "inventory_digest": "b" * 64,
        "candidate_context_digest": "c" * 64,
        "recovery_context_digest": "d" * 64,
        "stage": "prepared",
        "cron": "",
        "phases": (phase,),
    }
    legacy = bootstrap.BootstrapJournal.model_validate(base)
    assert legacy.launcher_terminals == ()

    carried = bootstrap.BootstrapJournal.model_validate(
        {**base, "launcher_terminals": ({"label": "e" * 64, "kind": "removed"},)}
    )
    assert carried.launcher_terminals[0].label == "e" * 64
    assert carried.launcher_terminals[0].new_digest is None

    with pytest.raises(ValidationError):
        bootstrap.BootstrapJournal.model_validate(
            {**base, "launcher_terminals": ({"label": "e" * 64, "kind": "rebound"},)}
        )


def test_child_projection_preserves_transport_encryption() -> None:
    plan = SimpleNamespace(
        candidate=SimpleNamespace(expected=SimpleNamespace(home="/unit")),
        projection=SimpleNamespace(
            db_url=SecretStr("postgresql://runner"),
            cluster_secret=SecretStr("secret"),
            ops_port=9000,
            transport_encryption="overlay",
        ),
    )
    assert (
        bootstrap._child_environment(cast("bootstrap.PreparedBootstrapHop", plan))[
            "AVA_TRANSPORT_ENCRYPTION"
        ]
        == "overlay"
    )


def test_fork_before_record_is_ambiguous() -> None:
    old = ExpectedProcess(pid=11, create_time=1.0, starttime=1)
    assert bootstrap._candidate_start_is_ambiguous("candidate_starting", None, old)
    assert bootstrap._candidate_start_is_ambiguous("candidate_starting", (old, "A"), old)


def test_launch_exception_before_record_never_starts_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = ExpectedProcess(pid=11, create_time=1.0, starttime=1)
    plan = SimpleNamespace(
        candidate=SimpleNamespace(
            challenge=SimpleNamespace(valid_until=datetime.now(UTC) + timedelta(minutes=1))
        ),
        projection=object(),
        image=object(),
        request=SimpleNamespace(candidate_context="/unit/run/candidate"),
        old_session=SimpleNamespace(process=old),
        journal=None,
        validation_seconds=0.0,
        launchd=(),
    )
    stage = ""
    starts = 0

    def record_stage(_plan: object, _generation: str, value: str, _cron: bytes) -> None:
        nonlocal stage
        stage = value

    def launch(*_args: object, **_kwargs: object) -> None:
        nonlocal starts
        starts += 1
        raise RuntimeError("fork returned before record publication")

    def recovery() -> dict[str, object]:
        journal = {
            "request": "/unit/run/request",
            "request_digest": "a" * 64,
            "inventory_digest": "b" * 64,
            "candidate_context_digest": "c" * 64,
            "recovery_context_digest": "d" * 64,
            "stage": stage,
            "cron": "",
            "phases": [
                {
                    "stage": "candidate_starting",
                    "observed_at": "2026-09-04T00:00:00+00:00",
                    "monotonic_s": 0.0,
                    "pid": 1,
                    "elapsed_s": None,
                }
            ],
        }
        return {"version": 1, "generation": "g", "journal": journal}

    monkeypatch.setattr(bootstrap, "validate_operation", lambda *_args: None)
    monkeypatch.setattr(bootstrap, "_cron_tables", lambda _plan: (b"", b""))
    monkeypatch.setattr(bootstrap, "_journal", record_stage)
    monkeypatch.setattr(bootstrap, "_replace_cron", lambda *_args: None)
    monkeypatch.setattr(bootstrap, "_stop_old_observer", lambda _plan: None)
    monkeypatch.setattr(bootstrap, "_start_observer", launch)
    monkeypatch.setattr(
        bootstrap,
        "_recorded_observer",
        lambda _plan: (_ for _ in ()).throw(ReleaseRejectedError("no new exact record")),
    )
    monkeypatch.setattr(bootstrap, "_restore_a", lambda *_args: pytest.fail("second launch"))
    monkeypatch.setattr(bootstrap.updater_handoff, "read_bootstrap_recovery", recovery)

    with pytest.raises(ReleaseRejectedError, match="spawn is ambiguous"):
        bootstrap._execute_bootstrap_hop(cast("bootstrap.PreparedBootstrapHop", plan), "g")
    assert starts == 1


@pytest.fixture
def native_launchd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    import os
    import plistlib
    import subprocess

    from cli.commands._update_bootstrap import native

    directory = tmp_path / "LaunchAgents"
    directory.mkdir()
    home = tmp_path / "unit"
    home.mkdir()
    (home / "run").mkdir(mode=0o700)
    argv = ["/retained/python", "-m", "services.agent_ops.daemon"]
    loaded: set[str] = set()
    actions: list[list[str]] = []
    launchers: list[ExpectedLauncher] = []
    for label in ("com.ava.a", "com.ava.b"):
        body = plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": argv,
                "EnvironmentVariables": {"AVA_HOME": str(home)},
                "RunAtLoad": label not in loaded,
            }
        )
        definition_path = directory / f"{label}.plist"
        definition_path.write_bytes(body)
        definition_path.chmod(0o600)
        launchers.append(
            ExpectedLauncher(
                kind="launchd",
                name=label,
                definition_digest=hashlib.sha256(body).hexdigest(),
            )
        )

    def path(label: str) -> Path:
        return directory / f"{label}.plist"

    def read(label: str) -> bytes | None:
        return path(label).read_bytes() if path(label).exists() else None

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        actions.append(argv)
        if argv[1] == "bootout":
            loaded.remove(argv[2].split("/")[-1])
        elif argv[1] == "bootstrap":
            loaded.add(Path(argv[3]).stem)
        else:
            pytest.fail("unexpected native write")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(native, "_path", path)
    monkeypatch.setattr(native, "read_launchd_definition", read)
    monkeypatch.setattr(native, "launchd_loaded_state", lambda label, _until: label in loaded)
    # Native writes are forbidden: membership cannot bind the effective argv.
    monkeypatch.setattr(subprocess, "run", run)
    expected = _expected(ExpectedProcess(pid=os.getpid(), create_time=1.0, starttime=None))
    expected = expected.model_copy(update={"home": str(home), "launchers": tuple(launchers)})
    until = datetime.now(UTC) + timedelta(minutes=1)
    snapshots = native.capture_launchd(expected, argv, until)
    return SimpleNamespace(
        native=native,
        directory=directory,
        expected=expected,
        argv=argv,
        loaded=loaded,
        actions=actions,
        until=until,
        snapshots=snapshots,
        path=path,
    )


def test_native_launchd_roundtrip_preserves_bytes_and_loaded_state(
    native_launchd: SimpleNamespace,
) -> None:
    env = native_launchd
    original = {item.label: env.path(item.label).read_bytes() for item in env.snapshots}
    env.native.quiesce_launchd(env.snapshots, env.until, lambda: None)
    assert not env.loaded
    assert not list(env.directory.glob("*.plist"))
    env.native.quiesce_launchd(env.snapshots, env.until, lambda: None)
    assert env.actions == []  # No loaded-job authority is inferred.
    env.native.restore_launchd(env.snapshots, env.until, lambda: None)
    assert not env.loaded
    assert {item.label: env.path(item.label).read_bytes() for item in env.snapshots} == original
    env.native.restore_launchd(env.snapshots, env.until, lambda: None)
    assert env.actions == []  # Unloaded RunAtLoad stays unloaded, even on replay.


@pytest.mark.parametrize("crash_at", [1, 2])
def test_native_launchd_restores_partial_quiesce_after_crash(
    native_launchd: SimpleNamespace,
    crash_at: int,
) -> None:
    from shared.updater_recovery import LaunchdRecovery

    env = native_launchd
    calls = 0

    def authorize() -> None:
        nonlocal calls
        calls += 1
        if calls == crash_at:
            raise SystemExit("updater died")

    with pytest.raises(SystemExit, match="updater died"):
        env.native.quiesce_launchd(env.snapshots, env.until, authorize)
    assert not env.loaded
    assert env.path("com.ava.a").exists() is (crash_at == 1)
    assert env.path("com.ava.b").exists()
    retained = tuple(
        LaunchdRecovery.model_validate_json(item.model_dump_json()) for item in env.snapshots
    )
    recaptured = env.native.capture_launchd(env.expected, env.argv, env.until, retained=retained)
    env.native.restore_launchd(recaptured, env.until, lambda: None)
    assert not env.loaded
    assert all(env.path(item.label).read_bytes() == item.definition.encode() for item in retained)


@pytest.mark.parametrize("phase", ["quiesce", "restore"])
def test_native_launchd_never_overwrites_changed_definition(
    native_launchd: SimpleNamespace,
    phase: str,
) -> None:
    env = native_launchd
    if phase == "restore":
        env.native.quiesce_launchd(env.snapshots, env.until, lambda: None)
    env.path("com.ava.a").write_bytes(b"another writer owns these bytes")
    before = len(env.actions)
    operation = env.native.quiesce_launchd if phase == "quiesce" else env.native.restore_launchd
    with pytest.raises(ReleaseRejectedError, match="refusing to overwrite"):
        operation(env.snapshots, env.until, lambda: None)
    assert len(env.actions) == before
    assert env.path("com.ava.a").read_bytes() == b"another writer owns these bytes"


def test_native_launchd_refuses_loaded_run_at_load_before_mutation(
    native_launchd: SimpleNamespace,
) -> None:
    env = native_launchd
    env.loaded.add("com.ava.b")
    with pytest.raises(ReleaseRejectedError, match="no verified effective binding"):
        env.native.capture_launchd(env.expected, env.argv, env.until)
    assert env.actions == []


def test_native_launchd_unknown_is_never_absence(
    native_launchd: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = native_launchd
    monkeypatch.setattr(env.native, "launchd_loaded_state", lambda *_args: None)
    with pytest.raises(ReleaseRejectedError, match="state is unknown"):
        env.native.quiesce_launchd(env.snapshots, env.until, lambda: None)
    assert env.actions == []
    assert all(env.path(item.label).exists() for item in env.snapshots)


@pytest.mark.parametrize("recreated", [False, True])
def test_native_launchd_custody_preserves_a_concurrent_replacement(
    native_launchd: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    *,
    recreated: bool,
) -> None:
    env = native_launchd
    first = env.snapshots[0]
    actual_rename = env.native.os.rename
    source = env.path(first.label)
    foreign = b"concurrent replacement after the admission read"
    later = b"another public generation after atomic custody"

    def race(before: Path, after: Path) -> None:
        assert before == source
        before.write_bytes(foreign)
        actual_rename(before, after)
        if recreated:
            before.write_bytes(later)

    monkeypatch.setattr(env.native.os, "rename", race)
    with pytest.raises(ReleaseRejectedError, match="retained"):
        env.native.quiesce_launchd(env.snapshots, env.until, lambda: None)
    assert source.read_bytes() == (later if recreated else foreign)
    assert Path(first.custody).read_bytes() == foreign
    assert env.path(env.snapshots[1].label).exists()
    assert env.actions == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("definition", "changed"),
        ("mode", 420),
        ("custody", "/unit/run/bootstrap-launcher-other.held"),
    ],
)
def test_bootstrap_journal_cannot_rebind_native_originals(
    field: str, value: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from shared.updater_recovery import LaunchdRecovery

    handoff = bootstrap.updater_handoff
    monkeypatch.setattr(handoff.shared.paths, "run_dir", lambda: tmp_path)
    handoff.begin(expected_session="ava-updater", generation="bootstrap")
    assert handoff.claim_running("bootstrap", expected_session="ava-updater")
    phase = bootstrap.BootstrapPhase(
        stage="prepared",
        observed_at=datetime.now(UTC),
        monotonic_s=0.0,
        pid=1,
        elapsed_s=None,
    )
    original = bootstrap.BootstrapJournal(
        request="/unit/run/request",
        request_digest="a" * 64,
        inventory_digest="b" * 64,
        candidate_context_digest="c" * 64,
        recovery_context_digest="d" * 64,
        stage="prepared",
        cron="",
        phases=(phase,),
        launchd=(
            LaunchdRecovery(
                label="com.ava.bootstrap",
                definition="retained",
                loaded=False,
                mode=384,
                custody="/unit/run/bootstrap-launcher-original.held",
            ),
        ),
    )
    handoff.write_bootstrap_recovery("bootstrap", original.model_dump(mode="json"))
    before = handoff.bootstrap_state_path().read_bytes()
    changed = original.model_copy(
        update={
            "stage": "launchers_quiesced",
            "phases": (phase, phase.model_copy(update={"stage": "launchers_quiesced"})),
            "launchd": (original.launchd[0].model_copy(update={field: value}),),
        }
    )
    with pytest.raises(handoff.BootstrapRecoveryInvalidError, match="identity changed"):
        handoff.write_bootstrap_recovery("bootstrap", changed.model_dump(mode="json"))
    assert handoff.bootstrap_state_path().read_bytes() == before
    changed = changed.model_copy(update={"launchd": original.launchd})
    handoff.write_bootstrap_recovery("bootstrap", changed.model_dump(mode="json"))
    retained = handoff.read_bootstrap_recovery()
    assert retained is not None
    assert retained["journal"] == changed.model_dump(mode="json")
