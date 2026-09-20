"""`ops.ops_prepare_facts.cluster_prepare_facts_op` — the relay contract.

The handler must locate the announced image strictly under the unit home, run
exactly one bounded child of the candidate's `cli.prepared_facts` entry with the
explicit minimal projection, and relay the parsed shipment unchanged. Every
refusal (missing image, non-zero child, shipment outside its shape bound)
happens before anything is trusted; the entry's own authority checks are
deliberately not duplicated here.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from ops import ops_prepare_facts
from ops.rpc_prepare_facts import (
    ImageRef,
    PrepareFactsPayload,
    PrepareFactsResult,
    RestrictedHopMaterial,
)
from services.agent_ops.bootstrap import PreparedObservation
from shared.config import settings
from shared.managed_writer_barrier import RolloutIdentity
from shared.managed_writer_observation import (
    ExpectedProcess,
    ExpectedUnitWriters,
    ObservationChallenge,
)
from shared.managed_writer_publication import CandidateUnitPlan, NormalService, PublishedUnit
from shared.runtime_publication_input import PreparationReceipt, PreparedService
from shared.runtime_release import ReleaseRejectedError

ARTIFACT = "a" * 64
MANIFEST = "b" * 64
SCHEMA = "c" * 64
RECOVERY_ARTIFACT = "d" * 64
RECOVERY_MANIFEST = "e" * 64
RECOVERY_SCHEMA = "f" * 64
DB_URL = "postgresql://runner@unit/db"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _payload(artifact_digest: str = ARTIFACT) -> PrepareFactsPayload:
    return PrepareFactsPayload(
        operation=RolloutIdentity(
            holder="gateway:pid1",
            acquired_at=datetime(2026, 9, 20, tzinfo=UTC),
            target_sha="0" * 40,
        ),
        candidate=ImageRef(
            artifact_digest=artifact_digest, manifest_digest=MANIFEST, schema_digest=SCHEMA
        ),
        recovery=ImageRef(
            artifact_digest=RECOVERY_ARTIFACT,
            manifest_digest=RECOVERY_MANIFEST,
            schema_digest=RECOVERY_SCHEMA,
        ),
    )


def _hop_material(home: Path) -> RestrictedHopMaterial:
    """One internally consistent restricted-hop material block (task #4129 I4)."""
    context = PreparedObservation(
        expected=ExpectedUnitWriters(
            machine="runner",
            home=str(home),
            artifact_digest=RECOVERY_ARTIFACT,
            manifest_digest=RECOVERY_MANIFEST,
            processes=(),
            sessions=(),
            launchers=(),
        ),
        operation=RolloutIdentity(
            holder="gateway:pid1",
            acquired_at=datetime(2026, 9, 20, tzinfo=UTC),
            target_sha="0" * 40,
        ),
        challenge=ObservationChallenge(
            challenge=UUID(int=1), valid_until=datetime(2026, 9, 20, 1, tzinfo=UTC)
        ),
        schema_digest=RECOVERY_SCHEMA,
    )
    return RestrictedHopMaterial(
        predecessor=ExpectedProcess(pid=424242, create_time=1700000000.0),
        recovery_context_path=f"{home}/run/hop-recovery-fixture.json",
        recovery_context=context.model_dump_json(),
    )


def _shipment(home: Path) -> PrepareFactsResult:
    """One internally consistent shipment, as the candidate entry would emit it."""
    expected = ExpectedUnitWriters(
        machine="runner",
        home=str(home),
        artifact_digest=ARTIFACT,
        manifest_digest=MANIFEST,
        processes=(),
        sessions=(),
        launchers=(),
    )
    receipt = PreparationReceipt(
        version=1,
        expected=expected,
        services=(PreparedService(session="ava-ops", requires_db=True, gate=None),),
        excluded_registrations=(),
        inventory_digest=expected.unit().inventory_digest,
        closure="unknown",
        unresolved=("writer closure",),
    )
    body = receipt.model_dump_json().encode("ascii")
    unit = PublishedUnit(
        machine="runner",
        home=str(home),
        inventory_digest=expected.unit().inventory_digest,
        prepared_receipt_digest=hashlib.sha256(body).hexdigest(),
        artifact_digest=ARTIFACT,
        manifest_digest=MANIFEST,
    )
    image = f"{home}/releases/{ARTIFACT}"
    selector = {
        "version": 2,
        "artifact_digest": ARTIFACT,
        "manifest_digest": MANIFEST,
        "prepared_receipt_digest": unit.prepared_receipt_digest,
    }
    candidate = CandidateUnitPlan(
        unit=unit,
        services=(
            NormalService(
                session="ava-ops",
                module="services.agent_ops.daemon",
                executable=f"{image}/venv/bin/python",
                entrypoint=f"{image}/venv/services/agent_ops/daemon.py",
                command_digest=_digest("command"),
            ),
        ),
        previous_selector_digest=None,
        selector_digest=hashlib.sha256(
            (json.dumps(selector, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest(),
    )
    return PrepareFactsResult(
        unit=unit,
        receipt_json=body.decode("ascii"),
        candidate=candidate,
        recovery=ImageRef(
            artifact_digest=RECOVERY_ARTIFACT,
            manifest_digest=RECOVERY_MANIFEST,
            schema_digest=RECOVERY_SCHEMA,
        ),
        previous_selector=None,
        hop_material=_hop_material(home),
    )


@pytest.fixture
def unit_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """One canonical unit home with a retained candidate image (no other files)."""
    home = (tmp_path / "unit").resolve()
    interpreter = home / "releases" / ARTIFACT / "venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n")
    monkeypatch.setattr(settings.general, "ava_home", home)
    monkeypatch.setattr(settings.data_plane, "db_url", DB_URL)
    return home


class _ChildRecorder:
    """Stub for `run_bounded`: records each call, replays one canned child."""

    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self._stdout = stdout
        self._stderr = stderr
        self._returncode = returncode

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(
            argv, self._returncode, stdout=self._stdout, stderr=self._stderr
        )


def test_payload_models_roundtrip_on_the_wire_shape() -> None:
    wire = _payload().model_dump(mode="json")
    assert PrepareFactsPayload.model_validate_json(json.dumps(wire)) == _payload()
    with pytest.raises(ValueError):
        PrepareFactsPayload.model_validate_json(json.dumps({**wire, "extra": 1}))


def test_unretained_image_refuses_before_any_child(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _ChildRecorder()
    monkeypatch.setattr(ops_prepare_facts, "run_bounded", child)

    with pytest.raises(ReleaseRejectedError, match="not retained"):
        ops_prepare_facts.cluster_prepare_facts_op(_payload(artifact_digest="9" * 64))

    assert child.calls == []


def test_child_runs_bounded_with_the_explicit_projection(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shipment = _shipment(unit_home)
    child = _ChildRecorder(stdout=json.dumps(shipment.model_dump(mode="json")) + "\n")
    monkeypatch.setattr(ops_prepare_facts, "run_bounded", child)

    result = ops_prepare_facts.cluster_prepare_facts_op(_payload())

    assert result == shipment
    assert len(child.calls) == 1
    call = child.calls[0]
    interpreter = str(unit_home / "releases" / ARTIFACT / "venv" / "bin" / "python")
    assert call["argv"][:7] == [
        interpreter,
        "-I",
        "-B",
        "-X",
        "utf8",
        "-m",
        "cli.prepared_facts",
    ]
    assert call["timeout"] == ops_prepare_facts._PREPARE_FACTS_TIMEOUT_S
    assert call["capture_output"] is True
    assert call["text"] is True
    assert call["cwd"] == str(unit_home)
    assert call["env"] == {
        "PATH": "/usr/bin:/bin",
        "HOME": str(unit_home.parent),
        "AVA_HOME": str(unit_home),
        "AVA_DB_URL": DB_URL,
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def test_child_refusal_surfaces_its_stderr_tail(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _ChildRecorder(stderr="prepared facts refused: unit is not registered\n", returncode=2)
    monkeypatch.setattr(ops_prepare_facts, "run_bounded", child)

    with pytest.raises(ReleaseRejectedError, match="unit is not registered"):
        ops_prepare_facts.cluster_prepare_facts_op(_payload())


def test_child_timeout_translates_to_a_refusal(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout(_argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("child", ops_prepare_facts._PREPARE_FACTS_TIMEOUT_S)

    monkeypatch.setattr(ops_prepare_facts, "run_bounded", timeout)

    with pytest.raises(ReleaseRejectedError, match="timed out"):
        ops_prepare_facts.cluster_prepare_facts_op(_payload())


def test_shipment_beyond_the_relay_bound_refuses(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _ChildRecorder(stdout="x" * (ops_prepare_facts._MAX_SHIPMENT_BYTES + 1))
    monkeypatch.setattr(ops_prepare_facts, "run_bounded", child)

    with pytest.raises(ReleaseRejectedError, match="relay bound"):
        ops_prepare_facts.cluster_prepare_facts_op(_payload())


def test_non_json_shipment_refuses(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    child = _ChildRecorder(stdout="not one json document")
    monkeypatch.setattr(ops_prepare_facts, "run_bounded", child)

    with pytest.raises(ReleaseRejectedError, match="not one JSON document"):
        ops_prepare_facts.cluster_prepare_facts_op(_payload())


def test_invalid_shipment_shape_refuses(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    child = _ChildRecorder(stdout='{"unit": {}}')
    monkeypatch.setattr(ops_prepare_facts, "run_bounded", child)

    with pytest.raises(ReleaseRejectedError, match="shape is invalid"):
        ops_prepare_facts.cluster_prepare_facts_op(_payload())


def test_daemon_dispatch_accepts_the_json_shaped_payload(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The op envelope crosses the wire as JSON, so the dispatch arm must give
    the handler a JSON-mode-validated payload (a python-mode validation refuses
    the RFC3339 datetimes the wire carries)."""
    from services.agent_ops import daemon as ops_daemon

    seen: list[PrepareFactsPayload] = []

    def handler(payload: PrepareFactsPayload) -> PrepareFactsResult:
        seen.append(payload)
        return _shipment(unit_home)

    monkeypatch.setattr(ops_prepare_facts, "cluster_prepare_facts_op", handler)

    status, result = ops_daemon._dispatch_sync(
        "cluster_prepare_facts", _payload().model_dump(mode="json")
    )

    assert status == "completed"
    assert seen == [_payload()]
    unit_wire = result["unit"]
    assert isinstance(unit_wire, dict) and unit_wire["machine"] == "runner"
