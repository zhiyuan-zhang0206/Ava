from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.paths import meiva_home
from cli.commands.observability import grafana_render
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database


@pytest.fixture()
def target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "dashboards" / "meiva-ops-main.json"
    target.parent.mkdir(parents=True)

    def _provisioning_dashboard(_home: Path) -> Path:
        return target

    monkeypatch.setattr(grafana_render, "provisioning_dashboard", _provisioning_dashboard)
    return target


def test_render_cmd_previews_writes_and_refreshes(
    target: Path, capsys: pytest.CaptureFixture[str], operator_database: Callable[[], Any]
) -> None:
    assert (
        grafana_render.cmd_grafana_render(
            force=False, repo_only=True, database_factory=operator_database
        )
        == 1
    )
    assert not target.is_file()

    assert (
        grafana_render.cmd_grafana_render(
            force=True, repo_only=True, database_factory=operator_database
        )
        == 0
    )
    written = target.read_text(encoding="utf-8")
    assert '"uid": "meiva-ops-main"' in written

    assert (
        grafana_render.cmd_grafana_render(
            force=False, repo_only=True, database_factory=operator_database
        )
        == 0
    )

    drifted = written.replace("LLM calls (window)", "LLM calls (window) EDITED")
    assert drifted != written
    target.write_text(drifted, encoding="utf-8")
    assert (
        grafana_render.cmd_grafana_render(
            force=False, repo_only=True, database_factory=operator_database
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "render differs" in err
    assert target.read_text(encoding="utf-8") == drifted

    assert (
        grafana_render.cmd_grafana_render(
            force=True, repo_only=True, database_factory=operator_database
        )
        == 0
    )
    assert target.read_text(encoding="utf-8") == written


def test_render_cmd_refuses_a_host_without_the_lgtm_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
) -> None:
    target = tmp_path / "missing" / "dashboards" / "meiva-ops-main.json"

    def _provisioning_dashboard(_home: Path) -> Path:
        return target

    monkeypatch.setattr(grafana_render, "provisioning_dashboard", _provisioning_dashboard)
    assert (
        grafana_render.cmd_grafana_render(
            force=False, repo_only=True, database_factory=operator_database
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "does not run the LGTM observability stack" in err


def test_render_cmd_force_records_the_dashboard_hash(
    target: Path, operator_database: Callable[[], Any]
) -> None:
    assert (
        grafana_render.cmd_grafana_render(
            force=True, repo_only=True, database_factory=operator_database
        )
        == 0
    )

    hashes_path = meiva_home() / "lgtm/native/config/provisioning-hashes.json"
    hashes = json.loads(hashes_path.read_text(encoding="utf-8"))
    assert (
        hashes["dashboards/meiva-ops-main.json"] == hashlib.sha256(target.read_bytes()).hexdigest()
    )
    assert hashes["$dashboard-source"] == "render"


def test_render_cmd_records_the_hash_when_the_file_is_already_in_sync(
    target: Path, operator_database: Callable[[], Any]
) -> None:
    assert (
        grafana_render.cmd_grafana_render(
            force=True, repo_only=True, database_factory=operator_database
        )
        == 0
    )
    hashes_path = meiva_home() / "lgtm/native/config/provisioning-hashes.json"
    hashes_path.unlink()

    assert (
        grafana_render.cmd_grafana_render(
            force=False, repo_only=True, database_factory=operator_database
        )
        == 0
    )

    hashes = json.loads(hashes_path.read_text(encoding="utf-8"))
    assert (
        hashes["dashboards/meiva-ops-main.json"] == hashlib.sha256(target.read_bytes()).hexdigest()
    )
    assert hashes["$dashboard-source"] == "render"
