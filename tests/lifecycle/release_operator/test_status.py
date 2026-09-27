"""`ava cluster release status` — read-only view of this home's release state.

Every assertion here also proves the read-only guarantee: nothing on disk
changes across a status call (`_snapshot` before/after).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from cli.release_operator import status as status_module
from cli.release_transition.journal import Operation
from cli.release_transition.request import ReleaseRef, Request
from shared import machine as shared_machine
from shared import paths as shared_paths
from shared.runtime_abi import current_abi
from shared.runtime_release import activate_release
from tests.lifecycle.release_operator.conftest import build_image, digest


def _reference(label: str) -> ReleaseRef:
    return ReleaseRef(
        artifact_digest=digest(label.encode()),
        manifest_digest=digest((label + "-m").encode()),
        schema_digest=digest((label + "-s").encode()),
        source_commit=digest((label + "-c").encode())[:40],
    )


def _request(home: Path) -> Request:
    return Request(
        id=uuid4(),
        home=str(home),
        registry=str(home.parent / "clusters.json"),
        created_at=datetime.now(UTC),
        machine="test-unit",
        previous=_reference("previous"),
        candidate=_reference("candidate"),
        executor=_reference("candidate"),
        configuration_digest="f" * 64,
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setattr(shared_paths, "ava_home", lambda: path)
    monkeypatch.setattr(shared_machine, "machine_name", lambda: "test-unit")
    return path


def _snapshot(home: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(home)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in home.rglob("*")
        if p.is_file()
    }


def test_reports_none_before_any_selection_or_operation(home: Path) -> None:
    before = _snapshot(home)

    code = status_module.cmd_release_status(operation=None, as_json=True)

    assert code == 0
    assert _snapshot(home) == before


def test_reports_the_current_selection_read_only(home: Path) -> None:
    reference = build_image(home, "candidate")
    activate_release(
        home / "releases",
        reference.artifact_digest,
        expected_current=None,
        manifest_digest=reference.manifest_digest,
        host_abi=current_abi(),
        schema_digest=reference.schema_digest,
    )
    before = _snapshot(home)

    body = status_module._status_body(operation=None)

    assert body["current"]["source_commit"] == reference.source_commit
    assert body["operation"] is None
    assert _snapshot(home) == before


def test_reports_the_active_operation_read_only(home: Path) -> None:
    request = _request(home)
    operation = Operation(request=request)
    request.path.parent.mkdir(parents=True)
    request.path.write_text(operation.model_dump_json())
    (home / "updates/active").write_text(str(request.path))
    before = _snapshot(home)

    body = status_module._status_body(operation=None)

    assert body["operation"]["id"] == str(request.id)
    assert body["operation"]["phase"] == "prepared"
    assert body["operation"]["direction"] == "candidate"
    assert body["operation"]["terminal"] is False
    assert _snapshot(home) == before


def test_explicit_operation_id_reads_that_journal_directly(home: Path) -> None:
    request = _request(home)
    operation = Operation(request=request)
    request.path.parent.mkdir(parents=True)
    request.path.write_text(operation.model_dump_json())
    # Deliberately no "active" pointer: --operation must not depend on it.

    body = status_module._status_body(operation=str(request.id))

    assert body["operation"]["id"] == str(request.id)


def test_explicit_unknown_operation_id_refuses(home: Path) -> None:
    code = status_module.cmd_release_status(operation=str(uuid4()), as_json=True)
    assert code == 2


def test_render_is_human_readable_when_not_json(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = status_module.cmd_release_status(operation=None, as_json=False)
    assert code == 0
    out = capsys.readouterr().out
    assert "home:" in out and "current release: none" in out and "release operation: none" in out
