"""`cli.commands._managed_writer_collector` -- the channel-D collector (task #4129 I5).

The red battery: every binding the collector re-derives is mutated once and must
refuse (fail-closed), the wait's three fates are pinned, and the adoption path
is exercised with a stubbed seat. The module's own import discipline is pinned
too: the collector is heavy (it reaches `shared.session_record` through the ops
daemon's schemas) and must stay behind method-local imports in the eager
updater closure.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple
from uuid import UUID, uuid4

import httpx
import pytest

from cli.commands import _managed_writer_collector as collector_mod
from cli.commands._managed_writer_collector import (
    CollectorRefusal,
    CollectorTransferError,
    JournaledRegistration,
    accept_unit,
    collect_and_adopt,
    read_journaled_registration,
    wait_for_candidate_ready,
)
from cli.commands._managed_writer_hop import CollectorInput, CollectorUnitInput
from services.agent_ops.bootstrap import BootstrapRuntimeIdentity, PreparedObservation
from shared.hop_ledger import envelope_bytes
from shared.managed_writer_barrier import (
    ManagedUnit,
    ManagedUnitClosure,
    ManagedWriterBarrierError,
    RolloutIdentity,
)
from shared.managed_writer_closure import LauncherTerminal
from shared.managed_writer_observation import (
    ExpectedLauncher,
    ExpectedProcess,
    ExpectedSession,
    ExpectedUnitWriters,
    ObservationChallenge,
)
from shared.managed_writer_publication import PendingPublication, PublishedUnit, WriterPublication
from shared.native_job_observation import LauncherObservation
from shared.updater_recovery import (
    BootstrapRecoveryJournal,
    BootstrapRecoveryPhase,
    BootstrapRecoveryStage,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The eager updater closure must not reach the collector (or what it drags in).
_MUST_BE_LAZY = ("cli.commands._managed_writer_collector", "shared.session_record")

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
NOW = datetime.now(UTC)
VALID_UNTIL = NOW + timedelta(hours=1)
OBSERVED_AT = NOW
ARTIFACT = "a" * 64
MANIFEST = "b" * 64
SCHEMA = "c" * 64
CHALLENGE = UUID(int=7)
RECEIPT_DIGEST = hashlib.sha256(b"sealed-receipt").hexdigest()
CANDIDATE_DIGEST = hashlib.sha256(b"candidate").hexdigest()
EVENT_DIGEST = hashlib.sha256(b"plan").hexdigest()


def _operation() -> RolloutIdentity:
    return RolloutIdentity(holder="gateway:pid9", acquired_at=T0, target_sha="0" * 40)


def _expected(machine: str = "runner-a", home: str = "/ava-a") -> ExpectedUnitWriters:
    writer = ExpectedProcess(pid=222, create_time=1700000001.0)
    return ExpectedUnitWriters(
        machine=machine,
        home=home,
        artifact_digest=ARTIFACT,
        manifest_digest=MANIFEST,
        processes=(writer,),
        sessions=(ExpectedSession(name="ava-ops", process=writer),),
        launchers=(ExpectedLauncher(kind="crontab", name="bootstrap", definition_digest="d" * 64),),
    )


def _context_bytes(expected: ExpectedUnitWriters, *, valid_until: datetime = VALID_UNTIL) -> bytes:
    context = PreparedObservation(
        expected=expected,
        operation=_operation(),
        challenge=ObservationChallenge(challenge=CHALLENGE, valid_until=valid_until),
        schema_digest=SCHEMA,
    )
    return (
        json.dumps(context.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _unit_input(
    machine: str = "runner-a",
    home: str = "/ava-a",
    *,
    ops_url: str | None = "http://runner-a:9",
    normal_request: bytes | None = None,
    request: bytes | None = None,
) -> CollectorUnitInput:
    return CollectorUnitInput(
        machine=machine,
        home=home,
        ops_url=ops_url,
        candidate_context=_context_bytes(_expected(machine, home)),
        request=request if request is not None else b'{"hop":"request"}\n',
        recovery_context=b'{"recovery":"context"}\n',
        normal_request=normal_request,
        prepared_receipt_digest=RECEIPT_DIGEST,
    )


def _journal_dict(
    unit: CollectorUnitInput,
    *,
    stage: BootstrapRecoveryStage = "candidate_ready",
    planned: bool = False,
    terminals: tuple[LauncherTerminal, ...] | None = None,
) -> dict[str, object]:
    journal = BootstrapRecoveryJournal(
        request=f"{unit.home}/run/hop-request.json",
        request_digest=hashlib.sha256(unit.request).hexdigest(),
        inventory_digest=unit.prepared_receipt_digest,
        candidate_context_digest=hashlib.sha256(unit.candidate_context).hexdigest(),
        recovery_context_digest=hashlib.sha256(unit.recovery_context).hexdigest(),
        normal_release_planned=planned,
        stage=stage,
        cron="",
        phases=(
            BootstrapRecoveryPhase(
                stage=stage, observed_at=NOW, monotonic_s=1.0, pid=os.getpid(), elapsed_s=None
            ),
        ),
        launcher_terminals=(
            terminals
            if terminals is not None
            else (LauncherTerminal(label="bootstrap", kind="removed"),)
        ),
    )
    return journal.model_dump(mode="json")


def _ledger_dict(
    unit: CollectorUnitInput,
    journal: dict[str, object] | None = None,
    *,
    session_record: dict[str, object] | None = None,
    **overrides: object,
) -> dict[str, object]:
    journal = journal if journal is not None else _journal_dict(unit)
    base: dict[str, object] = {
        "mode": "bootstrap_hop_ledger",
        "challenge": str(CHALLENGE),
        "journal_present": True,
        "journal_readable": True,
        "version": 1,
        "generation": "gen-1",
        "journal": journal,
        "payload_digest": hashlib.sha256(envelope_bytes(1, "gen-1", journal)).hexdigest(),
        "boot_id": str(uuid4()),
        "session_record": (
            session_record
            if session_record is not None
            else {"state": "ok", "pid": 222, "create_time": 1700000001.0, "starttime": None}
        ),
    }
    base.update(overrides)
    return base


def _runtime(expected: ExpectedUnitWriters, **overrides: object) -> BootstrapRuntimeIdentity:
    base: dict[str, object] = {
        "process": ExpectedProcess(pid=222, create_time=1700000001.0),
        "module": f"{expected.home}/releases/{ARTIFACT}/venv/bin/python",
        "home": expected.home,
        "artifact_digest": expected.artifact_digest,
        "manifest_digest": expected.manifest_digest,
    }
    base.update(overrides)
    return BootstrapRuntimeIdentity.model_validate(base)


def _observation_dict(
    expected: ExpectedUnitWriters,
    runtime: BootstrapRuntimeIdentity | None = None,
    **overrides: object,
) -> dict[str, object]:
    base: dict[str, object] = {
        "mode": "bootstrap_observation",
        "full_ready": False,
        "challenge": str(CHALLENGE),
        "observer_instance": str(uuid4()),
        "unit": expected.unit().model_dump(mode="json"),
        "observed_at": OBSERVED_AT.isoformat(),
        "processes": ["exited"],
        "sessions": ["absent"],
        "launchers": [
            LauncherObservation(
                kind="crontab", definition="absent", loaded=None, enabled=False, current_digest=None
            ).model_dump(mode="json")
        ],
        "closure": "unknown",
        "runtime": (runtime if runtime is not None else _runtime(expected)).model_dump(mode="json"),
    }
    base.update(overrides)
    return base


class _World(NamedTuple):
    unit: CollectorUnitInput
    expected: ExpectedUnitWriters
    observation: dict[str, object]
    ledger: dict[str, object]


def _world() -> _World:
    unit = _unit_input()
    expected = _expected()
    return _World(unit, expected, _observation_dict(expected), _ledger_dict(unit))


def _accept(world: _World, *, coordinator: str = "coord") -> ManagedUnitClosure:
    return accept_unit(
        world.unit,
        world.expected,
        world.observation,
        world.ledger,
        operation=_operation(),
        challenge=CHALLENGE,
        valid_until=VALID_UNTIL,
        coordinator_machine=coordinator,
    )


def _collector(*units: CollectorUnitInput) -> CollectorInput:
    return CollectorInput(
        operation=_operation(),
        challenge=CHALLENGE,
        valid_until=VALID_UNTIL,
        candidate_digest=CANDIDATE_DIGEST,
        units=units if units else (_unit_input(),),
    )


def _registration(
    monkeypatch: pytest.MonkeyPatch, valid_until: datetime | None, digest: str | None = None
) -> None:
    def registered(_operation: RolloutIdentity) -> JournaledRegistration:
        return JournaledRegistration(valid_until=valid_until, plan_digest=digest)

    monkeypatch.setattr(collector_mod, "read_journaled_registration", registered)


def _fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        collector_mod.settings.gateway, "update_managed_writer_hop_poll_seconds", 0.001
    )
    monkeypatch.setattr(
        collector_mod.settings.gateway, "update_managed_writer_pull_timeout_seconds", 0.5
    )


def _pulls(observation: dict[str, object], ledger: dict[str, object]) -> Any:
    def pull(
        _unit: CollectorUnitInput,
        endpoint: str,
        *,
        challenge: UUID,
        timeout: float,
        what: str,
    ) -> dict[str, object]:
        assert challenge == CHALLENGE
        return observation if endpoint == "bootstrap-observation" else ledger

    return pull


class _FakeTransaction:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_exc: object) -> bool:
        return False


# ── the import discipline ────────────────────────────────────────────────────


def test_the_named_modules_resolve() -> None:
    for name in _MUST_BE_LAZY:
        assert importlib.util.find_spec(name) is not None, (
            f"{name} no longer resolves -- the closure probe has gone vacuous"
        )


def test_the_collector_stays_out_of_the_eager_cli_closure() -> None:
    """The collector is heavy; the updater's pre-checkout closure must not reach it."""
    probe = textwrap.dedent(f"""
        import sys

        for name in {["cli.commands", "cli.commands._update_agent_runner"]!r}:
            __import__(name)

        for name in {list(_MUST_BE_LAZY)!r}:
            if name in sys.modules:
                print(name)
    """)
    proc = subprocess.run(  # noqa: S603 -- fixed argv, sys.executable is trusted
        [sys.executable, "-c", probe],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        env=dict(os.environ),
        check=False,
    )
    assert proc.returncode == 0, f"import probe failed:\n{proc.stdout}\n{proc.stderr}"
    assert proc.stdout == ""


# ── the journal registration read ────────────────────────────────────────────


class _RegistrationConn:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row

    def execute(self, _sql: str) -> _RegistrationConn:
        return self

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row

    def __enter__(self) -> _RegistrationConn:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _registration_row(
    monkeypatch: pytest.MonkeyPatch, row: tuple[object, ...] | None
) -> JournaledRegistration:
    def connect(**_kwargs: object) -> _RegistrationConn:
        return _RegistrationConn(row)

    monkeypatch.setattr(collector_mod, "db_connect", connect)
    return read_journaled_registration(_operation())


def _pending_payload(
    operation: RolloutIdentity, *, valid_until: datetime | None, plan: str | None
) -> dict[str, object]:
    pending = PendingPublication(
        operation=operation,
        predecessor=None,
        candidate_digest=CANDIDATE_DIGEST,
        challenge=CHALLENGE,
        units=(
            PublishedUnit(
                machine="runner-a",
                home="/ava-a",
                inventory_digest="1" * 64,
                prepared_receipt_digest="2" * 64,
                artifact_digest="3" * 64,
                manifest_digest="4" * 64,
            ),
        ),
        valid_until=valid_until,
        plan_digest=plan,
    )
    return WriterPublication(current=None, pending=pending).model_dump(mode="json")


def test_registration_reads_refuse_absent_state(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _registration_row(monkeypatch, None) == JournaledRegistration(None, None)
    assert _registration_row(monkeypatch, (None,)) == JournaledRegistration(None, None)


def test_registration_ignores_another_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    other = RolloutIdentity(holder="gateway:other", acquired_at=T0, target_sha="0" * 40)
    row = (_pending_payload(other, valid_until=VALID_UNTIL, plan=EVENT_DIGEST),)
    assert _registration_row(monkeypatch, row) == JournaledRegistration(None, None)


def test_registration_returns_the_matching_journaled_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = (_pending_payload(_operation(), valid_until=VALID_UNTIL, plan=EVENT_DIGEST),)
    assert _registration_row(monkeypatch, row) == JournaledRegistration(VALID_UNTIL, EVENT_DIGEST)


def test_registration_carries_a_legacy_journals_empty_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = (_pending_payload(_operation(), valid_until=None, plan=None),)
    assert _registration_row(monkeypatch, row) == JournaledRegistration(None, None)


# ── the window source ────────────────────────────────────────────────────────


def test_window_prefers_the_journaled_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    journaled = NOW + timedelta(minutes=30)
    _registration(monkeypatch, journaled, EVENT_DIGEST)
    assert collector_mod._window_and_plan(_collector()) == (journaled, EVENT_DIGEST)


def test_window_falls_back_to_the_sealed_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    _registration(monkeypatch, None)
    assert collector_mod._window_and_plan(_collector()) == (VALID_UNTIL, None)


def test_window_refuses_without_any_source(monkeypatch: pytest.MonkeyPatch) -> None:
    _registration(monkeypatch, None)
    empty = CollectorInput(
        operation=_operation(),
        challenge=CHALLENGE,
        valid_until=VALID_UNTIL,
        candidate_digest=CANDIDATE_DIGEST,
        units=(),
    )
    with pytest.raises(CollectorRefusal, match="no observation window is available"):
        collector_mod._window_and_plan(empty)


# ── the hop wait ─────────────────────────────────────────────────────────────


def test_wait_returns_on_candidate_ready(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fast_polls(monkeypatch)
    _registration(monkeypatch, VALID_UNTIL)
    unit = _unit_input()
    monkeypatch.setattr(
        collector_mod, "_pull", _pulls({}, _ledger_dict(unit, {"stage": "candidate_ready"}))
    )

    assert wait_for_candidate_ready(_collector(unit)) is None

    out = capsys.readouterr().out
    assert "runner-a candidate_ready" in out
    assert "all 1 units candidate-ready" in out


def test_wait_prints_each_stage_change(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fast_polls(monkeypatch)
    _registration(monkeypatch, VALID_UNTIL)
    unit = _unit_input()
    responses = iter(
        [
            _ledger_dict(unit, {"stage": "cron_quiesced"}),
            _ledger_dict(unit, {"stage": "candidate_starting"}),
            _ledger_dict(unit, {"stage": "candidate_ready"}),
        ]
    )

    def pull(*_args: object, **_kwargs: object) -> dict[str, object]:
        return next(responses)

    monkeypatch.setattr(collector_mod, "_pull", pull)

    assert wait_for_candidate_ready(_collector(unit)) is None

    out = capsys.readouterr().out
    assert "runner-a cron_quiesced" in out
    assert "runner-a candidate_starting" in out
    assert "runner-a candidate_ready" in out


def test_wait_reports_a_recovered_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_polls(monkeypatch)
    _registration(monkeypatch, VALID_UNTIL)
    unit = _unit_input()
    monkeypatch.setattr(
        collector_mod, "_pull", _pulls({}, _ledger_dict(unit, {"stage": "recovered"}))
    )

    detail = wait_for_candidate_ready(_collector(unit))

    assert detail is not None
    assert "runner-a recovered its predecessor" in detail
    assert "candidate-ready" in detail


def test_wait_returns_the_deadline_detail_when_the_window_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_polls(monkeypatch)
    _registration(monkeypatch, NOW - timedelta(seconds=1))
    attempts: list[int] = []

    def dead(*_args: object, **_kwargs: object) -> dict[str, object]:
        attempts.append(1)
        return {}

    monkeypatch.setattr(collector_mod, "_pull", dead)

    detail = wait_for_candidate_ready(_collector(_unit_input()))

    assert detail is not None
    assert "hit the sealed window with units not candidate-ready" in detail
    assert "runner-a=not observed" in detail
    assert attempts == [], "a dead window must not be polled"


def test_wait_keeps_polling_through_transfer_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fast_polls(monkeypatch)
    _registration(monkeypatch, datetime.now(UTC) + timedelta(seconds=0.05))
    attempts: list[int] = []

    def flaky(*_args: object, **_kwargs: object) -> dict[str, object]:
        attempts.append(1)
        raise CollectorTransferError("the hop ledger read: unreachable (ConnectError)")

    monkeypatch.setattr(collector_mod, "_pull", flaky)

    detail = wait_for_candidate_ready(_collector(_unit_input()))

    assert detail is not None
    assert "runner-a=the hop ledger read: unreachable (ConnectError)" in detail
    assert len(attempts) >= 2, "a transfer failure must be retried until the deadline"
    assert "unreachable (ConnectError)" in capsys.readouterr().out


def test_wait_reports_a_foreign_ledger_as_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _fast_polls(monkeypatch)
    _registration(monkeypatch, VALID_UNTIL)
    unit = _unit_input()
    monkeypatch.setattr(
        collector_mod, "_pull", _pulls({}, dict(_ledger_dict(unit), mode="bootstrap_hop_ledger_x"))
    )

    detail = wait_for_candidate_ready(_collector(unit))

    assert detail is not None
    assert detail.startswith("the hop wait refused: ")


# ── the unit acceptance ──────────────────────────────────────────────────────


class _Rejection(NamedTuple):
    expected: str
    observation: dict[str, object]
    ledger: dict[str, object]


def _rejection(
    world: _World,
    expected: str,
    *,
    observation: dict[str, object] | None = None,
    ledger: dict[str, object] | None = None,
) -> _Rejection:
    return _Rejection(
        expected,
        observation if observation is not None else world.observation,
        ledger if ledger is not None else world.ledger,
    )


def _rejections() -> list[_Rejection]:
    world = _world()
    unit, expected = world.unit, world.expected
    bad_journal = _journal_dict(unit)
    bad_journal["request_digest"] = "0" * 64
    missing_digest = _journal_dict(unit)
    missing_digest["candidate_context_digest"] = "0" * 64
    recovery_drift = _journal_dict(unit)
    recovery_drift["recovery_context_digest"] = "0" * 64
    inventory_drift = _journal_dict(unit)
    inventory_drift["inventory_digest"] = "0" * 64
    return [
        _rejection(
            world,
            "the observer read is not its wire shape",
            observation={"mode": "bootstrap_observation"},
        ),
        _rejection(world, "another mode", observation=dict(world.observation, mode="other")),
        _rejection(
            world,
            "restricted pre-publication observer",
            observation=dict(world.observation, full_ready=True),
        ),
        _rejection(world, "claimed a closure", observation=dict(world.observation, closure="ok")),
        _rejection(
            world,
            "echoes another challenge",
            observation=dict(world.observation, challenge=str(uuid4())),
        ),
        _rejection(
            world,
            "describes another unit inventory",
            observation=dict(
                world.observation,
                unit=ManagedUnit(
                    machine="runner-a", home="/ava-z", inventory_digest="9" * 64
                ).model_dump(mode="json"),
            ),
        ),
        _rejection(
            world,
            "outside the sealed window",
            observation=dict(
                world.observation, observed_at=(T0 - timedelta(seconds=1)).isoformat()
            ),
        ),
        _rejection(
            world,
            "outside the sealed window",
            observation=dict(world.observation, observed_at=VALID_UNTIL.isoformat()),
        ),
        _rejection(
            world,
            "another image identity",
            observation=dict(
                world.observation,
                runtime=_runtime(expected, manifest_digest="f" * 64).model_dump(mode="json"),
            ),
        ),
        _rejection(
            world,
            "not loaded from its prepared image",
            observation=dict(
                world.observation,
                runtime=_runtime(expected, module="/elsewhere/venv/bin/python").model_dump(
                    mode="json"
                ),
            ),
        ),
        _rejection(
            world,
            "not loaded from its prepared image",
            observation=dict(
                world.observation,
                runtime=_runtime(
                    expected,
                    module=(
                        f"{expected.home}/releases/{ARTIFACT}/venv/../../../../outside/bin/python"
                    ),
                ).model_dump(mode="json"),
            ),
        ),
        _rejection(world, "another read mode", ledger=dict(world.ledger, mode="ledger_x")),
        _rejection(
            world,
            "echoes another challenge",
            ledger=dict(world.ledger, challenge=str(uuid4())),
        ),
        _rejection(
            world,
            "no readable hop journal",
            ledger=dict(world.ledger, journal_present=False, journal_readable=False),
        ),
        _rejection(
            world, "no readable hop journal", ledger=dict(world.ledger, journal_readable=False)
        ),
        _rejection(world, "no boot identity", ledger=dict(world.ledger, boot_id=None)),
        _rejection(
            world,
            "does not prove its process identity",
            ledger=dict(
                world.ledger,
                session_record={
                    "state": "absent",
                    "pid": None,
                    "create_time": None,
                    "starttime": None,
                },
            ),
        ),
        _rejection(
            world,
            "not the observer process",
            ledger=dict(
                world.ledger,
                session_record={
                    "state": "ok",
                    "pid": 333,
                    "create_time": 1700000001.0,
                    "starttime": None,
                },
            ),
        ),
        _rejection(
            world,
            "envelope version is unsupported",
            ledger=dict(world.ledger, version=2),
        ),
        _rejection(
            world,
            "envelope is incomplete",
            ledger=dict(world.ledger, generation=None, payload_digest=None),
        ),
        _rejection(
            world,
            "do not recompute their payload digest",
            ledger=dict(world.ledger, payload_digest="0" * 64),
        ),
        _rejection(
            world,
            "does not parse as its schema",
            ledger=_ledger_dict(unit, {"stage": "candidate_ready"}),
        ),
        _rejection(
            world,
            "not at candidate-ready",
            ledger=_ledger_dict(unit, _journal_dict(unit, stage="old_stopped")),
        ),
        _rejection(
            world,
            "normal plan does not match the sealed dispatch",
            ledger=_ledger_dict(unit, _journal_dict(unit, planned=True)),
        ),
        _rejection(world, "hop request does not match", ledger=_ledger_dict(unit, bad_journal)),
        _rejection(
            world, "candidate context does not match", ledger=_ledger_dict(unit, missing_digest)
        ),
        _rejection(
            world, "recovery context does not match", ledger=_ledger_dict(unit, recovery_drift)
        ),
        _rejection(
            world, "inventory receipt does not match", ledger=_ledger_dict(unit, inventory_drift)
        ),
        _rejection(
            world,
            "do not meet a positive closure",
            observation=dict(world.observation, processes=["alive"]),
        ),
    ]


def test_accept_refuses_every_binding_drift() -> None:
    rejects = _rejections()
    assert len(rejects) >= 26
    for rejection in rejects:
        world = _World(_unit_input(), _expected(), rejection.observation, rejection.ledger)
        with pytest.raises(CollectorRefusal, match=rejection.expected):
            _accept(world)


def test_accept_runs_the_local_identity_checks_for_the_coordinators_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psutil

    from shared.proc_tree import stable_create_time
    from shared.session_record import pid_starttime_ticks

    home = str(tmp_path.resolve())
    module_dir = tmp_path.resolve() / "releases" / ARTIFACT / "venv" / "bin"
    module_dir.mkdir(parents=True)
    module = module_dir / "python"
    module.write_text("")
    process = psutil.Process()
    live = ExpectedProcess(
        pid=process.pid,
        create_time=stable_create_time(process),
        starttime=pid_starttime_ticks(process.pid),
    )
    unit = _unit_input("coord", home)
    expected = _expected("coord", home)
    observation = _observation_dict(expected, _runtime(expected, process=live, module=str(module)))
    ledger = _ledger_dict(
        unit,
        session_record={
            "state": "ok",
            "pid": live.pid,
            "create_time": live.create_time,
            "starttime": live.starttime,
        },
    )

    closure = _accept(_World(unit, expected, observation, ledger), coordinator="coord")

    assert closure.outcome == "old_writers_absent_relaunchers_fenced"

    # The same unit with an unresolvable module path refuses locally.
    missing = _observation_dict(
        expected,
        _runtime(expected, process=live, module=f"{home}/releases/{ARTIFACT}/venv/bin/absent"),
    )
    with pytest.raises(CollectorRefusal, match="cannot be resolved locally"):
        _accept(_World(unit, expected, missing, ledger), coordinator="coord")

    # A dead observer process refuses too.
    dead = _observation_dict(
        expected,
        _runtime(
            expected,
            process=ExpectedProcess(pid=99999999, create_time=1700000000.0),
            module=str(module),
        ),
    )
    dead_ledger = _ledger_dict(
        unit,
        session_record={
            "state": "ok",
            "pid": 99999999,
            "create_time": 1700000000.0,
            "starttime": None,
        },
    )
    with pytest.raises(CollectorRefusal, match="observer process is not alive"):
        _accept(_World(unit, expected, dead, dead_ledger), coordinator="coord")


# ── the collection and adoption ──────────────────────────────────────────────


def test_collect_and_adopt_reaches_the_seat_with_one_closure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fast_polls(monkeypatch)
    world = _world()
    _registration(monkeypatch, VALID_UNTIL, EVENT_DIGEST)
    monkeypatch.setattr(collector_mod, "_pull", _pulls(world.observation, world.ledger))
    adopted: list[Any] = []

    def adopt(_conn: object, collection: object) -> None:
        adopted.append(collection)

    monkeypatch.setattr(collector_mod, "adopt_pending_collection", adopt)
    monkeypatch.setattr(collector_mod, "write_transaction", _FakeTransaction)

    assert collect_and_adopt(_collector(world.unit)) == 0

    assert len(adopted) == 1
    collection = adopted[0]
    assert collection.operation == _operation()
    assert collection.challenge == CHALLENGE
    assert collection.candidate_digest == CANDIDATE_DIGEST
    assert [entry.unit.home for entry in collection.units] == ["/ava-a"]
    out = capsys.readouterr().out
    assert f"window V={VALID_UNTIL.isoformat()}" in out
    assert f"plan_digest={EVENT_DIGEST}" in out
    assert "runner-a: positive closure assembled" in out
    assert "collection adopted (1 units)" in out


def test_collect_refuses_on_a_unit_transfer_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fast_polls(monkeypatch)
    _registration(monkeypatch, VALID_UNTIL)
    calls: list[int] = []

    def flaky(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append(1)
        raise CollectorTransferError("the observer read: unreachable (ConnectError)")

    monkeypatch.setattr(collector_mod, "_pull", flaky)
    adopted: list[Any] = []

    def adopt(_conn: object, collection: object) -> None:
        adopted.append(collection)

    monkeypatch.setattr(collector_mod, "adopt_pending_collection", adopt)
    monkeypatch.setattr(collector_mod, "write_transaction", _FakeTransaction)

    assert collect_and_adopt(_collector(_unit_input())) == 1

    assert calls == [1]
    assert adopted == []
    err = capsys.readouterr().err
    assert "\u2717 runner-a: the observer read: unreachable (ConnectError)" in err


def test_collect_refuses_without_a_window_source(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fast_polls(monkeypatch)
    _registration(monkeypatch, None)

    assert (
        collect_and_adopt(
            _collector(
                CollectorUnitInput(
                    machine="runner-a",
                    home="/ava-a",
                    ops_url=None,
                    candidate_context=b"not a context",
                    request=b"{}",
                    recovery_context=b"{}",
                    normal_request=None,
                    prepared_receipt_digest=RECEIPT_DIGEST,
                )
            )
        )
        == 1
    )

    assert "no observation window is available" in capsys.readouterr().err


def test_collect_reports_a_seat_refusal_with_checked_recovery(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fast_polls(monkeypatch)
    world = _world()
    _registration(monkeypatch, VALID_UNTIL)
    monkeypatch.setattr(collector_mod, "_pull", _pulls(world.observation, world.ledger))

    def refuse(_conn: object, _collection: object) -> None:
        raise ManagedWriterBarrierError(
            "existing managed writer evidence requires explicit retirement"
        )

    monkeypatch.setattr(collector_mod, "adopt_pending_collection", refuse)
    monkeypatch.setattr(collector_mod, "write_transaction", _FakeTransaction)

    assert collect_and_adopt(_collector(world.unit)) == 1

    err = capsys.readouterr().err
    assert "managed-writer collect refused" in err
    assert "requires explicit retirement" in err
    assert "recover-pending" in err


# ── the wire reads ───────────────────────────────────────────────────────────


class _Response:
    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content


def _stub_post(monkeypatch: pytest.MonkeyPatch, responder: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> _Response:
        calls.append({"url": url, **kwargs})
        return responder(url, kwargs)

    monkeypatch.setattr(collector_mod.http_dial, "post", post)
    return calls


def test_pull_posts_the_challenge_envelope_with_the_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(collector_mod.settings.data_plane, "cluster_secret", "s3cret")
    calls = _stub_post(monkeypatch, lambda _url, _kw: _Response(200, b'{"mode":"x"}'))

    payload = collector_mod._pull(
        _unit_input(),
        "bootstrap-hop-ledger",
        challenge=CHALLENGE,
        timeout=3.5,
        what="the hop ledger read",
    )

    assert payload == {"mode": "x"}
    assert calls[0]["url"] == "http://runner-a:9/ops/bootstrap-hop-ledger"
    assert calls[0]["content"] == json.dumps({"challenge": str(CHALLENGE)}).encode()
    assert calls[0]["headers"]["Authorization"] == "Bearer s3cret"
    assert calls[0]["timeout"] == 3.5


def test_pull_omits_the_bearer_when_no_secret_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(collector_mod.settings.data_plane, "cluster_secret", "")
    calls = _stub_post(monkeypatch, lambda _url, _kw: _Response(200, b"{}"))

    collector_mod._pull(
        _unit_input(),
        "bootstrap-observation",
        challenge=CHALLENGE,
        timeout=1.0,
        what="the observer read",
    )

    assert "Authorization" not in calls[0]["headers"]


def test_pull_leaves_the_units_trailing_slash_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_post(monkeypatch, lambda _url, _kw: _Response(200, b"{}"))
    unit = _unit_input(ops_url="http://runner-a:9/")

    collector_mod._pull(unit, "bootstrap-observation", challenge=CHALLENGE, timeout=1.0, what="w")

    assert calls[0]["url"] == "http://runner-a:9/ops/bootstrap-observation"


def test_pull_refuses_on_a_409_challenge_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_post(monkeypatch, lambda _url, _kw: _Response(409, b'{"error":"unknown"}'))

    with pytest.raises(CollectorRefusal, match="refused the challenge"):
        collector_mod._pull(
            _unit_input(),
            "bootstrap-observation",
            challenge=CHALLENGE,
            timeout=1.0,
            what="the observer read",
        )


def test_pull_reports_other_statuses_as_transfers(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_post(monkeypatch, lambda _url, _kw: _Response(503, b'{"error":"down"}'))

    with pytest.raises(CollectorTransferError, match="answered HTTP 503"):
        collector_mod._pull(
            _unit_input(),
            "bootstrap-observation",
            challenge=CHALLENGE,
            timeout=1.0,
            what="the observer read",
        )


def test_pull_refuses_an_oversized_or_non_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_post(
        monkeypatch,
        lambda _url, _kw: _Response(200, b"x" * (collector_mod._MAX_PULL_BODY_BYTES + 1)),
    )
    with pytest.raises(CollectorTransferError, match="exceeds its read bound"):
        collector_mod._pull(
            _unit_input(), "bootstrap-observation", challenge=CHALLENGE, timeout=1.0, what="w"
        )

    _stub_post(monkeypatch, lambda _url, _kw: _Response(200, b"<html>no</html>"))
    with pytest.raises(CollectorTransferError, match="not a JSON object"):
        collector_mod._pull(
            _unit_input(), "bootstrap-observation", challenge=CHALLENGE, timeout=1.0, what="w"
        )

    _stub_post(monkeypatch, lambda _url, _kw: _Response(200, b"[1, 2]"))
    with pytest.raises(CollectorTransferError, match="not a JSON object"):
        collector_mod._pull(
            _unit_input(), "bootstrap-observation", challenge=CHALLENGE, timeout=1.0, what="w"
        )


def test_pull_reports_an_unreachable_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(_url: str, **_kwargs: Any) -> _Response:
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(collector_mod.http_dial, "post", explode)

    with pytest.raises(CollectorTransferError, match=r"unreachable \(ConnectError\)"):
        collector_mod._pull(
            _unit_input(), "bootstrap-observation", challenge=CHALLENGE, timeout=1.0, what="w"
        )


def test_unit_base_url_looks_up_a_missing_ops_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def lookup(name: str) -> str:
        return f"http://{name}:7/"

    monkeypatch.setattr(collector_mod.machines, "lookup", lookup)

    assert collector_mod._unit_base_url(_unit_input(ops_url=None)) == "http://runner-a:7"


def test_unit_base_url_refuses_an_unregistered_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_name: str) -> str:
        raise collector_mod.machines.MachineNotRegistered("no machine named 'runner-a'")

    monkeypatch.setattr(collector_mod.machines, "lookup", missing)

    with pytest.raises(CollectorRefusal, match="no ops address is known for runner-a"):
        collector_mod._unit_base_url(_unit_input(ops_url=None))
