"""GET /api/ui/contributions — the console-contribution aggregate.

Plugins are written to disk with real `ava-plugin.json` manifests and read back
through the endpoint, so what the console would receive is what a manifest on
disk actually produces: the enabled set is honored, declarations are attributed
to the plugin that made them, and a manifest that no longer validates is loud.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared import paths
from shared.config import settings
from shared.plugins_config import write_local


@pytest.fixture(autouse=True)
def _isolate_plugin_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo_plugins"
    user = tmp_path / "user_plugins"
    repo.mkdir()
    user.mkdir()
    monkeypatch.setattr(paths, "repo_plugins_dir", lambda: repo)
    monkeypatch.setattr(paths, "plugins_dir", lambda: user)
    monkeypatch.setattr(paths, "plugins_config_path", lambda: tmp_path / "plugins.json")
    monkeypatch.setattr(settings.general, "ava_home", str(tmp_path / "ava"))


def _write_plugin(name: str, ui: dict[str, object] | None, *, raw: str | None = None) -> None:
    """A plugin directory with a `plugin.py` and, unless `raw` says otherwise, a
    manifest declaring `ui`."""
    directory = paths.repo_plugins_dir() / name
    directory.mkdir()
    (directory / "plugin.py").write_text('"""A test plugin."""\n', encoding="utf-8")
    if raw is not None:
        (directory / "ava-plugin.json").write_text(raw, encoding="utf-8")
        return
    if ui is None:
        return
    manifest = {
        "apiVersion": 2,
        "name": name.replace("_", "-"),
        "version": "1.0.0",
        "engines": {"ava": ">=0.1.0"},
        "contributions": {"ui": ui},
    }
    (directory / "ava-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")


def _themes(client: TestClient) -> list[dict[str, object]]:
    r = client.get("/api/ui/contributions")
    assert r.status_code == 200, r.text
    return r.json()["themes"]


def test_declared_theme_reaches_the_console_attributed() -> None:
    _write_plugin(
        "skins",
        {"themes": [{"name": "solarized", "tokens": {"--background": "oklch(0.99 0.02 90)"}}]},
    )
    write_local({"plugins": {"skins": {"enabled": True}}})

    with TestClient(app) as client:
        assert _themes(client) == [
            {
                "plugin": "skins",
                "name": "solarized",
                "tokens": {"--background": "oklch(0.99 0.02 90)"},
            }
        ]


def test_disabled_plugin_contributes_nothing() -> None:
    """The enabled set is the gate — a disabled plugin's skin is not offered."""
    _write_plugin("skins", {"themes": [{"name": "solarized", "tokens": {"--primary": "#111111"}}]})
    write_local({"plugins": {"skins": {"enabled": False}}})

    with TestClient(app) as client:
        assert _themes(client) == []


def test_two_plugins_merge_and_same_name_stays_distinguishable() -> None:
    """Names collide across plugins; attribution is what keeps them apart."""
    _write_plugin("skins", {"themes": [{"name": "solarized", "tokens": {"--primary": "#111111"}}]})
    _write_plugin("other", {"themes": [{"name": "solarized", "tokens": {"--primary": "#222222"}}]})
    write_local({"plugins": {"skins": {"enabled": True}, "other": {"enabled": True}}})

    with TestClient(app) as client:
        by_plugin = {t["plugin"]: t for t in _themes(client)}
    assert set(by_plugin) == {"skins", "other"}
    assert by_plugin["skins"]["tokens"] == {"--primary": "#111111"}
    assert by_plugin["other"]["tokens"] == {"--primary": "#222222"}
    assert {t["name"] for t in by_plugin.values()} == {"solarized"}


def test_plugin_without_a_manifest_or_without_ui_is_skipped() -> None:
    _write_plugin("plain", None)
    _write_plugin("declares_other", {})
    write_local({"plugins": {"plain": {"enabled": True}, "declares_other": {"enabled": True}}})

    with TestClient(app) as client:
        assert _themes(client) == []


def test_invalid_manifest_names_the_plugin() -> None:
    """The install gate already validated it, so a manifest that fails now means
    the on-disk copy changed under the cluster — loud, not skipped."""
    _write_plugin(
        "broken",
        None,
        raw=json.dumps(
            {
                "apiVersion": 2,
                "name": "broken",
                "version": "1.0.0",
                "engines": {"ava": ">=0.1.0"},
                "contributions": {"ui": {"themes": [{"name": "x", "tokens": {"--nope": "#fff"}}]}},
            }
        ),
    )
    write_local({"plugins": {"broken": {"enabled": True}}})

    with TestClient(app) as client:
        r = client.get("/api/ui/contributions")
    assert r.status_code == 500, r.text
    assert "broken" in r.text
    assert "--nope" in r.text
