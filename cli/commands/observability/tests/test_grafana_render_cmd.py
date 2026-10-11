"""`ava lgtm render` — the rendered dashboard command (task #3697 S1).

The command is the operator surface of the renderer: diff the render against
this host's provisioning copy (exit 1 when it differs), write it with
``--force``, and refuse cleanly on a host without the native LGTM tree.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cli.commands.observability import grafana_render
from tests.path_scoped.cli_tests import operator_database as operator_database


@pytest.fixture()
def target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "dashboards" / "ava-ops-main.json"
    target.parent.mkdir(parents=True)

    def _provisioning_dashboard(_home: Path) -> Path:
        return target

    monkeypatch.setattr(grafana_render, "_provisioning_dashboard", _provisioning_dashboard)
    return target


def test_render_cmd_previews_writes_and_refreshes(
    target: Path, capsys: pytest.CaptureFixture[str], operator_database: Callable[[], Any]
) -> None:
    # Absent target: the diff preview reports what it would create, exit 1.
    assert (
        grafana_render.cmd_grafana_render(
            force=False, repo_only=True, database_factory=operator_database
        )
        == 1
    )
    assert not target.is_file()

    # --force writes the render.
    assert (
        grafana_render.cmd_grafana_render(
            force=True, repo_only=True, database_factory=operator_database
        )
        == 0
    )
    written = target.read_text(encoding="utf-8")
    assert '"uid": "ava-ops-main"' in written

    # A second pass finds the provisioning copy in sync.
    assert (
        grafana_render.cmd_grafana_render(
            force=False, repo_only=True, database_factory=operator_database
        )
        == 0
    )

    # A drifted file is reported (exit 1) and left untouched in diff mode.
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

    # --force refreshes it back to the render.
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
    target = tmp_path / "missing" / "dashboards" / "ava-ops-main.json"

    def _provisioning_dashboard(_home: Path) -> Path:
        return target

    monkeypatch.setattr(grafana_render, "_provisioning_dashboard", _provisioning_dashboard)
    assert (
        grafana_render.cmd_grafana_render(
            force=False, repo_only=True, database_factory=operator_database
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "does not run the LGTM observability stack" in err
