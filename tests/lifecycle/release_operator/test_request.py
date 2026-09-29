"""`ava cluster release request` — build the fleet's release request on the gateway home.

Exercises the real `current_release` discovery and `verify_pair` admission
against real fixture images (`conftest.build_image`); only the *prepared
receipt file* is stubbed — `PreparationReceipt`'s own cross-field validation
belongs to `tests/lifecycle/preparation/test_preparation.py`, not this
module's wiring — and the registered units, which are database rows
(`registered_units`; real in tests/lifecycle/db_authority/test_fleet_of_one.py).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from base import paths as base_paths
from base.cluster import machine as base_machine
from base.deploy.release.runtime_release import activate_release
from base.runtime_abi import current_abi
from cli.release_fleet.policy import AlertRoute
from cli.release_fleet.request import FleetRequest
from cli.release_operator import request as request_module
from cli.release_operator.layout import receipt_path
from cli.release_transition.request import ReleaseRef
from tests.lifecycle.release_operator.conftest import build_image

_COMMIT = "a" * 40


_RUNNER = ("macbook-air", "/Users/zzy/.ava")


@pytest.fixture
def rows() -> dict[str, set[Any]]:
    """The database's registered units, machines and paused machines."""
    return {"units": set(), "machines": {"test-unit"}, "paused": set()}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: dict[str, set[Any]]) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    rows["units"].add(("test-unit", str(path)))
    monkeypatch.setattr(base_paths, "ava_home", lambda: path)
    monkeypatch.setattr(base_machine, "machine_name", lambda: "test-unit")
    monkeypatch.setattr(
        request_module,
        "registered_units",
        lambda: (set(rows["units"]), set(rows["machines"]), set(rows["paused"])),
    )
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


def _stub_receipt(
    monkeypatch: pytest.MonkeyPatch, home: Path, reference: ReleaseRef, *, path: Path | None = None
) -> Path:
    """Place a receipt at the exact path `--commit` resolves to (or `path`); its
    bytes are never really parsed — `PreparationReceipt.model_validate_json` is stubbed."""
    path = receipt_path(home, reference.source_commit) if path is None else path
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


@pytest.mark.parametrize(
    ("exclude", "reason", "message"),
    [
        (("macbook-air:/Users/zzy/.ava",), None, "given together"),
        ((), "paused", "given together"),
        (("win:C:\\ava",), "gone", "not registered"),
        (("test-unit:HOME",), "gone", "cannot be excluded"),
        ((), None, "dbgen-8"),
    ],
)
def test_every_registered_unit_is_accounted_for_before_writing(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: dict[str, set[Any]],
    capsys: pytest.CaptureFixture[str],
    exclude: tuple[str, ...],
    reason: str | None,
    message: str,
) -> None:
    previous = build_image(home, "previous")
    candidate = build_image(home, "candidate")
    _select(home, previous)
    _stub_receipt(monkeypatch, home, candidate)
    rows["units"].add(_RUNNER)
    rows["machines"].add(_RUNNER[0])
    exclude = tuple(value.replace("HOME", str(home)) for value in exclude)
    code = request_module.cmd_release_request(
        commit=candidate.source_commit, out=tmp_path / "out.json", exclude=exclude, reason=reason
    )
    assert code == 2 and message in capsys.readouterr().err
    assert not (tmp_path / "out.json").exists()


def test_paused_and_operator_excluded_units_are_recorded_exclusions(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: dict[str, set[Any]]
) -> None:
    previous = build_image(home, "previous")
    candidate = build_image(home, "candidate")
    _select(home, previous)
    _stub_receipt(monkeypatch, home, candidate)
    rows["units"] |= {_RUNNER, ("win", "C:\\Users\\zzy\\.ava")}
    rows["machines"] |= {"macbook-air", "win"}
    rows["paused"].add("win")
    out = tmp_path / "out.json"
    code = request_module.cmd_release_request(
        commit=candidate.source_commit,
        out=out,
        exclude=("macbook-air:/Users/zzy/.ava",),
        reason="lid closed",
    )
    assert code == 0
    request = FleetRequest.model_validate_json(out.read_bytes())
    assert request.units == () and request.coordinator is None
    assert [(e.unit.label, e.reason, e.detail) for e in request.excluded] == [
        ("macbook-air:/Users/zzy/.ava", "operator", "lid closed"),
        ("win:C:\\Users\\zzy\\.ava", "paused", "paused machine"),
    ]
    assert request.excluded[0].recorded_by.startswith("operator:")


def test_an_explicit_receipt_and_watch_window_are_captured(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = build_image(home, "previous")
    candidate = build_image(home, "candidate")
    _select(home, previous)
    elsewhere = tmp_path / "receipt-b.json"
    _stub_receipt(monkeypatch, home, candidate, path=elsewhere)
    out = tmp_path / "out.json"
    code = request_module.cmd_release_request(
        commit=candidate.source_commit,
        out=out,
        exclude=(),
        reason=None,
        receipt=elsewhere,
        watch_s=60,
    )
    assert code == 0
    request = FleetRequest.model_validate_json(out.read_bytes())
    assert request.candidate == candidate and request.policy.watch_s == 60
    code = request_module.cmd_release_request(
        commit="f" * 40, out=tmp_path / "other.json", exclude=(), reason=None, receipt=elsewhere
    )
    assert code == 2


_REJECTING = UUID("4d2f7a3e-6c1b-4f0e-9a8d-2b5c7e9f1a03")


def _webhook_secret(home: Path, mode: int = 0o600) -> None:
    (home / "secrets").mkdir(mode=0o700)
    secret = home / "secrets" / "release-webhook"
    secret.write_text("https://hooks.example/fleet/0123456789abcdef\n")
    secret.chmod(mode)


def test_an_alert_route_and_an_acknowledged_rejection_are_captured(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The out-of-band webhook, the observing agent and the re-request of a
    rejected candidate are operator choices the request carries."""
    previous = build_image(home, "previous")
    candidate = build_image(home, "candidate")
    _select(home, previous)
    _stub_receipt(monkeypatch, home, candidate)
    _webhook_secret(home)
    out = tmp_path / "out.json"
    code = request_module.cmd_release_request(
        commit=candidate.source_commit,
        out=out,
        exclude=(),
        reason=None,
        alert_agent=1818,
        alert_webhook_file="release-webhook",
        acknowledged_rejection=str(_REJECTING),
    )
    assert code == 0
    policy = FleetRequest.model_validate_json(out.read_bytes()).policy
    assert policy.alert_route == AlertRoute(recipient_agent=1818, webhook_file="release-webhook")
    assert policy.acknowledged_rejection == _REJECTING


@pytest.mark.parametrize(
    ("mode", "rejection", "message"),
    [
        (None, None, "No such file"),
        (0o644, None, "owner-only"),
        (0o600, "not-an-operation", "UUID"),
    ],
)
def test_an_unusable_alert_route_or_rejection_refuses_before_writing(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: int | None,
    rejection: str | None,
    message: str,
) -> None:
    previous = build_image(home, "previous")
    candidate = build_image(home, "candidate")
    _select(home, previous)
    _stub_receipt(monkeypatch, home, candidate)
    if mode is not None:
        _webhook_secret(home, mode)
    out = tmp_path / "out.json"
    code = request_module.cmd_release_request(
        commit=candidate.source_commit,
        out=out,
        exclude=(),
        reason=None,
        alert_webhook_file="release-webhook",
        acknowledged_rejection=rejection,
    )
    assert code == 2 and message in capsys.readouterr().err
    assert not out.exists()


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
    request = FleetRequest.model_validate_json(out.read_bytes())
    assert request.home == str(home)
    assert request.machine == "test-unit"
    assert request.previous == previous
    assert request.candidate == candidate
    assert request.executor == candidate
    assert (request.units, request.excluded, request.coordinator) == ((), (), None)
    assert request.path.parent.exists() is False  # nothing was submitted or dispatched
