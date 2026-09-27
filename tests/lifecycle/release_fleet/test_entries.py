"""The handoff's `receipt` and `preflight` entries a unit answers a fleet coordinator with.

Real fixture images (`release_operator.conftest.build_image`, verified for
real), a real unit journal and installed enrollment; the loaded identity and
the native adapter probe are replaced.
"""

from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

import cli.release_handoff.__main__ as handoff
from cli.release_fleet import entries
from cli.release_fleet.policy import UnitKey
from cli.release_fleet.request import CoordinatorEndpoint, UnitReceipt, UnitRequest
from cli.release_transition.journal import create
from cli.release_transition.local import LocalTransition
from shared.cluster.authority.unit import Enrollment, UnitIdentity, unit_enrollment_path
from shared.private_storage import write_private_bytes
from shared.runtime_abi import current_abi
from shared.runtime_release import activate_release
from shared.start_inputs import configuration_digest
from tests.lifecycle.release_operator.conftest import build_image

_MACHINE = "macbook-air"


class Unit:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.previous = build_image(home, "previous")
        self.candidate = build_image(home, "candidate")
        activate_release(
            home / "releases",
            self.previous.artifact_digest,
            expected_current=None,
            manifest_digest=self.previous.manifest_digest,
            host_abi=current_abi(),
            schema_digest=self.previous.schema_digest,
        )
        self.enrollment = Enrollment(
            version=1,
            enrollment_id="c" * 32,
            unit=UnitIdentity(machine=_MACHINE, home=str(home)),
            secret="s" * 43,
        )
        write_private_bytes(unit_enrollment_path(home), self.enrollment.model_dump_json().encode())

    def probe(self, **changes: Any) -> bytes:
        document: dict[str, Any] = {
            "version": 1,
            "kind": "receipt-probe",
            "id": str(uuid4()),
            "home": str(self.home),
            "machine": _MACHINE,
            "executor": self.candidate.model_dump(mode="json"),
        } | changes
        return json.dumps(document).encode()

    def request(self, **changes: Any) -> UnitRequest:
        fields: dict[str, Any] = {
            "id": uuid4(),
            "home": str(self.home),
            "registry": str(self.home.parent / "clusters.json"),
            "created_at": datetime.now(UTC),
            "machine": _MACHINE,
            "configuration_digest": configuration_digest(self.home),
            "previous": self.previous,
            "candidate": self.candidate,
            "executor": self.candidate,
            "gateway": UnitKey(machine="ubuntu", home="/home/zzy/.ava"),
            "coordinator": CoordinatorEndpoint(host="10.0.0.5", port=8121),
            "enrollment_id": self.enrollment.enrollment_id,
            "policy": {},
        } | changes
        return UnitRequest.model_validate(fields)


@pytest.fixture
def unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Unit:
    import shared.cluster
    import shared.machine
    import shared.os_boot_unit

    home = tmp_path.resolve() / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(shared.machine, "machine_name", lambda: _MACHINE)
    monkeypatch.setattr(shared.machine, "machine_role", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(shared.cluster, "registry_path", lambda: home.parent / "clusters.json")
    monkeypatch.setattr(shared.os_boot_unit, "systemd_running", lambda: True)
    return Unit(home)


def test_the_receipt_is_everything_a_coordinator_needs_to_include_the_unit(unit: Unit) -> None:
    receipt = entries.receipt(unit.probe())
    assert (receipt.machine, receipt.home, receipt.roles) == (
        _MACHINE,
        str(unit.home),
        ("agent-runner",),
    )
    assert (receipt.previous, receipt.candidate) == (unit.previous, unit.candidate)
    assert receipt.adapter == "linux-systemd-v1" and receipt.abi == current_abi().to_json()
    assert receipt.enrollment_id == unit.enrollment.enrollment_id
    spec = receipt.spec()
    assert spec.unit == UnitKey(machine=_MACHINE, home=str(unit.home))
    assert spec.receipt_digest == receipt.digest
    assert spec.configuration_digest == configuration_digest(unit.home)
    assert UnitReceipt.model_validate_json(receipt.model_dump_json()).digest == receipt.digest


def test_a_receipt_names_what_keeps_the_unit_out(unit: Unit) -> None:
    receipt = entries.receipt(unit.probe())
    with pytest.raises(ValueError, match="holds no enrollment"):
        receipt.model_copy(update={"enrollment_id": None}).spec()
    with pytest.raises(ValueError, match="no selected release"):
        receipt.model_copy(update={"previous": None}).spec()
    with pytest.raises(ValueError, match="names machine"):
        entries.receipt(unit.probe(machine="company-mini"))
    forged = unit.candidate.model_copy(update={"source_commit": "f" * 40})
    with pytest.raises(ValueError):
        entries.receipt(unit.probe(executor=forged.model_dump(mode="json")))


def test_the_preflight_answers_ready_only_for_an_admissible_unit_request(
    unit: Unit, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = unit.request()
    encoded = request.model_dump_json().encode()
    # Until dbgen-8 the submission gate itself refuses a remote unit, naming it.
    with pytest.raises(ValueError, match="dbgen-8"):
        entries.preflight(encoded)
    monkeypatch.setattr(LocalTransition, "preflight", lambda _self: None)
    assert entries.preflight(encoded) == {
        "ready": True,
        "unit": request.unit.label,
        "operation": str(request.id),
    }
    other = unit.request(enrollment_id="d" * 32)
    with pytest.raises(ValueError, match="installed enrollment"):
        entries.preflight(other.model_dump_json().encode())
    create(unit.request(id=uuid4()))  # another operation, incomplete
    with pytest.raises(ValueError, match="remains incomplete"):
        entries.preflight(encoded)


def test_the_handoff_entry_runs_them_as_the_named_executor(
    unit: Unit, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = unit.home / "releases" / unit.candidate.artifact_digest
    monkeypatch.setattr(handoff, "_code_root", lambda: root / "venv/lib/python3.12/site-packages")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(unit.probe())))
    assert handoff.main(["receipt", "-"]) == 0
    printed = UnitReceipt.model_validate_json(capsys.readouterr().out)
    assert printed.candidate == unit.candidate
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(unit.probe(machine="win"))))
    assert handoff.main(["receipt", "-"]) == 2
    assert "release receipt refused" in capsys.readouterr().err
    request = unit.request().model_dump_json().encode()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(request)))
    assert handoff.main(["preflight", "-"]) == 2
    assert "dbgen-8" in capsys.readouterr().err
