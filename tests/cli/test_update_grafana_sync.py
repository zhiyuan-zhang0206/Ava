"""`_sync_grafana_provisioning` — dashboards refresh hook in `ava cluster update` (#975).

Bug: the term-alignment batch renamed `events.kind` → `event_name` and updated
the repo dashboard sources (`dashboards/ops/*.json`, `dashboards/ops/alerts/
rules.yml`), but the runtime Grafana provisioning copies were only refreshed by
hand — after the deploy every panel died with a db query error. The hook runs
after checkout+uv-sync in the gateway local leg, regenerating the dashboards
from the just-checked-out tree and re-copying the alert rules, so a rollout
carries its dashboard refresh with it. Non-fatal by design.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cli.commands.update import _sync_grafana_provisioning
from shared.config import settings


def _make_layout(root: Path, *, with_grafana: bool) -> Path:
    repo = root / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / "dashboards" / "ops" / "alerts").mkdir(parents=True)
    (repo / "dashboards" / "ops" / "alerts" / "rules.yml").write_text("rules: []\n")
    (repo / "dashboards" / "ops" / "alerts" / "contact.yml").write_text("contact: []\n")
    (repo / "dashboards" / "ops" / "alerts" / "datasources.yml").write_text("datasources: []\n")
    if with_grafana:
        prov = root / "home" / "grafana" / "provisioning"
        (prov / "dashboards").mkdir(parents=True)
        (prov / "alerting").mkdir(parents=True)
        (prov / "datasources").mkdir(parents=True)
    return repo


def test_skips_when_no_grafana_provisioning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """An agent-runner (no Grafana provisioning dir) is a no-op — no generator
    subprocess, no copy."""
    repo = _make_layout(tmp_path, with_grafana=False)
    monkeypatch.setattr(settings.general, "ava_home", str(tmp_path / "home"))
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)  # pyright: ignore[reportUnknownArgumentType]
        return subprocess.CompletedProcess(cmd, 0)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    _sync_grafana_provisioning(repo)
    assert calls == []
    assert not (tmp_path / "home" / "grafana").exists()


def test_syncs_dashboards_and_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On the Grafana host: the generator runs against the new tree and the
    alert rules are copied into the runtime provisioning dir."""
    repo = _make_layout(tmp_path, with_grafana=True)
    home = tmp_path / "home"
    monkeypatch.setattr(settings.general, "ava_home", str(home))
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)  # pyright: ignore[reportUnknownArgumentType]
        return subprocess.CompletedProcess(cmd, 0)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    _sync_grafana_provisioning(repo)

    assert len(calls) == 1
    assert calls[0][0] == str(repo / ".venv" / "bin" / "python")
    assert any("gen_plugin_dashboard.py" in c for c in calls[0])
    dst = home / "grafana" / "provisioning" / "alerting" / "rules.yml"
    assert dst.read_text() == "rules: []\n"
    contact = home / "grafana" / "provisioning" / "alerting" / "contact.yml"
    assert contact.read_text() == "contact: []\n"
    datasources = home / "grafana" / "provisioning" / "datasources" / "datasources.yml"
    assert datasources.read_text() == "datasources: []\n"


def test_generator_failure_is_non_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A failing generator must not abort the rollout — it warns and returns
    without copying rules."""
    repo = _make_layout(tmp_path, with_grafana=True)
    monkeypatch.setattr(settings.general, "ava_home", str(tmp_path / "home"))

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    _sync_grafana_provisioning(repo)
    captured = capsys.readouterr()  # pyright: ignore[reportUnknownMemberType]
    assert "gen_plugin_dashboard.py failed" in captured.err  # pyright: ignore[reportUnknownMemberType]
    dst = tmp_path / "home" / "grafana" / "provisioning" / "alerting" / "rules.yml"
    assert not dst.exists()
    contact = tmp_path / "home" / "grafana" / "provisioning" / "alerting" / "contact.yml"
    assert not contact.exists()
