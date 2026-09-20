"""The standalone death-continuation entry (#4117 S4).

Companion to ``test_release_normal.py`` (which drives the checked chain): these
tests drive ``cli.commands._update_normal_release_standalone`` — the late-stage
preparation predicate (already-stopped bootstrap, an advanced selector pointer,
dead lineage handoff owners) and the lock/claim/clear order of the entry. The
claim test runs the real ``updater_handoff`` writer against a unit-local
``$AVA_HOME``, so the ownership CAS that a deep-crash recovery depends on is
exercised for real instead of mocked.
"""

import hashlib
import json
import os
import subprocess
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import psutil
import pytest
from pydantic import SecretStr

from cli.commands import _managed_writer_mode as mode_mod
from cli.commands import _update_normal_release as normal
from cli.commands import _update_normal_release_standalone as standalone
from cli.commands._release_selector import read_selector, selector_bytes
from cli.commands._release_services import PreparedService
from cli.commands._update_bootstrap import BootstrapHopRequest
from services.agent_ops.bootstrap import ObserverProjection, PreparedObservation
from shared import spawn_receipt, ui_update_state, updater_handoff
from shared.cluster import session_name
from shared.managed_writer_observation import ExpectedProcess, ProcessVerdict
from shared.managed_writer_publication import PublishedUnit
from shared.proc_tree import stable_create_time
from shared.runtime_release import ReleaseRejectedError
from shared.session_record import SessionRecord
from shared.updater_recovery import NormalReleaseRecoveryJournal
from tests.cli.test_release_normal import (
    GENERATION,
    _attempt_for,
    _await_birth_stub,
    _birth_receipt,
    _ChainRig,
    _context,
    _journal,
    _journal_for,
    _observe_as,
    _prepared_plan,
    _prepared_service,
    _published_unit,
    _record_for,
    _seed_environment,
    _two_service_setup,
    _write_ops_record,
)
from tests.cli.test_release_normal import (
    unit_home as unit_home,
)

_PREVIOUS_SELECTOR = "previous-release\n"
_BOOTSTRAP_PID = 4242


def _dead_pid() -> int:
    """A PID with positive death evidence (spawned, then fully reaped)."""
    child = subprocess.Popen(["/usr/bin/true"])
    pid = child.pid
    child.wait()
    return pid


def _seed_standalone(
    home: Path,
    *,
    predecessor: ExpectedProcess,
    owner_pid: int,
    owner_create_time: float,
    expected_session: str,
    selector_kind: str = "previous",
) -> tuple[Path, PublishedUnit, PreparedObservation]:
    """One unit home seeded exactly as the standalone preparation reads it.

    Everything the preparation private-references lives under ``run/`` at mode
    0600 (``_private_reference`` discipline); the envelope digests cover the
    exact seeded bytes.
    """
    run = home / "run"
    run.mkdir(parents=True, exist_ok=True)
    (home / "releases").mkdir(parents=True, exist_ok=True)
    unit = _published_unit(home)
    context = _context(home, uuid4(), datetime.now(UTC) + timedelta(minutes=10))
    request_path = run / "normal-request.json"
    context_path = run / "context.json"
    inventory_path = run / "inventory.json"
    recovery_path = run / "recovery.json"
    bootstrap_path = run / "bootstrap.json"

    context_path.write_text(context.model_dump_json())
    inventory_path.write_text("inventory")
    recovery_path.write_text("recovery")
    request = normal.NormalReleaseRequest(
        context_path=str(context_path),
        unit=unit,
        previous_selector=_PREVIOUS_SELECTOR,
        predecessor=predecessor,
    )
    request_path.write_text(request.model_dump_json())
    bootstrap_request = BootstrapHopRequest(
        candidate_context=str(context_path),
        recovery_context=str(recovery_path),
        inventory_receipt=str(inventory_path),
        predecessor=predecessor,
        normal_release_path=str(request_path),
    )
    bootstrap_path.write_text(bootstrap_request.model_dump_json())

    selector = selector_bytes(unit) if selector_kind == "prepared" else _PREVIOUS_SELECTOR.encode()
    (home / "releases" / "current-release").write_bytes(selector)

    (run / "updater-handoff.json").write_text(
        json.dumps(
            {
                "generation": GENERATION,
                "expected_session": expected_session,
                "phase": "running",
                "created_at": "2026-09-20T00:00:00+00:00",
                "expires_at": "2030-01-01T00:00:00+00:00",
                "owner_pid": owner_pid,
                "owner_create_time": owner_create_time,
            }
        )
    )
    (run / "updater-bootstrap-recovery.json").write_text(
        json.dumps(
            {
                "version": 1,
                "generation": GENERATION,
                "journal": {
                    "request": str(bootstrap_path),
                    "request_digest": hashlib.sha256(bootstrap_path.read_bytes()).hexdigest(),
                    "inventory_digest": hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
                    "candidate_context_digest": hashlib.sha256(
                        context_path.read_bytes()
                    ).hexdigest(),
                    "recovery_context_digest": hashlib.sha256(
                        recovery_path.read_bytes()
                    ).hexdigest(),
                    "normal_release_planned": True,
                    "stage": "candidate_ready",
                    "cron": "",
                    "phases": [
                        {
                            "stage": "candidate_ready",
                            "observed_at": "2026-09-20T00:00:00+00:00",
                            "monotonic_s": 0.0,
                            "pid": _BOOTSTRAP_PID,
                            "elapsed_s": None,
                        }
                    ],
                    "normal_release": None,
                },
            }
        )
    )
    _write_ops_record(
        home,
        SessionRecord(
            pid=_BOOTSTRAP_PID,
            create_time=2.0,
            cmd="exec /image/python -m services.agent_ops.daemon",
            cwd=str(home),
            started_at=1.0,
            starttime=99,
        ),
    )
    for path in (request_path, context_path, inventory_path, recovery_path, bootstrap_path):
        path.chmod(0o600)
    return request_path, unit, context


def _wire_prepare(
    monkeypatch: pytest.MonkeyPatch,
    *,
    predecessor: ExpectedProcess,
    context: PreparedObservation,
    home: Path,
    bootstrap_verdict: ProcessVerdict,
    probes: list[str],
) -> None:
    def observe(expected: ExpectedProcess) -> ProcessVerdict:
        if expected.pid == predecessor.pid:
            return "exited"
        if expected.pid == _BOOTSTRAP_PID:
            return bootstrap_verdict
        pytest.fail(f"unexpected observe target {expected.pid}")

    def read_context(_path: Path) -> PreparedObservation:
        return context

    def probe(
        _loaded: PreparedObservation, _projection: object, *, verified_image: object = None
    ) -> str:
        probes.append("probe")
        return "alive"

    def services(_unit: PublishedUnit, _schema_digest: str) -> tuple[PreparedService, ...]:
        return (_prepared_service(home, "ava-ops"),)

    def skip_operation(_context: PreparedObservation, _projection: object) -> None:
        return None

    def skip_preflight(_plan: normal.PreparedNormalRelease) -> None:
        return None

    def projection() -> ObserverProjection:
        return ObserverProjection(
            db_url=SecretStr("postgresql://unused"),
            cluster_secret=SecretStr("secret"),
            ops_port=1,
        )

    monkeypatch.setattr(standalone.ObserverProjection, "from_environment", projection)
    monkeypatch.setattr(standalone, "observe_process", observe)
    monkeypatch.setattr(standalone, "read_prepared_context", read_context)
    monkeypatch.setattr(standalone, "probe_bootstrap", probe)
    monkeypatch.setattr(standalone, "prepare_normal_services", services)
    monkeypatch.setattr(standalone, "validate_operation", skip_operation)
    monkeypatch.setattr(standalone, "_preflight_pending_plan", skip_preflight)


def _seed_dead_predecessor_case(
    home: Path,
    *,
    owner_pid: int | None = None,
    owner_create_time: float = 1.0,
    expected_session: str | None = None,
    selector_kind: str = "previous",
) -> tuple[ExpectedProcess, Path, PublishedUnit, PreparedObservation]:
    predecessor = ExpectedProcess(pid=_dead_pid(), create_time=1.0)
    request_path, unit, context = _seed_standalone(
        home,
        predecessor=predecessor,
        owner_pid=predecessor.pid if owner_pid is None else owner_pid,
        owner_create_time=owner_create_time,
        expected_session=(
            f"direct-updater:pid{predecessor.pid}" if expected_session is None else expected_session
        ),
        selector_kind=selector_kind,
    )
    return predecessor, request_path, unit, context


def test_prepare_accepts_a_stopped_bootstrap_and_skips_probing(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    predecessor, request_path, _unit, context = _seed_dead_predecessor_case(unit_home)
    probes: list[str] = []
    _wire_prepare(
        monkeypatch,
        predecessor=predecessor,
        context=context,
        home=unit_home,
        bootstrap_verdict="exited",
        probes=probes,
    )

    plan = standalone.prepare_normal_release(request_path)

    assert probes == []
    assert plan.bootstrap is not None
    assert plan.bootstrap.pid == _BOOTSTRAP_PID
    assert plan.resume_generation == GENERATION


def test_prepare_probes_a_live_bootstrap(monkeypatch: pytest.MonkeyPatch, unit_home: Path) -> None:
    predecessor, request_path, _unit, context = _seed_dead_predecessor_case(unit_home)
    probes: list[str] = []
    _wire_prepare(
        monkeypatch,
        predecessor=predecessor,
        context=context,
        home=unit_home,
        bootstrap_verdict="alive",
        probes=probes,
    )

    standalone.prepare_normal_release(request_path)

    assert probes == ["probe"]


def test_prepare_still_refuses_without_a_readable_bootstrap_record(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    predecessor, request_path, _unit, context = _seed_dead_predecessor_case(unit_home)
    (unit_home / "run" / "sessions" / "ava-ops.json").unlink()
    _wire_prepare(
        monkeypatch,
        predecessor=predecessor,
        context=context,
        home=unit_home,
        bootstrap_verdict="exited",
        probes=[],
    )

    with pytest.raises(ReleaseRejectedError, match="no ava-ops session record"):
        standalone.prepare_normal_release(request_path)


def test_prepare_accepts_the_prepared_selector_pointer(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    predecessor, request_path, unit, context = _seed_dead_predecessor_case(
        unit_home, selector_kind="prepared"
    )
    _wire_prepare(
        monkeypatch,
        predecessor=predecessor,
        context=context,
        home=unit_home,
        bootstrap_verdict="exited",
        probes=[],
    )

    plan = standalone.prepare_normal_release(request_path)

    assert read_selector(unit_home) == selector_bytes(unit)
    assert plan.request.unit == unit


def test_prepare_refuses_a_foreign_selector(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    predecessor, request_path, _unit, context = _seed_dead_predecessor_case(unit_home)
    (unit_home / "releases" / "current-release").write_bytes(b"foreign-release\n")
    _wire_prepare(
        monkeypatch,
        predecessor=predecessor,
        context=context,
        home=unit_home,
        bootstrap_verdict="exited",
        probes=[],
    )

    with pytest.raises(ReleaseRejectedError, match="normal selector differs"):
        standalone.prepare_normal_release(request_path)


@pytest.mark.parametrize("owner_case", ["predecessor", "direct-updater", "updater-session"])
def test_prepare_accepts_dead_lineage_handoff_owners(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path, owner_case: str
) -> None:
    if owner_case == "predecessor":
        predecessor, request_path, _unit, context = _seed_dead_predecessor_case(unit_home)
    else:
        other = _dead_pid()
        predecessor, request_path, _unit, context = _seed_dead_predecessor_case(
            unit_home,
            owner_pid=other,
            expected_session=(
                f"direct-updater:pid{other}"
                if owner_case == "direct-updater"
                else session_name("updater")
            ),
        )
    _wire_prepare(
        monkeypatch,
        predecessor=predecessor,
        context=context,
        home=unit_home,
        bootstrap_verdict="exited",
        probes=[],
    )

    standalone.prepare_normal_release(request_path)


def test_prepare_refuses_a_live_handoff_owner(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    predecessor, request_path, _unit, context = _seed_dead_predecessor_case(
        unit_home,
        owner_pid=os.getpid(),
        owner_create_time=stable_create_time(psutil.Process()),
        expected_session=session_name("updater"),
    )
    _wire_prepare(
        monkeypatch,
        predecessor=predecessor,
        context=context,
        home=unit_home,
        bootstrap_verdict="exited",
        probes=[],
    )

    with pytest.raises(ReleaseRejectedError, match="dead lineage owner"):
        standalone.prepare_normal_release(request_path)


def test_prepare_refuses_a_foreign_handoff_owner(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    other = _dead_pid()
    predecessor, request_path, _unit, context = _seed_dead_predecessor_case(
        unit_home,
        owner_pid=other,
        expected_session="win-helper:1",
    )
    _wire_prepare(
        monkeypatch,
        predecessor=predecessor,
        context=context,
        home=unit_home,
        bootstrap_verdict="exited",
        probes=[],
    )

    with pytest.raises(ReleaseRejectedError, match="dead lineage owner"):
        standalone.prepare_normal_release(request_path)


def test_run_normal_release_claims_before_entering_execute(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    """The flipped entry: prepare -> lock -> claim -> execute -> clear."""
    plan = _prepared_plan(unit_home, ())
    order: list[str] = []
    routed: list[tuple[object, str]] = []

    def prepare(_path: Path) -> normal.PreparedNormalRelease:
        order.append("prepare")
        return plan

    def acquire() -> bool:
        order.append("lock")
        return True

    def release() -> None:
        order.append("release")

    def resume(generation: str, *, expected_session: str) -> bool:
        order.append(f"resume:{generation}:{expected_session}")
        return True

    def clear(generation: str) -> bool:
        order.append(f"clear:{generation}")
        return False

    def spy_execute(routed_plan: object, generation: str) -> None:
        order.append("execute")
        routed.append((routed_plan, generation))

    monkeypatch.setattr(standalone, "prepare_normal_release", prepare)
    monkeypatch.setattr(standalone, "try_acquire_updater_lock", acquire)
    monkeypatch.setattr(standalone, "release_updater_lock", release)
    monkeypatch.setattr(updater_handoff, "resume_bootstrap", resume)
    monkeypatch.setattr(updater_handoff, "clear", clear)
    monkeypatch.setattr(ui_update_state, "lifecycle_lock", nullcontext)
    monkeypatch.setattr(standalone, "execute_normal_release", spy_execute)

    standalone.run_normal_release(unit_home / "normal-request.json")

    assert routed == [(plan, GENERATION)]
    assert order == [
        "prepare",
        "lock",
        f"resume:{GENERATION}:direct-updater:pid{os.getpid()}",
        "execute",
        f"clear:{GENERATION}",
        "release",
    ]


def test_run_normal_release_declines_when_another_updater_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    plan = _prepared_plan(unit_home, ())
    calls: list[str] = []

    def prepare(_path: Path) -> normal.PreparedNormalRelease:
        return plan

    def decline() -> bool:
        calls.append("lock")
        return False

    def forbidden(*_args: object, **_kwargs: object) -> None:
        calls.append("side-effect")

    monkeypatch.setattr(standalone, "prepare_normal_release", prepare)
    monkeypatch.setattr(standalone, "try_acquire_updater_lock", decline)
    monkeypatch.setattr(standalone, "release_updater_lock", forbidden)
    monkeypatch.setattr(standalone, "execute_normal_release", forbidden)
    monkeypatch.setattr(updater_handoff, "resume_bootstrap", forbidden)

    with pytest.raises(ReleaseRejectedError, match="another updater holds this unit"):
        standalone.run_normal_release(unit_home / "normal-request.json")
    assert calls == ["lock"]


def test_run_normal_release_does_not_clear_after_a_declined_claim(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    """A declined claim must not drop retained state that was never taken over.

    The ``claimed`` guard is the only thing between a failed re-claim and a
    destructive clear of another owner's retained bootstrap state; the entry
    must fall straight from the declined resume to releasing the host lock.
    """
    plan = _prepared_plan(unit_home, ())
    calls: list[str] = []

    def prepare(_path: Path) -> normal.PreparedNormalRelease:
        return plan

    def acquire() -> bool:
        calls.append("lock")
        return True

    def release() -> None:
        calls.append("release")

    def decline(generation: str, *, expected_session: str) -> bool:
        calls.append("resume")
        return False

    def forbidden_clear(*_args: object, **_kwargs: object) -> None:
        calls.append("clear")

    def forbidden_execute(*_args: object, **_kwargs: object) -> None:
        calls.append("execute")

    monkeypatch.setattr(standalone, "prepare_normal_release", prepare)
    monkeypatch.setattr(standalone, "try_acquire_updater_lock", acquire)
    monkeypatch.setattr(standalone, "release_updater_lock", release)
    monkeypatch.setattr(updater_handoff, "resume_bootstrap", decline)
    monkeypatch.setattr(updater_handoff, "clear", forbidden_clear)
    monkeypatch.setattr(ui_update_state, "lifecycle_lock", nullcontext)
    monkeypatch.setattr(standalone, "execute_normal_release", forbidden_execute)

    with pytest.raises(ReleaseRejectedError, match="could not claim its existing handoff"):
        standalone.run_normal_release(unit_home / "normal-request.json")
    assert calls == ["lock", "resume", "release"]


def test_claim_allows_the_first_journal_cas_for_a_dead_owner(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    _predecessor, request_path, unit, context = _seed_dead_predecessor_case(unit_home)
    journal = NormalReleaseRecoveryJournal(
        request_path=str(request_path),
        operation_context=normal._prepared_recovery(context),
        unit=unit,
        previous_selector=_PREVIOUS_SELECTOR,
        stage="waiting",
    )

    with pytest.raises(ReleaseRejectedError, match="ownership"):
        normal._write_normal_journal(GENERATION, journal)

    assert updater_handoff.resume_bootstrap(
        GENERATION, expected_session=f"direct-updater:pid{os.getpid()}"
    )

    written = normal._write_normal_journal(GENERATION, journal)
    assert written.stage == "waiting"
    stored = _journal(unit_home)
    assert stored is not None
    assert stored.stage == "waiting"


# ── the #4117 S5 flip: activation pin + checked-chain probes (relocated under the 800-line ceiling) ──


def test_normal_activation_enters_the_checked_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flip removed the fence: the entry drives the checked chain directly."""
    driven: list[tuple[object, str]] = []

    def drive(plan: object, generation: str) -> None:
        driven.append((plan, generation))

    monkeypatch.setattr(normal, "_drive_checked_normal_release", drive)
    plan = Mock(spec=normal.PreparedNormalRelease)
    assert normal.execute_normal_release(plan, "generation") == 0
    assert driven == [(plan, "generation")]


def test_checked_chain_reenters_from_waiting_behind_a_written_selector(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    """INJ-3 window: the selector CAS landed but the journal still reads waiting.

    Re-entry re-proves the selector idempotently (the CAS replay) and still
    converges to the same readback without duplicate effects.
    """
    plan, readbacks, selector = _two_service_setup(unit_home)
    journal = _journal_for(plan, "waiting")
    _seed_environment(unit_home, normal_release=journal.model_dump(mode="json"))
    rig = _ChainRig(monkeypatch, unit_home, plan, selector=selector, readbacks=readbacks)

    result = rig.drive()

    assert rig.calls[0] == "migration-receipt"
    assert "selector-cas" in rig.calls
    assert "stop-bootstrap" in rig.calls
    assert result.services == (readbacks["ava-frontend"], readbacks["ava-ops"])
    assert rig.landed == [result]


def test_checked_chain_replays_a_fully_observed_roster_without_extra_effects(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    """INJ-10 window: every service was observed; the journal still reads starting.

    The crash lands before the ``observed`` journal write with the last attempt
    still retained. Re-entry adjudicates that attempt, re-observes both recorded
    services, and converges with zero new spawns and no extra effects.
    """
    plan, readbacks, selector = _two_service_setup(unit_home)
    first, last = plan.services
    attempt = _attempt_for(unit_home, last)
    journal = _journal_for(
        plan, "starting", starting_session=last.identity.session, starting_attempt=attempt
    )
    _seed_environment(unit_home, normal_release=journal.model_dump(mode="json"))
    receipt = _birth_receipt(unit_home, attempt)
    last_record = _record_for(attempt, receipt, last)
    first_attempt = _attempt_for(unit_home, first)
    first_record = _record_for(first_attempt, _birth_receipt(unit_home, first_attempt), first)

    def read_record(_home: Path, session: str) -> SessionRecord | None:
        if session == first.identity.session:
            return first_record
        if session == last.identity.session:
            return last_record
        return None

    monkeypatch.setattr(
        spawn_receipt,
        "await_birth",
        _await_birth_stub(spawn_receipt.SpawnOutcome("spawned_alive", receipt, "alive")),
    )
    monkeypatch.setattr(spawn_receipt, "read_session_record", read_record)
    monkeypatch.setattr(normal, "observe_process", _observe_as("alive"))
    rig = _ChainRig(monkeypatch, unit_home, plan, selector=selector, readbacks=readbacks)

    result = rig.drive()

    assert rig.starts == []
    assert rig.adopted == [("ava-ops", first_record), ("ava-frontend", last_record)]
    assert rig.calls == [
        "selector-cas",
        "ready:ava-ops",
        "ready:ava-frontend",
        "read-readbacks",
        "unit-readback",
    ]
    assert rig.stages == ["observed"]
    assert rig.landed == [result]
    assert result.services == (readbacks["ava-frontend"], readbacks["ava-ops"])
    retained = _journal(unit_home)
    assert retained is not None and retained.stage == "observed" and retained.readback == result


def test_checked_activation_declaration_is_present_in_the_real_module() -> None:
    """The flip (#4117 S5) declares the guard in the module whose state it proves."""
    assert (
        mode_mod._guard_ready("cli.commands._update_normal_release", "CHECKED_ACTIVATION_READY")
        is True
    )
