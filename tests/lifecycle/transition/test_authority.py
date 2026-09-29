"""The release's write-generation records and their orchestration around the ledger.

Real journal, real ledger (`base.cluster.authority.ledger`) and the real
`authority.fence` / `authority.authorize` driven by the fleet coordinator
(a fleet of one, `tests/lifecycle/release_fleet/fakes.py`); only the
catalog and pooler effects are replaced by the ledger transitions they perform
(they run on real PostgreSQL and PgBouncer in
tests/lifecycle/db_authority/test_release_fence.py). A process death is
injected at every durable boundary; continuation must reuse the recorded
generation or hold, never mint twice or skip a fence.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from base.cluster.authority import (
    AuthorityRefusedError,
    ClosureEvidence,
    OperationAuthority,
    VerifiedGeneration,
    activate,
    require_ledger,
)
from base.cluster.authority.ledger import begin_mint, begin_revoke, mark_closed, record_drops
from base.deploy.release.runtime_release import current_pointer
from cli.commands.data_plane import write_generation
from cli.release_fleet.request import FleetRequest
from cli.release_transition import authority
from cli.release_transition.authority_evidence import (
    Fence,
    FenceEvidence,
    GenerationRef,
    Issue,
    require_coherent,
)
from cli.release_transition.journal import Journal, Operation, create, exclusive, read_operation
from cli.release_transition.request import ReleaseRef
from tests.lifecycle.release_fleet.fakes import OffDutyGateway, drive
from tests.lifecycle.transition.phases import (
    advance_to,
    at_phase,
    closed,
    generation,
    seed_active,
    step,
)


class ControllerLost(BaseException):
    """A process death cannot run the executor's exception compensation."""


def _point(home: Path, reference: ReleaseRef) -> None:
    (home / "releases/current-release").write_text(
        json.dumps(
            {
                "artifact_digest": reference.artifact_digest,
                "manifest_digest": reference.manifest_digest,
            }
        )
    )


@pytest.fixture
def request_record(tmp_path: Path) -> FleetRequest:
    home = tmp_path.resolve() / "home"
    home.mkdir(mode=0o700)
    previous = ReleaseRef(
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        schema_digest="c" * 64,
        source_commit="d" * 40,
    )
    candidate = previous.model_copy(update={"artifact_digest": "e" * 64, "source_commit": "9" * 40})
    (home / "releases").mkdir()
    _point(home, previous)
    seed_active(home, 0)
    return FleetRequest(
        id=uuid4(),
        home=str(home),
        registry=str(home.parent / "registry.json"),
        created_at=datetime.now(UTC),
        machine="test",
        previous=previous,
        candidate=candidate,
        executor=candidate,
        configuration_digest="f" * 64,
    )


def _encrypt(name: str, _password: str) -> str:
    return f"SCRAM-SHA-256$4096:c2VlZA==${name}"


@dataclass
class DataPlane:
    """The catalog/pooler effects as the ledger transitions they perform, with
    one optional process death after a named boundary."""

    home: Path
    crash_after: str = ""
    calls: list[str] = field(default_factory=list[str])

    def _boundary(self, name: str) -> None:
        if self.crash_after == name:
            self.crash_after = ""
            raise ControllerLost(name)

    def fence(self, operation_authority: OperationAuthority) -> write_generation.WriteFence:
        self.calls.append("fence")
        self._boundary("fence-effect")
        revoking = begin_revoke(self.home, operation_authority)
        self._boundary("revoked")
        ledger = require_ledger(self.home)
        roles = (
            *(name for entry in ledger.revoked for name in entry.roles),
            ledger.owner,
            *ledger.groups.model_dump().values(),
        )
        closure = ClosureEvidence(roles=tuple(sorted(roles)), terminated=len(revoking), rounds=1)
        mark_closed(self.home, operation_authority, closure)
        self._boundary("closed")
        pending = [entry.number for entry in require_ledger(self.home).revoked if not entry.dropped]
        record_drops(self.home, operation_authority, dict.fromkeys(pending))
        self._boundary("pruned")
        return write_generation.WriteFence(pooler="stopped", closure=closure)

    def admit(self, operation_authority: OperationAuthority) -> object:
        self.calls.append("admit")
        self._boundary("admit-effect")
        ledger = require_ledger(self.home)
        if ledger.active is None:
            pending = begin_mint(self.home, operation_authority, encrypt=_encrypt)
            self._boundary("pending")
            verified = VerifiedGeneration(pending.number, pending.credential_digest, pending.roles)
            activate(self.home, operation_authority, verified)
            self._boundary("active")
        return require_ledger(self.home).active


class Transition(OffDutyGateway):
    """Real fence/authorize; every other phase is a no-op except the selector."""

    def __init__(self, request: FleetRequest, *, fail_start: bool = False) -> None:
        super().__init__(request)
        self.fail_start = fail_start

    def fence(self, journal: Journal) -> None:
        authority.fence(journal)

    def select(self, operation: Operation) -> None:
        _point(self.home, operation.reference)

    def authorize(self, journal: Journal) -> None:
        authority.authorize(journal, journal.operation.reference)

    def start(self, journal: Journal) -> None:
        authority.require_issued(journal.operation)
        if self.fail_start and journal.operation.direction == "candidate":
            raise RuntimeError("candidate readiness failed")


@pytest.fixture
def plane(request_record: FleetRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[DataPlane]:
    fake = DataPlane(Path(request_record.home))
    monkeypatch.setattr(write_generation, "fence_write_generation", fake.fence)
    monkeypatch.setattr(write_generation, "admit_write_generation", fake.admit)
    yield fake


def _drive(request: FleetRequest, **kwargs: bool) -> Operation:
    with exclusive(request.path) as journal:
        drive(journal, Transition(request, **kwargs))
    return read_operation(request.path)


def _summary(operation: Operation) -> tuple[list[tuple[str, int, str]], list[tuple[str, int]]]:
    fences = [(f.direction, f.generation.number, f.state) for f in operation.db_fences]
    issues = [(i.direction, i.number) for i in operation.db_issues]
    return fences, issues


# ── journal records ──────────────────────────────────────────────────────────


def test_fencing_and_authorizing_sit_between_stop_select_and_start(
    request_record: FleetRequest,
) -> None:
    create(request_record)
    with exclusive(request_record.path) as journal:
        advance_to(journal, "stopping")
        with pytest.raises(ValueError, match="invalid release transition"):
            journal.advance("selecting")
        journal.advance("fencing")
        with pytest.raises(ValueError, match="closed fence"):
            journal.advance("selecting")
        step(journal, "selecting")
        with pytest.raises(ValueError, match="invalid release transition"):
            journal.advance("starting")
        journal.advance("authorizing")
        with pytest.raises(ValueError, match="authorized issue"):
            journal.advance("starting")
        step(journal, "starting")


def test_fence_evidence_is_required_exactly_when_closed_and_covers_its_logins() -> None:
    fenced = generation(3)
    with pytest.raises(ValidationError, match="exactly when it is closed"):
        Fence(direction="candidate", generation=fenced, state="closed")
    with pytest.raises(ValidationError, match="exactly when it is closed"):
        Fence.model_validate(closed("candidate", fenced).model_dump() | {"state": "revoking"})
    partial = FenceEvidence(
        roles=(fenced.gateway,), pooler="forced", terminated=2, rounds=1, dropped=False
    )
    with pytest.raises(ValidationError, match="cover the fenced generation"):
        Fence(direction="candidate", generation=fenced, state="closed", evidence=partial)
    with pytest.raises(ValidationError):
        FenceEvidence(
            roles=fenced.roles,
            pooler="stopped",
            terminated=0,
            rounds=0,
            remaining_sessions=1,  # pyright: ignore[reportArgumentType] — the receipt is only ever zero
            dropped=True,
        )


def test_issue_names_its_generation_exactly_when_authorized() -> None:
    selector = ("e" * 64, "b" * 64)
    with pytest.raises(ValidationError, match="exactly when it is authorized"):
        Issue(direction="candidate", number=1, selector=selector, state="authorized")
    with pytest.raises(ValidationError, match="the number its intent recorded"):
        Issue(
            direction="candidate",
            number=1,
            selector=selector,
            state="authorized",
            generation=generation(2),
        )


def test_recovery_fences_exactly_the_candidates_issue_with_a_newer_number(
    request_record: FleetRequest,
) -> None:
    recovered = at_phase("complete", request=request_record, direction="previous")
    assert _summary(recovered) == (
        [("candidate", 0, "closed"), ("previous", 1, "closed")],
        [("candidate", 1), ("previous", 2)],
    )
    dump = recovered.model_dump()
    wrong_fence = closed("previous", generation(0))
    with pytest.raises(ValidationError, match="exactly the candidate's issued generation"):
        Operation.model_validate(dump | {"db_fences": (dump["db_fences"][0], wrong_fence)})
    stale = dump["db_issues"][1] | {"number": 1, "generation": generation(1)}
    with pytest.raises(ValidationError, match="strictly increase"):
        Operation.model_validate(dump | {"db_issues": (dump["db_issues"][0], stale)})
    candidate = at_phase("complete", request=request_record).model_dump()
    with pytest.raises(ValidationError, match="no recovery write authority"):
        Operation.model_validate(candidate | {"db_fences": dump["db_fences"]})


def test_pitr_operations_carry_no_write_generation_records(request_record: FleetRequest) -> None:
    completed = at_phase("complete", request=request_record)
    require_coherent("complete", None, (), ())
    with pytest.raises(ValueError, match="reuses the active write generation"):
        require_coherent("starting", None, completed.db_fences, completed.db_issues)


def test_receipts_are_journaled_only_in_their_phase_and_never_replaced(
    request_record: FleetRequest,
) -> None:
    create(request_record)
    intent = closed("candidate", generation(0)).model_copy(
        update={"state": "revoking", "evidence": None}
    )
    with exclusive(request_record.path) as journal:
        with pytest.raises(ValueError, match="only while fencing"):
            journal.record_fence(intent)
        advance_to(journal, "fencing")
        with pytest.raises(ValueError, match="intent before any closure"):
            journal.record_fence(closed("candidate", generation(0)))
        journal.record_fence(intent)
        assert journal.record_fence(intent) == journal.operation  # exact replay
        with pytest.raises(ValueError, match="cannot change its generation"):
            journal.record_fence(closed("candidate", generation(5)))
        journal.record_fence(closed("candidate", generation(0)))
        other = FenceEvidence(
            roles=generation(0).roles, pooler="forced", terminated=9, rounds=3, dropped=False
        )
        with pytest.raises(ValueError, match="replace its receipt"):
            journal.record_fence(
                Fence(
                    direction="candidate", generation=generation(0), state="closed", evidence=other
                )
            )
        with pytest.raises(ValueError, match="only while authorizing"):
            journal.record_issue(
                Issue(
                    direction="candidate",
                    number=1,
                    selector=request_record.candidate.selector,
                    state="minting",
                )
            )


# ── orchestration against the ledger ─────────────────────────────────────────


def test_release_fences_generation_zero_and_admits_one(
    request_record: FleetRequest, plane: DataPlane
) -> None:
    create(request_record)
    final = _drive(request_record)
    assert final.terminal and final.direction == "candidate"
    assert _summary(final) == ([("candidate", 0, "closed")], [("candidate", 1)])
    fence = final.fence("candidate")
    assert fence is not None and fence.evidence is not None
    assert fence.evidence.pooler == "stopped" and fence.evidence.dropped
    ledger = require_ledger(Path(request_record.home))
    assert ledger.active is not None and ledger.active.number == ledger.counter == 1
    assert [(entry.number, entry.state, entry.dropped) for entry in ledger.revoked] == [
        (0, "closed", True)
    ]
    issue = final.issue("candidate")
    assert issue is not None and issue.generation is not None
    assert issue.generation.credential_digest == ledger.active.credential_digest
    assert plane.calls == ["fence", "admit"]


def test_failed_candidate_is_fenced_before_its_predecessor_gets_a_new_generation(
    request_record: FleetRequest, plane: DataPlane
) -> None:
    create(request_record)
    final = _drive(request_record, fail_start=True)
    assert final.terminal and final.direction == "previous"
    assert _summary(final) == (
        [("candidate", 0, "closed"), ("previous", 1, "closed")],
        [("candidate", 1), ("previous", 2)],
    )
    ledger = require_ledger(Path(request_record.home))
    assert ledger.active is not None and ledger.active.number == 2
    assert ledger.active.origin.direction == "previous"
    assert [(entry.number, entry.state) for entry in ledger.revoked] == [
        (0, "closed"),
        (1, "closed"),
    ]


_FENCE_BOUNDARIES = ["fence-effect", "revoked", "closed", "pruned"]
_ISSUE_BOUNDARIES = ["admit-effect", "pending", "active"]


@pytest.mark.parametrize("boundary", [*_FENCE_BOUNDARIES, *_ISSUE_BOUNDARIES])
def test_process_death_at_each_ledger_boundary_continues_the_recorded_generation(
    request_record: FleetRequest, plane: DataPlane, boundary: str
) -> None:
    create(request_record)
    plane.crash_after = boundary
    with pytest.raises(ControllerLost, match=boundary):
        _drive(request_record)
    interrupted = read_operation(request_record.path)
    phase = "fencing" if boundary in _FENCE_BOUNDARIES else "authorizing"
    assert interrupted.phase == phase and interrupted.error is None
    # Intent was durable before the effect that died.
    if phase == "fencing":
        record = interrupted.fence("candidate")
        assert record is not None and record.state == "revoking"
        assert record.generation.number == 0
    else:
        issue = interrupted.issue("candidate")
        assert issue is not None and issue.state == "minting" and issue.number == 1
    final = _drive(request_record)
    assert final.terminal
    assert _summary(final) == ([("candidate", 0, "closed")], [("candidate", 1)])
    ledger = require_ledger(Path(request_record.home))
    # One mint in total: the continuation reconciled, never allocated again.
    assert ledger.counter == 1 and ledger.active is not None and ledger.active.number == 1


@pytest.mark.parametrize("receipt", ["fence", "issue"])
def test_death_after_a_receipt_does_not_repeat_the_effect(
    request_record: FleetRequest,
    plane: DataPlane,
    monkeypatch: pytest.MonkeyPatch,
    receipt: str,
) -> None:
    create(request_record)
    record = Journal.record_fence if receipt == "fence" else Journal.record_issue
    final_state = "closed" if receipt == "fence" else "authorized"

    def dies_after_receipt(journal: Journal, value: Fence | Issue) -> Operation:
        written = record(journal, value)  # type: ignore[arg-type]
        if value.state == final_state:
            raise ControllerLost(receipt)
        return written

    monkeypatch.setattr(
        Journal, "record_fence" if receipt == "fence" else "record_issue", dies_after_receipt
    )
    with pytest.raises(ControllerLost):
        _drive(request_record)
    monkeypatch.undo()
    monkeypatch.setattr(write_generation, "fence_write_generation", plane.fence)
    monkeypatch.setattr(write_generation, "admit_write_generation", plane.admit)
    final = _drive(request_record)
    assert final.terminal
    assert plane.calls == ["fence", "admit"]
    assert _summary(final) == ([("candidate", 0, "closed")], [("candidate", 1)])


def test_an_uncertain_fence_holds_and_never_selects(
    request_record: FleetRequest, plane: DataPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(request_record)

    def refused(_authority: OperationAuthority) -> None:
        raise AuthorityRefusedError("closure not proven: sessions survived termination")

    monkeypatch.setattr(write_generation, "fence_write_generation", refused)
    with pytest.raises(AuthorityRefusedError, match="closure not proven"):
        _drive(request_record)
    held = read_operation(request_record.path)
    assert held.phase == "fencing" and held.direction == "candidate"
    assert held.error is not None and "sessions survived" in held.error
    assert current_pointer(Path(request_record.home) / "releases") == (
        request_record.previous.selector
    )
    assert plane.calls == []


# ── holds before any effect ──────────────────────────────────────────────────


def test_authorizing_requires_the_selected_target(
    request_record: FleetRequest, plane: DataPlane
) -> None:
    create(request_record)
    with exclusive(request_record.path) as journal:
        advance_to(journal, "fencing")
        authority.fence(journal)
        journal.advance("selecting")
        journal.advance("authorizing")  # the selector still names the predecessor
        with pytest.raises(AuthorityRefusedError, match="selected target"):
            authority.authorize(journal, request_record.candidate)
        assert journal.operation.issue("candidate") is None
    assert plane.calls == ["fence"]
    assert require_ledger(Path(request_record.home)).counter == 0


def test_a_continuation_holds_when_the_ledger_allocates_another_number(
    request_record: FleetRequest, plane: DataPlane
) -> None:
    create(request_record)
    home = Path(request_record.home)
    with exclusive(request_record.path) as journal:
        advance_to(journal, "fencing")
        authority.fence(journal)
        journal.advance("selecting")
        _point(home, request_record.candidate)
        journal.advance("authorizing")
        journal.record_issue(
            Issue(
                direction="candidate",
                number=7,
                selector=request_record.candidate.selector,
                state="minting",
            )
        )
        with pytest.raises(AuthorityRefusedError, match="not the recorded 7"):
            authority.authorize(journal, request_record.candidate)
    assert plane.calls == ["fence"]
    assert require_ledger(home).unrevoked is None


def test_a_foreign_pending_generation_is_never_adopted(
    request_record: FleetRequest, plane: DataPlane
) -> None:
    create(request_record)
    home = Path(request_record.home)
    with exclusive(request_record.path) as journal:
        advance_to(journal, "fencing")
        authority.fence(journal)
        journal.advance("selecting")
        _point(home, request_record.candidate)
        journal.advance("authorizing")
        foreign = OperationAuthority(operation=uuid4(), direction="candidate")
        begin_mint(home, foreign, encrypt=_encrypt)
        with pytest.raises(AuthorityRefusedError, match="not this operation's recorded issue"):
            authority.authorize(journal, request_record.candidate)
        assert journal.operation.issue("candidate") is None
    assert plane.calls == ["fence"]


def test_fencing_refuses_without_exactly_one_admitted_generation(
    request_record: FleetRequest, plane: DataPlane
) -> None:
    create(request_record)
    home = Path(request_record.home)
    begin_revoke(home, OperationAuthority(operation=uuid4(), direction="candidate"))
    with exclusive(request_record.path) as journal:
        advance_to(journal, "fencing")
        with pytest.raises(AuthorityRefusedError, match="exactly one admitted"):
            authority.fence(journal)
        assert journal.operation.fence("candidate") is None
    assert plane.calls == []


def test_a_recorded_fence_target_that_is_no_longer_admitted_holds(
    request_record: FleetRequest, plane: DataPlane
) -> None:
    create(request_record)
    with exclusive(request_record.path) as journal:
        advance_to(journal, "fencing")
        journal.record_fence(
            Fence(direction="candidate", generation=generation(0), state="revoking")
        )
        # The synthetic reference differs from the ledger's generation 0 digest.
        with pytest.raises(AuthorityRefusedError, match="not the recorded fence target"):
            authority.fence(journal)
    assert plane.calls == []


def test_a_closed_receipt_the_ledger_contradicts_holds(
    request_record: FleetRequest, plane: DataPlane
) -> None:
    create(request_record)
    with exclusive(request_record.path) as journal:
        advance_to(journal, "fencing")
        ledger = require_ledger(Path(request_record.home))
        assert ledger.active is not None
        journal.record_fence(
            Fence(
                direction="candidate",
                generation=GenerationRef.of(ledger.active),
                state="revoking",
            )
        )
        journal.record_fence(closed("candidate", GenerationRef.of(ledger.active)))
        with pytest.raises(AuthorityRefusedError, match="does not record write generation 0"):
            authority.fence(journal)
    assert plane.calls == []


def test_start_requires_the_ledger_to_hold_exactly_the_issued_generation(
    request_record: FleetRequest,
) -> None:
    create(request_record)
    home = Path(request_record.home)
    issued = at_phase("starting", request=request_record)
    with pytest.raises(AuthorityRefusedError, match="not the operation's issued generation 1"):
        authority.require_issued(issued)
    # The home's real generation 0 is not the synthetic issue either.
    ledger = require_ledger(home)
    assert ledger.active is not None and ledger.active.number == 0


def test_start_refuses_without_an_authorized_issue() -> None:
    pending = Operation.model_construct(pitr=None, direction="candidate", db_issues=())
    with pytest.raises(RuntimeError, match="no authorized write generation"):
        authority.require_issued(pending)


def test_executor_dials_the_owner_socket_as_the_gateway_group(
    request_record: FleetRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.parse import parse_qs, unquote, urlsplit

    from base import cluster
    from base.cluster.registry import ClusterRecord
    from base.db import connections

    home = Path(request_record.home)
    record = ClusterRecord(
        ports={**cluster.LEGACY_AVA_PORTS, "postgres": 45432},  # type: ignore[typeddict-item]
        gateway_home=str(home),
        created_at="test",
    )
    adopted: list[str] = []

    def registered(_home: Path) -> ClusterRecord:
        return record

    monkeypatch.setattr(cluster, "get_record", registered)
    monkeypatch.setattr(cluster, "db_identity", lambda: "ava")
    monkeypatch.setattr(connections, "adopt_administrator", adopted.append)
    authority.adopt_executor_authority(home)
    (url,) = adopted
    parts = urlsplit(url)
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    assert parts.password is None and parts.path == "/ava"
    assert query["host"].startswith("/") and query["port"] == "45432"
    assert unquote(query["options"]) == "-c role=ava_gateway"


# ── where the executor and the stage bind the generation ─────────────────────


def test_the_executor_adopts_its_administrator_authority_before_any_phase(
    request_record: FleetRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from cli.release_transition import execute
    from cli.release_transition.journal import LINUX

    create(request_record)
    with exclusive(request_record.path) as journal:
        journal.record_launch({"kind": LINUX, "unit": "ava-update.test.service"})
        journal.mark_launch_attempted()
    events: list[str] = []
    # execute() exports the captured home to its own process environment.
    monkeypatch.setattr(execute, "os", SimpleNamespace(environ={}, getpid=lambda: 1))
    monkeypatch.setattr(
        "base.deploy.release.runtime_interpreter.verify_loaded_image", lambda *_a, **_k: None
    )
    monkeypatch.setattr(ReleaseRef, "verify", lambda _self, _home: None)
    monkeypatch.setattr(execute, "_executor_receipt", lambda _launch: {"pid": 1})
    monkeypatch.setattr(
        authority, "adopt_executor_authority", lambda home: events.append(f"adopt {home}")
    )
    monkeypatch.setattr(
        "cli.release_fleet.coordinator.run_coordinator",
        lambda journal: events.append(f"coordinate {journal.operation.phase}"),
    )
    execute.execute(request_record.path)
    assert events == [f"adopt {request_record.home}", "coordinate prepared"]


def test_stage_start_refuses_a_generation_other_than_the_issued_one(
    request_record: FleetRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli.release_transition import stage

    starting = at_phase("starting", request=request_record)  # issues synthetic generation 1

    def unreachable(*_args: object) -> int:
        pytest.fail("a root started under another generation")

    # start_operation exports the captured home into the live environment; the
    # raw-env seam restores this process's own values afterwards.
    monkeypatch.setitem(os.environ, "AVA_HOME", request_record.home)
    monkeypatch.setitem(os.environ, "AVA_CLUSTER_REGISTRY", request_record.registry)
    monkeypatch.setattr(stage, "read_operation", lambda _path: starting)
    monkeypatch.setattr(stage, "_require_inputs", lambda _operation: None)
    monkeypatch.setattr(stage, "_require_native_root_owner", lambda _operation, _home: None)
    monkeypatch.setattr(stage, "start_image", unreachable)
    with pytest.raises(AuthorityRefusedError, match="not the operation's issued generation 1"):
        stage.start_operation(request_record.path)
