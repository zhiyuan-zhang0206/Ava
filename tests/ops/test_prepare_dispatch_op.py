"""`ops.ops_prepare_dispatch.cluster_prepare_dispatch_op` -- the seal-and-validate relay contract.

The handler writes the dispatched sealed plan into the unit's private `run/`
directory under its content name, runs exactly one bounded child of the
candidate's `cli.prepared_plan` entry with the explicit minimal projection (no
database), and answers with the unit's acknowledgement. A validation refusal
is an answer -- the acknowledgement's `refusal` field -- while dispatch faults
(payload shape, missing image, timeout, unintelligible child) stay op failures.
The entry's own authority checks are deliberately not duplicated here.
"""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ops import ops_prepare_dispatch
from ops.rpc_prepare_dispatch import (
    DispatchValidation,
    PrepareDispatchPayload,
    PrepareDispatchResult,
    prepared_plan_name,
)
from shared.config import settings
from shared.runtime_release import ReleaseRejectedError

PLAN = '{"version":1,"target_sha":"' + "0" * 40 + '"}\n'
ARTIFACT = "a" * 64
RECEIPT = "cd" * 32
DIGEST = hashlib.sha256(PLAN.encode("ascii")).hexdigest()


@pytest.fixture
def unit_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """One canonical unit home with a retained candidate image and identity."""
    home = (tmp_path / "unit").resolve()
    interpreter = home / "releases" / ARTIFACT / "venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
    (home / "machine_name").write_text("runner\n", encoding="utf-8")
    monkeypatch.setattr(settings.general, "ava_home", home)
    return home


def _payload(artifact_digest: str = ARTIFACT, plan_json: str = PLAN) -> PrepareDispatchPayload:
    return PrepareDispatchPayload(plan_json=plan_json, artifact_digest=artifact_digest)


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


def _ack(plan_digest: str = DIGEST, receipt_digest: str = RECEIPT) -> str:
    validation = DispatchValidation(plan_digest=plan_digest, receipt_digest=receipt_digest)
    return (
        json.dumps(validation.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
    )


def test_payload_models_roundtrip_on_the_wire_shape() -> None:
    wire = _payload().model_dump(mode="json")
    assert PrepareDispatchPayload.model_validate_json(json.dumps(wire)) == _payload()
    with pytest.raises(ValueError):
        PrepareDispatchPayload.model_validate_json(json.dumps({**wire, "extra": 1}))


def test_passing_acknowledgement_requires_its_receipt() -> None:
    with pytest.raises(ValueError):
        PrepareDispatchResult(machine="runner", home="/ava", plan_digest=DIGEST)

    refusal = PrepareDispatchResult(
        machine="runner", home="/ava", plan_digest=DIGEST, refusal="no image"
    )
    assert refusal.receipt_digest is None


def test_plan_lands_private_under_its_content_name_and_the_child_runs(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _ChildRecorder(stdout=_ack())
    monkeypatch.setattr(ops_prepare_dispatch, "run_bounded", child)

    result = ops_prepare_dispatch.cluster_prepare_dispatch_op(_payload())

    assert result == PrepareDispatchResult(
        machine="runner", home=str(unit_home), plan_digest=DIGEST, receipt_digest=RECEIPT
    )
    plan_path = unit_home / "run" / prepared_plan_name(DIGEST)
    assert plan_path.read_bytes() == PLAN.encode("ascii")
    assert stat.S_IMODE(plan_path.stat().st_mode) == 0o600
    assert len(child.calls) == 1
    call = child.calls[0]
    interpreter = str(unit_home / "releases" / ARTIFACT / "venv" / "bin" / "python")
    assert call["argv"] == [
        interpreter,
        "-I",
        "-B",
        "-X",
        "utf8",
        "-m",
        "cli.prepared_plan",
        "--prepared",
        str(plan_path),
    ]
    assert call["timeout"] == ops_prepare_dispatch._VALIDATE_TIMEOUT_S
    assert call["capture_output"] is True
    assert call["text"] is True
    assert call["cwd"] == str(unit_home)
    assert call["env"] == {
        "PATH": "/usr/bin:/bin",
        "HOME": str(unit_home.parent),
        "AVA_HOME": str(unit_home),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def test_child_refusal_is_an_acknowledgement_not_an_op_failure(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _ChildRecorder(
        stderr="prepared plan refused: prepared entry deadline expired; no operation was started\n",
        returncode=2,
    )
    monkeypatch.setattr(ops_prepare_dispatch, "run_bounded", child)

    result = ops_prepare_dispatch.cluster_prepare_dispatch_op(_payload())

    assert result.machine == "runner"
    assert result.home == str(unit_home)
    assert result.plan_digest == DIGEST
    assert result.receipt_digest is None
    assert result.refusal is not None and "deadline expired" in result.refusal


def test_non_ascii_plan_refuses(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    child = _ChildRecorder(stdout=_ack())
    monkeypatch.setattr(ops_prepare_dispatch, "run_bounded", child)

    with pytest.raises(ReleaseRejectedError, match="not ASCII"):
        ops_prepare_dispatch.cluster_prepare_dispatch_op(_payload(plan_json='{"x":"caf\u00e9"}'))

    assert child.calls == []


def test_plan_beyond_the_relay_bound_refuses(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _ChildRecorder(stdout=_ack())
    monkeypatch.setattr(ops_prepare_dispatch, "run_bounded", child)
    oversized = "x" * (ops_prepare_dispatch._MAX_PLAN_BYTES + 1)

    with pytest.raises(ReleaseRejectedError, match="relay bound"):
        ops_prepare_dispatch.cluster_prepare_dispatch_op(_payload(plan_json=oversized))

    assert child.calls == []


def test_non_json_plan_refuses(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    child = _ChildRecorder(stdout=_ack())
    monkeypatch.setattr(ops_prepare_dispatch, "run_bounded", child)

    with pytest.raises(ReleaseRejectedError, match="not one JSON document"):
        ops_prepare_dispatch.cluster_prepare_dispatch_op(
            _payload(plan_json="not one json document")
        )

    assert child.calls == []


def test_unretained_image_refuses_before_any_child(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _ChildRecorder(stdout=_ack())
    monkeypatch.setattr(ops_prepare_dispatch, "run_bounded", child)

    with pytest.raises(ReleaseRejectedError, match="not retained"):
        ops_prepare_dispatch.cluster_prepare_dispatch_op(_payload(artifact_digest="9" * 64))

    assert child.calls == []


def test_child_timeout_translates_to_a_refusal(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout(_argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("child", ops_prepare_dispatch._VALIDATE_TIMEOUT_S)

    monkeypatch.setattr(ops_prepare_dispatch, "run_bounded", timeout)

    with pytest.raises(ReleaseRejectedError, match="timed out"):
        ops_prepare_dispatch.cluster_prepare_dispatch_op(_payload())


def test_invalid_acknowledgement_shape_refuses(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _ChildRecorder(stdout='{"plan_digest": "' + DIGEST + '"}\n')
    monkeypatch.setattr(ops_prepare_dispatch, "run_bounded", child)

    with pytest.raises(ReleaseRejectedError, match="shape is invalid"):
        ops_prepare_dispatch.cluster_prepare_dispatch_op(_payload())


def test_acknowledgement_for_another_plan_refuses(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _ChildRecorder(stdout=_ack(plan_digest="9" * 64))
    monkeypatch.setattr(ops_prepare_dispatch, "run_bounded", child)

    with pytest.raises(ReleaseRejectedError, match="different plan digest"):
        ops_prepare_dispatch.cluster_prepare_dispatch_op(_payload())


def test_acknowledgement_beyond_the_relay_bound_refuses(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _ChildRecorder(stdout="x" * (ops_prepare_dispatch._MAX_ACK_BYTES + 1))
    monkeypatch.setattr(ops_prepare_dispatch, "run_bounded", child)

    with pytest.raises(ReleaseRejectedError, match="relay bound"):
        ops_prepare_dispatch.cluster_prepare_dispatch_op(_payload())


def test_daemon_dispatch_accepts_the_json_shaped_payload(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The op envelope crosses the wire as JSON, so the dispatch arm must give
    the handler a JSON-mode-validated payload."""
    from services.agent_ops import daemon as ops_daemon

    seen: list[PrepareDispatchPayload] = []

    def handler(payload: PrepareDispatchPayload) -> PrepareDispatchResult:
        seen.append(payload)
        return PrepareDispatchResult(
            machine="runner", home=str(unit_home), plan_digest=DIGEST, receipt_digest=RECEIPT
        )

    monkeypatch.setattr(ops_prepare_dispatch, "cluster_prepare_dispatch_op", handler)

    status, result = ops_daemon._dispatch_sync(
        "cluster_prepare_dispatch", _payload().model_dump(mode="json")
    )

    assert status == "completed"
    assert seen == [_payload()]
    assert result["plan_digest"] == DIGEST
