"""Terminal business truth is bound to its exact completed home journal."""

from __future__ import annotations

# pyright: reportUnusedImport=false
# ruff: noqa: F811 -- imported pytest fixture is injected by name.
import json
from dataclasses import replace
from pathlib import Path

import pytest

from cli.release_transition import journal, pitr_submit, submit
from cli.release_transition.pitr_inputs import require_inputs
from cli.release_transition.request import PitrRequest, Request
from services.pitr.activation_state import mark_pre_mutation_rolled_back, record_path, write_record
from shared.release_operation import authorized_pitr
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
    image = request.image.model_copy(update={"artifact_digest": "e" * 64})
    release = Request(
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
    return journal.Operation(request=release, phase="complete")


@pytest.mark.parametrize("later_release", [False, True])
def test_repeated_rollback_joins_exact_completed_business_receipt(
    pitr_request: PitrRequest, monkeypatch: pytest.MonkeyPatch, later_release: bool
) -> None:
    completed = _rolled_back(pitr_request)
    active = _release(pitr_request) if later_release else completed
    home = Path(pitr_request.home)
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)
    monkeypatch.setattr("shared.os_boot_unit.systemd_running", lambda: True)
    monkeypatch.setattr(pitr_submit, "_active", _constant(active))
    before = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
    request = pitr_submit.prepare_request("rollback", origin="operator")
    assert request == pitr_request
    assert request.activation_id == pitr_request.activation_id
    # A no-restart completion cannot fall into preflight/launch on repeat.
    path, native = submit.submit_request(request)
    assert path == request.path and native == {"state": "not_started"}
    assert {path: path.read_bytes() for path in home.rglob("*") if path.is_file()} == before


def test_terminal_repeat_rejects_unowned_business_bytes(
    pitr_request: PitrRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rolled_back(pitr_request)
    home = Path(pitr_request.home)
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)
    path = record_path(home)
    payload = json.loads(path.read_bytes())
    payload["origin"] = "unowned writer"
    path.write_text(json.dumps(payload))
    before = pitr_request.path.read_bytes()
    with pytest.raises(ValueError, match="activation record differs"):
        pitr_submit.prepare_request("rollback", origin="operator")
    assert pitr_request.path.read_bytes() == before


def test_rolled_back_record_without_completed_journal_refuses_before_reservation(
    pitr_request: PitrRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path(pitr_request.home)
    record = _record(pitr_request)
    write_record(home, record)
    mark_pre_mutation_rolled_back(home, record)
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)
    monkeypatch.setattr(pitr_submit, "_active", _constant(_release(pitr_request)))
    with pytest.raises(ValueError, match="retained home journal"):
        pitr_submit.prepare_request("rollback", origin="operator")
    assert not pitr_request.path.exists()


def test_shadow_record_does_not_authorize_captured_env_deletion(pitr_request: PitrRequest) -> None:
    journal.create(pitr_request)
    home = Path(pitr_request.home)
    with journal.exclusive(pitr_request.path) as handle:
        handle.advance("provisioning")
        with authorized_pitr(pitr_request.path, handle.pitr_record_write):
            write_record(home, _record(pitr_request))
        (home / ".env").unlink()
        with pytest.raises(ValueError, match="environment"):
            require_inputs(handle.operation)
