"""`cli.prepared_plan` -- the candidate entry's validation-to-acknowledgement contract.

Runs inside the candidate image to validate one dispatched sealed plan through
`prepare_operator_input` and answer with the digests of exactly what it
checked. The entry's deep data checks live in `prepare_operator_input` (covered
by its own tests); pinned here are the name-to-bytes binding, the canonical
acknowledgement shape, and the exit-2/sanitized-line refusal contract for every
refusal class the chain can raise.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli import prepared_plan
from ops.rpc_prepare_dispatch import prepared_plan_name
from shared.machine import MachineRoleMissing
from shared.managed_writer_barrier import ManagedWriterBarrierError
from shared.native_job_observation import NativeReadUnavailableError
from shared.runtime_release import ReleaseRejectedError

DIGEST = "ab" * 32
RECEIPT = "cd" * 32


def _validated(digest: str = DIGEST, receipt: str = RECEIPT) -> Any:
    """The minimal shape `produce_ack` reads off `prepare_operator_input`."""
    unit = SimpleNamespace(prepared_receipt_digest=receipt)
    return SimpleNamespace(digest=digest, local=SimpleNamespace(unit=unit))


def _stub_prepare(_path: Path) -> Any:
    return _validated()


def test_ack_is_named_for_its_digest_and_canonical(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / prepared_plan_name(DIGEST)
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(prepared_plan, "prepare_operator_input", _stub_prepare)

    assert prepared_plan.main(["--prepared", str(path)]) == 0

    out = capsys.readouterr().out
    assert out == f'{{"plan_digest":"{DIGEST}","receipt_digest":"{RECEIPT}"}}\n'
    assert json.loads(out) == {"plan_digest": DIGEST, "receipt_digest": RECEIPT}


def test_a_renamed_plan_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "prepared-plan-deadbeef.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(prepared_plan, "prepare_operator_input", _stub_prepare)

    assert prepared_plan.main(["--prepared", str(path)]) == 2
    assert "not named for its own bytes" in capsys.readouterr().err


@pytest.mark.parametrize(
    "error",
    [
        ReleaseRejectedError("prepared entry requires a canonical private unit plan"),
        OSError("input disappeared"),
        ValueError("json invalid"),
        ManagedWriterBarrierError("crossed scope"),
        NativeReadUnavailableError("native read unavailable"),
        MachineRoleMissing("no capability"),
    ],
)
def test_every_refusal_class_exits_two_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    def refuse(_path: Path) -> Any:
        raise error

    monkeypatch.setattr(prepared_plan, "prepare_operator_input", refuse)

    assert prepared_plan.main(["--prepared", "/unused/plan.json"]) == 2
    err = capsys.readouterr().err
    assert "prepared plan refused" in err
    assert "Traceback" not in err
