"""Composition uses fresh commands and delegates exactly one lifecycle/cleanup owner."""

from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.preview import local, release_proof
from scripts.preview.release_cycle import CapturedReceipt
from tests.lifecycle.preview.test_local import repo as repo


@pytest.fixture
def proof(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> release_proof.ReleaseProof:
    monkeypatch.setattr(release_proof.sys, "platform", "linux")
    preview = local.create(repo, "HEAD", tmp_path / "runs")
    preview.data.update(state="ready", verification="passed")
    monkeypatch.setattr(preview, "assert_checkout", lambda: None)
    (preview.run / "config.json").write_text(json.dumps({"ports": {"gateway": 48000}}))
    paths: list[Path] = []
    for name in ("a", "b"):
        path = tmp_path / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    "commit": name * 40,
                    "work": str(preview.run / "release-proof" / name / "acquisition"),
                    "frontend": {"gateway_port": 48000},
                }
            )
        )
        paths.append(path)
    return release_proof.ReleaseProof(preview, *paths)


@pytest.mark.parametrize("failed", [None, "a", "b", "cycle"])
def test_composition_preserves_failure_without_stopping_source_or_retrying_cycle(
    proof: release_proof.ReleaseProof, monkeypatch: pytest.MonkeyPatch, failed: str | None
) -> None:
    reached: list[str] = []

    def prepare(name: str) -> CapturedReceipt:
        reached.append(name)
        if name == failed:
            raise RuntimeError(name)
        return CapturedReceipt(proof.work / name / "receipt.json", b"captured", name * 40)

    class Cycle:
        def __init__(
            self, preview: local.Preview, previous: CapturedReceipt, candidate: CapturedReceipt
        ) -> None:
            assert preview is proof.preview
            assert previous.path == proof.work / "a/receipt.json"
            assert candidate.path == proof.work / "b/receipt.json"
            self.proof = {"result": "running"}

        def run(self) -> None:
            reached.append("cycle")
            if failed == "cycle":
                raise RuntimeError("cycle cleanup retained unknown custody")
            self.proof["result"] = "passed"

    def forbidden() -> None:
        pytest.fail("composition must not bypass preparation/cycle cleanup policy")

    monkeypatch.setattr(proof, "prepare", prepare)
    monkeypatch.setattr(proof.preview, "stop", forbidden)
    monkeypatch.setattr(release_proof, "ReleaseCycle", Cycle)
    if failed:
        with pytest.raises(RuntimeError, match=failed):
            proof.run()
    else:
        proof.run()
    expected = ["a"] if failed == "a" else ["a", "b"] if failed == "b" else ["a", "b", "cycle"]
    assert reached == expected
    recorded = json.loads(proof.path.read_text())
    assert recorded["result"] == ("failed" if failed else "passed")
    assert all("finished_at" in entry for entry in recorded["phases"].values())


def test_preparation_dispatch_uses_captured_request_and_fresh_environment(
    proof: release_proof.ReleaseProof, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    proof.preview.env["PYTHONPATH"] = "/untrusted"
    proof.preview.env["AVA_HOME"] = "/unrelated"

    def command(argv: list[str], **kwargs: Any) -> None:
        assert kwargs["timeout"] == 7200
        assert "PYTHONPATH" not in kwargs["env"] and "AVA_HOME" not in kwargs["env"]
        assert kwargs["evidence"] == proof.work / "a/custody.json"
        calls.append(argv)
        work = proof.work / "a"
        (work / "preparation").mkdir()
        receipt = work / "preparation/receipt.json"
        receipt.write_text(
            json.dumps({"source": {"source_commit": "a" * 40}, "request": {"commit": "a" * 40}})
        )
        (work / "prepared.json").write_text(
            json.dumps(
                {
                    "request_digest": proof.proof["requests"]["a"]["sha256"],
                    "preparation": str(receipt),
                    "preparation_digest": hashlib.sha256(receipt.read_bytes()).hexdigest(),
                }
            )
        )

    monkeypatch.setattr(release_proof.preparation_process, "run", command)
    receipt = proof.prepare("a")
    assert calls == [
        [
            str(proof.preview.source / ".venv/bin/python"),
            "-m",
            "scripts.preview.release_inputs",
            "--request",
            str(proof.work / "a/acquisition-request.json"),
            "--work",
            str(proof.work / "a"),
            "--store",
            str(proof.preview.home / "releases"),
        ]
    ]
    assert receipt.path == proof.work / "a/preparation/receipt.json"
    assert receipt.encoded == (proof.work / "a/captured-receipt.json").read_bytes()


def test_successful_child_exit_cannot_hide_changed_preparation_receipt(
    proof: release_proof.ReleaseProof, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = proof.work / "a"
    (work / "preparation").mkdir()
    receipt = work / "preparation/receipt.json"
    receipt.write_text(
        json.dumps({"source": {"source_commit": "a" * 40}, "request": {"commit": "a" * 40}})
    )
    (work / "prepared.json").write_text(
        json.dumps(
            {
                "request_digest": proof.proof["requests"]["a"]["sha256"],
                "preparation": str(receipt),
                "preparation_digest": "a" * 64,
            }
        )
    )

    def completed(*_args: object, **_kwargs: object) -> None:
        pass

    monkeypatch.setattr(release_proof.preparation_process, "run", completed)
    with pytest.raises(RuntimeError, match="preparation evidence differs"):
        proof.prepare("a")


def test_interrupt_preserves_failed_phase_and_leaves_source_cleanup_to_operator(
    proof: release_proof.ReleaseProof, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(name: str) -> CapturedReceipt:
        raise KeyboardInterrupt

    monkeypatch.setattr(proof, "prepare", interrupted)
    with pytest.raises(KeyboardInterrupt):
        proof.run()
    recorded = json.loads(proof.path.read_text())
    assert recorded["result"] == "failed"
    assert recorded["phases"]["prepare-a"]["result"] == "failed"
    assert recorded["cleanup"] == "source preview retained until cycle takes custody"


def test_cli_refuses_concurrent_lifecycle_before_constructing_proof(
    proof: release_proof.ReleaseProof, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        release_proof.sys,
        "argv",
        ["release_proof", str(proof.preview.run), "--previous", "/a", "--candidate", "/b"],
    )

    def preview(_run: Path) -> local.Preview:
        return proof.preview

    def no_signal(*_args: object) -> None:
        pass

    monkeypatch.setattr(release_proof.local, "Preview", preview)
    monkeypatch.setattr(release_proof.signal, "signal", no_signal)
    with (proof.preview.run / "operation.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            release_proof.main()


@pytest.mark.parametrize("change", ["port", "commit", "work"])
def test_mismatched_acquisition_refuses_before_output(tmp_path: Path, change: str) -> None:
    work = tmp_path.resolve() / "image"
    value: dict[str, Any] = {
        "commit": "a" * 40,
        "work": str(work / "acquisition"),
        "frontend": {"gateway_port": 48000},
    }
    if change == "port":
        value["frontend"]["gateway_port"] = 8000
    elif change == "commit":
        value["commit"] = "main"
    else:
        value["work"] = str(tmp_path / "unrelated")
    path = tmp_path / "request.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="acquisition must bind"):
        release_proof._request(path, work, 48000)
    assert not work.exists()


def test_b_preparation_cannot_replace_a_before_cycle_effects(
    proof: release_proof.ReleaseProof, monkeypatch: pytest.MonkeyPatch
) -> None:
    effects: list[str] = []

    def prepare(name: str) -> CapturedReceipt:
        path = proof.work / name / "receipt.json"
        path.write_text(
            json.dumps({"source": {"source_commit": name * 40}, "request": {"commit": name * 40}})
        )
        captured = CapturedReceipt.read(path)
        if name == "b":
            (proof.work / "a/receipt.json").write_bytes(captured.encoded)
        return captured

    def forbidden(cycle: release_proof.ReleaseCycle) -> None:
        effects.append("cycle")
        cycle.proof["result"] = "passed"

    monkeypatch.setattr(proof, "prepare", prepare)
    monkeypatch.setattr(release_proof.ReleaseCycle, "run", forbidden)
    with pytest.raises(RuntimeError, match="receipt changed"):
        proof.run()
    assert effects == []
    assert proof.proof["result"] == "failed"
