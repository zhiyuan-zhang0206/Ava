"""Prepared CLI never silently falls through to old source/gateway dispatch."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from cli import main
from cli.prepared_update import (
    PreparedOperatorInput,
    PreparedOperatorPlan,
    PreparedOperatorUnit,
    prepare_operator_input,
    run_prepared_update,
)
from shared.managed_writer_barrier import (
    ManagedUnit,
    ManagedUnitClosure,
    ManagedWriterBarrierError,
    ManagedWriterCollection,
    RolloutIdentity,
)
from shared.managed_writer_publication import (
    CandidateUnitPlan,
    NormalService,
    NormalStartPlan,
    PendingPublication,
    PreparedDispatch,
    PublishedUnit,
)
from shared.prepared_rollout import PreparedBlockage
from shared.runtime_publication_input import PreparationReceipt
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease


@pytest.mark.parametrize(
    "tail",
    [
        ["--prepared", "/absent"],
        ["--local", "--prepared=/absent", "--force"],
        ["--local", "--prepared=/absent", "--restart-only"],
        ["--local", "--prepared=/absent", "--dry-run"],
        ["--local", "--prepared=/absent", "--mode", "force"],
    ],
)
def test_invalid_combination_refuses_before_plan_or_old_dispatch(
    monkeypatch: pytest.MonkeyPatch, tail: list[str]
) -> None:
    def forbidden(_path: Path) -> None:
        raise AssertionError("invalid flags must not read a plan")

    monkeypatch.setattr("cli.prepared_update.prepare_operator_input", forbidden)
    monkeypatch.setattr("cli.preflight.require_anchored_home", forbidden)
    assert main.main(["cluster", "update", *tail]) == 2


def test_prepared_enters_handler_before_checkout_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def prepared(args: argparse.Namespace) -> int:
        seen.append(args.prepared)
        return 17

    def forbidden(_verb: str) -> None:
        raise AssertionError("wheel entry must not ask an absent checkout for its home")

    monkeypatch.setattr("cli.prepared_update.run_prepared_update", prepared)
    monkeypatch.setattr("cli.preflight.require_anchored_home", forbidden)
    assert main.main(["cluster", "update", "--local", "--prepared", "/private/plan"]) == 17
    assert seen == ["/private/plan"]


def test_source_cannot_impersonate_retained_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cli.prepared_update.WHEEL_RUNTIME", False)
    with pytest.raises(ReleaseRejectedError, match="retained POSIX"):
        prepare_operator_input(Path("/must-not-be-read"))


def test_unknown_prepared_flag_is_not_silently_forwarded() -> None:
    with pytest.raises(SystemExit) as error:
        main.main(["cluster", "update", "--local", "--prepared", "/x", "--permit-ready"])
    assert error.value.code == 2


def test_preparation_error_returns_refusal_without_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(_path: Path) -> None:
        raise ReleaseRejectedError("unknown LKG")

    monkeypatch.setattr("cli.prepared_update.prepare_operator_input", refuse)
    args = main._build_parser().parse_args(["cluster", "update", "--local", "--prepared", "/x"])
    assert run_prepared_update(args) == 2


def test_actual_parser_refuses_without_importing_settings_or_commands(tmp_path: Path) -> None:
    code = """
import importlib.abc
import sys
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'shared.config' or fullname == 'cli.commands':
            raise AssertionError('forbidden early import: ' + fullname)
sys.meta_path.insert(0, Deny())
from cli.main import main
assert main(['cluster', 'update', '--prepared', '/absent']) == 2
assert main(['cluster', 'update', '--local', '--prepared', '/absent']) == 2
"""
    environment = {**os.environ, "HOME": str(tmp_path), "AVA_HOME": str(tmp_path / "unit")}
    result = subprocess.run(  # noqa: S603 — fixed current interpreter and literal import-guard program.
        [sys.executable, "-B", "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


class FakeConnection:
    """Minimal caller-owned transaction seam for prepared CLI dispatch tests."""

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def transaction(self):  # type: ignore[no-untyped-def]
        return nullcontext(self)

    def execute(self, *_args: object) -> None:
        return None


def _prepared_database_url() -> str:
    return "postgresql://prepared-test"


def _prepared_projection() -> SimpleNamespace:
    return SimpleNamespace(db_url=SimpleNamespace(get_secret_value=_prepared_database_url))


def _fake_connect(*_args: object, **_kwargs: object) -> FakeConnection:
    return FakeConnection()


def _require_prepared_dispatch(
    prepared: PreparedOperatorInput,
    operation: RolloutIdentity,
    _conn: object,
    *_args: object,
) -> PendingPublication:
    """Absorb the five positional arguments the real API takes (conn, operation,
    request_id, request_digest, unit) after ``partial`` bound the fixture inputs."""
    return pending_dispatch(prepared, operation)


def _read_prepared_blockage(blockage: PreparedBlockage, _conn: object) -> PreparedBlockage:
    return blockage


def _gateway_machine_name(_path: object) -> bytes:
    return b"gateway\n"


def _prepared_operator_input_stub(
    prepared: PreparedOperatorInput, _path: object
) -> PreparedOperatorInput:
    return prepared


def _machine_name_or_regular_bytes(path: Path) -> bytes:
    if path.name == "machine_name":
        return b"gateway\n"
    return path.read_bytes()


def _ignore_sleep(_seconds: object) -> None:
    return None


def _clock_now(clock: Iterator[datetime]) -> datetime:
    return next(clock)


def prepared_input(
    *, home: Path = Path("/ava"), recovery_collection: str | None = None
) -> PreparedOperatorInput:
    unit = PublishedUnit(
        machine="gateway",
        home=str(home),
        inventory_digest="a" * 64,
        artifact_digest="b" * 64,
        manifest_digest="c" * 64,
    )
    selector = {
        "version": 2,
        "artifact_digest": unit.artifact_digest,
        "manifest_digest": unit.manifest_digest,
        "inventory_receipt_digest": unit.inventory_digest,
    }
    selector_digest = hashlib.sha256(
        (json.dumps(selector, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    normal = NormalStartPlan(
        schema_digest="d" * 64,
        applied_names=("baseline",),
        units=(
            CandidateUnitPlan(
                unit=unit,
                previous_selector_digest=None,
                selector_digest=selector_digest,
                services=(
                    NormalService(
                        session="ava-ops",
                        module="services.agent_ops.daemon",
                        executable=str(
                            home / "releases" / unit.artifact_digest / "venv/bin/python"
                        ),
                        entrypoint=str(home / "releases" / unit.artifact_digest / "venv/ops.py"),
                        command_digest="e" * 64,
                    ),
                ),
            ),
        ),
    )
    request = PreparedOperatorPlan(
        version=1,
        request_id=uuid4(),
        target_sha="1" * 40,
        coordinator=unit,
        units=(
            PreparedOperatorUnit(
                unit=unit,
                recovery=unit.model_copy(
                    update={"artifact_digest": "f" * 64, "manifest_digest": "e" * 64}
                ),
                recovery_schema_digest="d" * 64,
            ),
        ),
        valid_until=datetime.now(UTC) + timedelta(minutes=5),
        normal=normal,
        recovery_collection=recovery_collection,
    )
    root = home / "releases" / unit.artifact_digest
    return PreparedOperatorInput(
        path=home / "run/plan.json",
        digest="7" * 64,
        request=request,
        local=request.units[0],
        image=VerifiedRelease(
            digest=unit.artifact_digest,
            manifest_digest=unit.manifest_digest,
            root=root,
            interpreter=root / "venv/bin/python",
            cwd=root,
        ),
        recovery=cast("VerifiedRelease", SimpleNamespace()),
        receipt=cast("PreparationReceipt", SimpleNamespace(inventory_digest=unit.inventory_digest)),
    )


def pending_dispatch(
    prepared: PreparedOperatorInput, operation: RolloutIdentity
) -> PendingPublication:
    dispatch = PreparedDispatch(
        request_id=prepared.request.request_id,
        request_digest=prepared.digest,
        coordinator=prepared.request.coordinator,
        valid_until=prepared.request.valid_until,
    )
    return PendingPublication(
        operation=operation,
        predecessor=None,
        candidate_digest=prepared.digest,
        challenge=prepared.request.request_id,
        units=tuple(entry.unit for entry in prepared.request.normal.units),
        normal_start_plan=prepared.request.normal,
        dispatch=dispatch,
    )


def participant_input(prepared: PreparedOperatorInput) -> PreparedOperatorInput:
    participant = prepared.local.model_copy(
        update={
            "unit": prepared.local.unit.model_copy(update={"machine": "runner"}),
            "recovery": prepared.local.recovery.model_copy(update={"machine": "runner"}),
        }
    )
    request = prepared.request.model_copy(
        update={
            "units": (prepared.local, participant),
            "normal": prepared.request.normal.model_copy(
                update={
                    "units": (
                        *prepared.request.normal.units,
                        prepared.request.normal.units[0].model_copy(
                            update={"unit": participant.unit}
                        ),
                    )
                }
            ),
        }
    )
    return PreparedOperatorInput(
        prepared.path,
        prepared.digest,
        request,
        participant,
        prepared.image,
        prepared.recovery,
        prepared.receipt,
    )


def install_dispatch_seams(
    monkeypatch: pytest.MonkeyPatch,
    prepared: PreparedOperatorInput,
    operation: RolloutIdentity,
    *,
    create: object,
    bind: object,
    blockage: PreparedBlockage | None = None,
    recover: object | None = None,
) -> dict[str, object]:
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "services.agent_ops.bootstrap.ObserverProjection.from_environment", _prepared_projection
    )
    monkeypatch.setattr("psycopg.connect", _fake_connect)
    monkeypatch.setattr("shared.prepared_rollout.create_prepared_operation", create)
    monkeypatch.setattr("shared.prepared_rollout.bind_prepared_participant", bind)
    monkeypatch.setattr(
        "shared.prepared_rollout.require_prepared_dispatch",
        partial(_require_prepared_dispatch, prepared, operation),
    )

    def record(*args: object) -> None:
        seen["record"] = args

    def barrier(*args: object) -> None:
        seen["barrier"] = args

    monkeypatch.setattr("shared.prepared_rollout.record_prepared_preflight", record)
    monkeypatch.setattr("shared.prepared_rollout.require_all_prepared_preflights", barrier)
    if blockage is not None:
        monkeypatch.setattr(
            "shared.prepared_rollout.read_prepared_blockage",
            partial(_read_prepared_blockage, blockage),
        )
    if recover is not None:
        monkeypatch.setattr("shared.prepared_rollout.recover_prepared_operation", recover)
    return seen


def prepared_args() -> argparse.Namespace:
    return main._build_parser().parse_args(["cluster", "update", "--local", "--prepared", "/x"])


def test_coordinator_dispatch_creates_then_records_all_unit_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepared_input()
    operation = RolloutIdentity(
        holder="prepared:gateway:pid123", acquired_at=datetime.now(UTC), target_sha="1" * 40
    )
    seen: dict[str, object] = {}

    def create(_conn: object, **kwargs: object) -> RolloutIdentity:
        seen["create"] = kwargs
        return operation

    def bind(_conn: object, **_kwargs: object) -> RolloutIdentity:
        return operation

    dispatch_seen = install_dispatch_seams(
        monkeypatch, prepared, operation, create=create, bind=bind
    )
    monkeypatch.setattr("cli.prepared_update.regular_bytes", _gateway_machine_name)
    monkeypatch.setattr(
        "cli.prepared_update.prepare_operator_input",
        partial(_prepared_operator_input_stub, prepared),
    )
    assert run_prepared_update(prepared_args()) == 0
    created = cast(dict[str, object], seen["create"])
    assert isinstance(created, dict)
    assert created["dispatch"] == PreparedDispatch(
        request_id=prepared.request.request_id,
        request_digest=prepared.digest,
        coordinator=prepared.request.coordinator,
        valid_until=prepared.request.valid_until,
    )
    assert created["plan"] == prepared.request.normal
    assert created["target_sha"] == prepared.request.target_sha
    assert str(created["holder"]).startswith("prepared:gateway:pid")
    record = cast(tuple[object, ...], dispatch_seen["record"])
    barrier = cast(tuple[object, ...], dispatch_seen["barrier"])
    assert record[1:3] == (operation, prepared.request.request_id)
    assert barrier[1] == operation


def test_participant_dispatch_never_creates_the_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = participant_input(prepared_input())
    operation = RolloutIdentity(
        holder="prepared:gateway:pid123", acquired_at=datetime.now(UTC), target_sha="1" * 40
    )

    def forbidden(*_args: object, **_kwargs: object) -> RolloutIdentity:
        raise AssertionError("participant must not create a prepared operation")

    def bind(_conn: object, **_kwargs: object) -> RolloutIdentity:
        return operation

    dispatch_seen = install_dispatch_seams(
        monkeypatch, prepared, operation, create=forbidden, bind=bind
    )
    monkeypatch.setattr(
        "cli.prepared_update.prepare_operator_input",
        partial(_prepared_operator_input_stub, prepared),
    )
    assert run_prepared_update(prepared_args()) == 0
    assert "record" in dispatch_seen and "barrier" in dispatch_seen


def test_coordinator_refuses_recovery_without_fresh_collection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared = prepared_input()
    operation = RolloutIdentity(
        holder="old:gateway", acquired_at=datetime.now(UTC), target_sha="0" * 40
    )

    def create(*_args: object, **_kwargs: object) -> RolloutIdentity:
        raise ManagedWriterBarrierError("old operation requires explicit recovery")

    def forbidden_bind(*_args: object, **_kwargs: object) -> RolloutIdentity:
        raise AssertionError("recovery refusal must not bind a participant")

    install_dispatch_seams(
        monkeypatch,
        prepared,
        operation,
        create=create,
        bind=forbidden_bind,
        blockage=PreparedBlockage(
            operation,
            None,
            "updating",
            "old:gateway",
            operation.acquired_at,
            None,
            operation.target_sha,
        ),
    )
    monkeypatch.setattr("cli.prepared_update.regular_bytes", _gateway_machine_name)
    monkeypatch.setattr(
        "cli.prepared_update.prepare_operator_input",
        partial(_prepared_operator_input_stub, prepared),
    )
    assert run_prepared_update(prepared_args()) == 2
    assert "recovery" in capsys.readouterr().err


def test_recovery_uses_collection_bound_predecessor_then_runs_participant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "unit"
    run = home / "run"
    run.mkdir(parents=True)
    collection_path = run / "recovery.json"
    prepared = prepared_input(home=home, recovery_collection=str(collection_path))
    old = RolloutIdentity(holder="old:gateway", acquired_at=datetime.now(UTC), target_sha="0" * 40)
    new = RolloutIdentity(
        holder="prepared:gateway:recovery:1", acquired_at=datetime.now(UTC), target_sha="1" * 40
    )
    collection = ManagedWriterCollection(
        operation=new,
        candidate_digest=prepared.digest,
        challenge=prepared.request.request_id,
        collected_at=new.acquired_at,
        valid_until=prepared.request.valid_until,
        units=(
            ManagedUnitClosure(
                unit=ManagedUnit(
                    machine=prepared.local.unit.machine,
                    home=prepared.local.unit.home,
                    inventory_digest=prepared.local.unit.inventory_digest,
                ),
                boot_id=uuid4(),
                observer_instance=uuid4(),
                observation_digest="9" * 64,
                outcome="old_writers_absent_relaunchers_fenced",
            ),
        ),
    )
    collection_path.write_text(collection.model_dump_json(), encoding="utf-8")
    collection_path.chmod(0o600)
    seen: dict[str, object] = {}

    def create(*_args: object, **_kwargs: object) -> RolloutIdentity:
        raise ManagedWriterBarrierError("old operation requires explicit recovery")

    def bind(_conn: object, **_kwargs: object) -> RolloutIdentity:
        return new

    def recover(_conn: object, **kwargs: object) -> RolloutIdentity:
        seen["recover"] = kwargs
        return new

    install_dispatch_seams(
        monkeypatch,
        prepared,
        new,
        create=create,
        bind=bind,
        blockage=PreparedBlockage(
            old, None, "updating", old.holder, old.acquired_at, None, old.target_sha
        ),
        recover=recover,
    )
    monkeypatch.setattr(
        "cli.prepared_update.regular_bytes",
        _machine_name_or_regular_bytes,
    )
    monkeypatch.setattr(
        "cli.prepared_update.prepare_operator_input",
        partial(_prepared_operator_input_stub, prepared),
    )
    assert run_prepared_update(prepared_args()) == 0
    recovered = cast(dict[str, object], seen["recover"])
    assert recovered["abandoned"] == old
    assert recovered["fresh_collection"] == collection


def test_participant_refuses_after_deadline_while_waiting_to_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = participant_input(prepared_input())
    operation = RolloutIdentity(
        holder="prepared:gateway:pid123", acquired_at=datetime.now(UTC), target_sha="1" * 40
    )

    def forbidden(*_args: object, **_kwargs: object) -> RolloutIdentity:
        raise AssertionError("participant must not create a prepared operation")

    def wait_for_coordinator(*_args: object, **_kwargs: object) -> RolloutIdentity:
        raise ManagedWriterBarrierError("the exact prepared dispatch has not been published")

    install_dispatch_seams(
        monkeypatch, prepared, operation, create=forbidden, bind=wait_for_coordinator
    )
    monkeypatch.setattr(
        "cli.prepared_update.prepare_operator_input",
        partial(_prepared_operator_input_stub, prepared),
    )
    monkeypatch.setattr("cli.prepared_update.time.sleep", _ignore_sleep)
    clock: Iterator[datetime] = iter(
        (
            prepared.request.valid_until - timedelta(seconds=3),
            prepared.request.valid_until - timedelta(seconds=2),
            prepared.request.valid_until,
        )
    )
    monkeypatch.setattr("cli.prepared_update._now", partial(_clock_now, clock))
    assert run_prepared_update(prepared_args()) == 2
