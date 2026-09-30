"""Terminal business truth is bound to its exact completed home journal."""

from __future__ import annotations

# pyright: reportUnusedImport=false
# ruff: noqa: F811 -- imported pytest fixture is injected by name.
import json
from dataclasses import replace
from pathlib import Path

import pytest

from base.deploy.release.operation import authorized_pitr
from cli.release_fleet.request import FleetRequest
from cli.release_transition import journal
from cli.release_transition.pitr import submission
from cli.release_transition.request import PitrRequest
from services.pitr.activation.state import mark_pre_mutation_rolled_back, record_path, write_record
from tests.lifecycle.transition.phases import at_phase
from tests.lifecycle.transition.test_launcher_linux import _constant
from tests.lifecycle.transition.test_pitr_operation import (  # noqa: F401 — fixture
    _record,
    pitr_request,
)


def _rolled_back(request: PitrRequest) -> journal.Operation:
    journal.create(request)
    with journal.exclusive(request.path) as handle:
        handle.decide_rollback("f" * 64, maintenance_at=request.created_at)
        handle.advance("provisioning")
        assert handle.operation.pitr is not None
        handle.record_pitr(handle.operation.pitr.model_copy(update={"record_digest": None}))
        with authorized_pitr(request.path, handle.pitr_record_write):
            record = replace(_record(request), home_action="rollback", home_generation=2)
            write_record(Path(request.home), record)
            mark_pre_mutation_rolled_back(Path(request.home), record)
        return handle.pitr_no_restart()


def _release(request: PitrRequest) -> journal.Operation:
    image = request.image.model_copy(
        update={"artifact_digest": "e" * 64, "source_commit": "9" * 40}
    )
    release = FleetRequest(
        **request.model_dump(
            exclude={
                "kind",
                "image",
                "activation_id",
                "action",
                "generation",
                "expected_record",
                "origin",
                "configuration_files",
            }
        ),
        previous=request.image,
        candidate=image,
        executor=image,
    )
    return at_phase("complete", request=release)


def test_terminal_repeat_rejects_unowned_business_bytes(
    pitr_request: PitrRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rolled_back(pitr_request)
    home = Path(pitr_request.home)
    monkeypatch.setattr("base.paths.ava_home", lambda: home)
    path = record_path(home)
    payload = json.loads(path.read_bytes())
    payload["origin"] = "unowned writer"
    path.write_text(json.dumps(payload))
    before = pitr_request.path.read_bytes()
    with pytest.raises(ValueError, match="activation record differs"):
        submission.prepare_request("rollback", origin="operator")
    assert pitr_request.path.read_bytes() == before


def test_rolled_back_record_without_completed_journal_refuses_before_reservation(
    pitr_request: PitrRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path(pitr_request.home)
    record = _record(pitr_request)
    write_record(home, record)
    mark_pre_mutation_rolled_back(home, record)
    monkeypatch.setattr("base.paths.ava_home", lambda: home)
    monkeypatch.setattr(submission, "_active", _constant(_release(pitr_request)))
    with pytest.raises(ValueError, match="retained home journal"):
        submission.prepare_request("rollback", origin="operator")
    assert not pitr_request.path.exists()
