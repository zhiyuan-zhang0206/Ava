"""`ava cluster release request` — build one single-host release `Request`.

Exercises the real `current_release` discovery and `verify_pair` admission
against real fixture images (`conftest.build_image`); only the *prepared
receipt file* is stubbed — `PreparationReceipt`'s own cross-field validation
belongs to `tests/lifecycle/preparation/test_preparation.py`, not this
module's wiring.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import shared.cluster as cluster_pkg
from cli.release_operator import request as request_module
from cli.release_operator.layout import receipt_path
from cli.release_transition.request import ReleaseRef, Request
from shared import machine as shared_machine
from shared import paths as shared_paths
from shared.runtime_abi import current_abi
from shared.runtime_release import activate_release
from tests.lifecycle.release_operator.conftest import build_image

_COMMIT = "a" * 40


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setattr(shared_paths, "ava_home", lambda: path)
    monkeypatch.setattr(cluster_pkg, "registry_path", lambda: tmp_path / "clusters.json")
    monkeypatch.setattr(shared_machine, "machine_name", lambda: "test-unit")
    return path


def _select(home: Path, reference: ReleaseRef) -> None:
    activate_release(
        home / "releases",
        reference.artifact_digest,
        expected_current=None,
        manifest_digest=reference.manifest_digest,
        host_abi=current_abi(),
        schema_digest=reference.schema_digest,
    )


def _stub_receipt(monkeypatch: pytest.MonkeyPatch, home: Path, reference: ReleaseRef) -> Path:
    """Place a receipt at the exact path `--commit` resolves to; its bytes are
    never really parsed — `PreparationReceipt.model_validate_json` is stubbed."""
    path = receipt_path(home, reference.source_commit)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{}")
    fake = SimpleNamespace(
        source=SimpleNamespace(source_commit=reference.source_commit),
        image=SimpleNamespace(
            artifact_digest=reference.artifact_digest,
            manifest_digest=reference.manifest_digest,
            schema_digest=reference.schema_digest,
        ),
    )
    monkeypatch.setattr(
        request_module.PreparationReceipt,
        "model_validate_json",
        staticmethod(lambda _data: fake),
    )
    return path


def test_exclude_or_reason_always_refuses(home: Path, tmp_path: Path) -> None:
    code = request_module.cmd_release_request(
        commit=_COMMIT, out=tmp_path / "out.json", exclude=("m:home",), reason=None
    )
    assert code == 2
    code = request_module.cmd_release_request(
        commit=_COMMIT, out=tmp_path / "out.json", exclude=(), reason="paused"
    )
    assert code == 2
    assert not (tmp_path / "out.json").exists()


def test_malshaped_commit_refuses(home: Path, tmp_path: Path) -> None:
    code = request_module.cmd_release_request(
        commit="not-a-sha", out=tmp_path / "out.json", exclude=(), reason=None
    )
    assert code == 2


def test_missing_prepared_receipt_refuses(home: Path, tmp_path: Path) -> None:
    code = request_module.cmd_release_request(
        commit=_COMMIT, out=tmp_path / "out.json", exclude=(), reason=None
    )
    assert code == 2


def test_no_active_selection_refuses_naming_adopt(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = build_image(home, "candidate")
    _stub_receipt(monkeypatch, home, candidate)
    code = request_module.cmd_release_request(
        commit=candidate.source_commit, out=tmp_path / "out.json", exclude=(), reason=None
    )
    assert code == 2
    assert not (tmp_path / "out.json").exists()


def test_candidate_equal_to_current_refuses(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = build_image(home, "same")
    _select(home, reference)
    _stub_receipt(monkeypatch, home, reference)
    code = request_module.cmd_release_request(
        commit=reference.source_commit, out=tmp_path / "out.json", exclude=(), reason=None
    )
    assert code == 2
    assert not (tmp_path / "out.json").exists()


def test_out_already_exists_refuses(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = build_image(home, "previous")
    candidate = build_image(home, "candidate")
    _select(home, previous)
    _stub_receipt(monkeypatch, home, candidate)
    out = tmp_path / "out.json"
    out.write_text("already here")
    code = request_module.cmd_release_request(
        commit=candidate.source_commit, out=out, exclude=(), reason=None
    )
    assert code == 2
    assert out.read_text() == "already here"


def test_happy_path_writes_a_request_ava_cluster_update_can_consume(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = build_image(home, "previous")
    candidate = build_image(home, "candidate")
    _select(home, previous)
    _stub_receipt(monkeypatch, home, candidate)
    out = tmp_path / "out.json"

    code = request_module.cmd_release_request(
        commit=candidate.source_commit, out=out, exclude=(), reason=None
    )

    assert code == 0
    assert out.stat().st_mode & 0o777 == 0o600
    request = Request.model_validate_json(out.read_bytes())
    assert request.home == str(home)
    assert request.registry == str(cluster_pkg.registry_path())
    assert request.machine == "test-unit"
    assert request.previous == previous
    assert request.candidate == candidate
    assert request.executor == candidate
    assert request.path.parent.exists() is False  # nothing was submitted or dispatched
