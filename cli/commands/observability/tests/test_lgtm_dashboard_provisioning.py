from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from base.telemetry import DrainStatus, EventPipeline
from cli.commands.observability import _lgtm_provisioning as provisioning

_UID = "meiva-ops-main"
_REL = provisioning.RENDERED_DASHBOARD_REL
_REPO_ROOT = Path(__file__).resolve().parents[4]
_REFERENCE = {"uid": _UID, "title": "Meiva Ops", "panels": [{"id": 1, "title": "Ops"}]}
_STALE = {"uid": "ava-ops-main", "title": "Ava Ops", "panels": [{"id": 1, "title": "Ops"}]}


def _json(document: Any) -> str:
    return json.dumps(document) + "\n"


def _repo(tmp_path: Path, reference: Any = _REFERENCE) -> Path:
    source = tmp_path / "repo" / "deploy/lgtm/config/grafana/provisioning"
    (source / "dashboards").mkdir(parents=True, exist_ok=True)
    if reference is not None:
        (source / _REL).write_text(_json(reference), encoding="utf-8")
    return tmp_path / "repo"


def _broken_database() -> Any:
    raise RuntimeError("database is down")


def _render(repo: Path, native: Path) -> list[Any]:
    written: list[Any] = []
    pipeline = EventPipeline(writer=written.extend)
    try:
        provisioning.render_provisioning(
            repo, native, database_factory=_broken_database, producer=lambda: pipeline
        )
    finally:
        assert pipeline.stop().status is DrainStatus.COMPLETED
    return written


def _dashboard(native: Path) -> Path:
    return native / "config" / "provisioning" / _REL


def _hashes(native: Path) -> dict[str, Any]:
    return json.loads((native / "config" / "provisioning-hashes.json").read_text(encoding="utf-8"))


def test_render_failure_installs_the_checkout_reference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    native = tmp_path / "native"

    written = _render(repo, native)

    reference_text = (repo / "deploy/lgtm/config/grafana/provisioning" / _REL).read_text(
        encoding="utf-8"
    )
    assert _dashboard(native).read_text(encoding="utf-8") == reference_text
    hashes = _hashes(native)
    assert hashes[_REL] == hashlib.sha256(reference_text.encode("utf-8")).hexdigest()
    assert hashes["$dashboard-source"] == "reference"
    [event] = written
    assert (event.event_name, event.level, event.attributes["outcome"]) == (
        "lgtm_dashboard_render_failed",
        "warning",
        "reference",
    )
    assert "installed the checkout reference" in capsys.readouterr().err


def test_render_failure_keeps_a_matching_installed_dashboard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    native = tmp_path / "native"
    installed = _dashboard(native)
    installed.parent.mkdir(parents=True)
    installed.write_text(_json({"uid": _UID, "panels": [{"id": 7}]}), encoding="utf-8")
    before = installed.read_text(encoding="utf-8")

    _render(_repo(tmp_path), native)

    assert installed.read_text(encoding="utf-8") == before
    assert "keeping the installed dashboard" in capsys.readouterr().err


@pytest.mark.parametrize(
    "reference",
    [pytest.param(None, id="no-reference"), pytest.param(_STALE, id="pre-rename-reference")],
)
def test_render_failure_without_a_usable_dashboard_fails_converge(
    tmp_path: Path, reference: Any
) -> None:
    with pytest.raises(RuntimeError, match="refusing to converge with no dashboards"):
        _render(_repo(tmp_path, reference=reference), tmp_path / "native")

    assert not _dashboard(tmp_path / "native").exists()


def _station(
    tmp_path: Path,
    *,
    document: Any = _REFERENCE,
    recorded: dict[str, Any] | None = None,
) -> Path:
    native = tmp_path / "native"
    dashboard = _dashboard(native)
    if document is not None:
        dashboard.parent.mkdir(parents=True)
        dashboard.write_text(_json(document), encoding="utf-8")
    if recorded is not None:
        (native / "config").mkdir(parents=True, exist_ok=True)
        (native / "config" / "provisioning-hashes.json").write_text(
            json.dumps(recorded), encoding="utf-8"
        )
    return native


def _grafana(
    *,
    dashboard: tuple[int, Any] = (200, {"meta": {"folderTitle": "Meiva"}}),
    folders: tuple[int, Any] = (200, [{"uid": "meiva-ops", "title": "Meiva"}]),
) -> Any:
    def get(url: str) -> tuple[int, Any]:
        return folders if url.endswith("/api/folders") else dashboard

    return get


def _healthy_recorded() -> dict[str, Any]:
    return {_REL: "sha", "$dashboard-source": "render"}


def test_verify_accepts_a_healthy_station(tmp_path: Path) -> None:
    native = _station(tmp_path, recorded=_healthy_recorded())

    assert (
        provisioning.verify_dashboard_provisioning(
            native, grafana_url="http://grafana.test", get=_grafana()
        )
        == []
    )


def test_verify_reports_each_broken_layer(tmp_path: Path) -> None:
    native = _station(
        tmp_path,
        document={"uid": "ava-ops-main", "panels": []},
        recorded={
            _REL: "sha",
            "$dashboard-source": "reference",
            "dashboards/ava-ops-main.json": "x",
        },
    )

    problems = provisioning.verify_dashboard_provisioning(
        native,
        grafana_url="http://grafana.test",
        get=_grafana(
            dashboard=(200, {"meta": {"folderTitle": "Ava"}}),
            folders=(200, [{"uid": "ava", "title": "Ava"}]),
        ),
    )

    assert problems == [
        "dashboard uid is 'ava-ops-main', expected 'meiva-ops-main'",
        "dashboard carries no panels",
        "the installed dashboard is the checkout reference, not a render",
        "provisioning-hashes.json still records the pre-rename dashboards/ava-ops-main.json",
        "dashboard sits in folder 'Ava', expected 'Meiva'",
        "pre-rename folder still present: 'Ava' (ava)",
    ]


def test_verify_reports_a_missing_dashboard_file(tmp_path: Path) -> None:
    native = _station(tmp_path, document=None, recorded=_healthy_recorded())

    problems = provisioning.verify_dashboard_provisioning(
        native, grafana_url="http://grafana.test", get=_grafana(dashboard=(404, {}))
    )

    assert problems == [
        f"dashboard file is missing or not a JSON object: {_dashboard(native)}",
        "Grafana answered 404 for dashboard uid 'meiva-ops-main'",
    ]


def test_verify_reports_grafana_that_cannot_be_read(tmp_path: Path) -> None:
    native = _station(tmp_path, recorded=_healthy_recorded())

    problems = provisioning.verify_dashboard_provisioning(
        native, grafana_url="http://grafana.test", get=lambda _url: (0, "ConnectError: refused")
    )

    assert problems == ["Grafana is unreachable at http://grafana.test: ConnectError: refused"]


def test_dashboard_folder_matches_the_provisioning_configuration() -> None:
    dashboards_yml = (
        _REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/dashboards.yml"
    ).read_text(encoding="utf-8")

    assert f"folder: {provisioning.DASHBOARD_FOLDER}" in dashboards_yml


def test_converge_drops_sidecar_entries_with_no_rendered_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    native = tmp_path / "native"
    (native / "config").mkdir(parents=True)
    (native / "config" / "provisioning-hashes.json").write_text(
        json.dumps({"dashboards/ava-ops-main.json": "sha"}), encoding="utf-8"
    )

    _render(_repo(tmp_path), native)

    hashes = _hashes(native)
    assert "dashboards/ava-ops-main.json" not in hashes
    assert _REL in hashes
    assert "dropped hash entries with no rendered file" in capsys.readouterr().out
