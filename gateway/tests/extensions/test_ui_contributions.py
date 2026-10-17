import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import gateway.app as gateway_app
from base import paths
from base.lm.catalog import ModelCatalog
from base.packages.plugins.enable_config import write_local
from tests.fixtures.gateway_config import gateway_test_client


@pytest.fixture(autouse=True)
def _isolate_plugin_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_catalog: ModelCatalog
) -> None:
    monkeypatch.setattr(gateway_app, "build_model_catalog", lambda: model_catalog)
    repo = tmp_path / "repo_plugins"
    user = tmp_path / "user_plugins"
    repo.mkdir()
    user.mkdir()
    monkeypatch.setattr(paths, "repo_plugins_dir", lambda: repo)
    monkeypatch.setattr(paths, "plugins_dir", lambda: user)
    monkeypatch.setenv("MEIVA_HOME", str(tmp_path / "meiva"))


def _write_plugin(
    name: str,
    ui: dict[str, object] | None,
    *,
    raw: str | None = None,
    manifest: bool = True,
) -> None:
    directory = paths.repo_plugins_dir() / name
    directory.mkdir()
    (directory / "plugin.py").write_text('"""A test plugin."""\n', encoding="utf-8")
    if raw is not None:
        (directory / "meiva-plugin.json").write_text(raw, encoding="utf-8")
        return
    if not manifest:
        return
    manifest_data = {
        "apiVersion": 2,
        "name": name.replace("_", "-"),
        "version": "1.0.0",
        "engines": {"meiva": ">=0.1.0"},
        "contributions": {} if ui is None else {"ui": ui},
    }
    (directory / "meiva-plugin.json").write_text(json.dumps(manifest_data), encoding="utf-8")


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

    with gateway_test_client() as client:
        assert _themes(client) == [
            {
                "plugin": "skins",
                "name": "solarized",
                "tokens": {"--background": "oklch(0.99 0.02 90)"},
                "dark_tokens": None,
            }
        ]


def test_a_packs_dark_half_reaches_the_console() -> None:
    _write_plugin(
        "skins",
        {
            "themes": [
                {
                    "name": "solarized",
                    "tokens": {"--background": "#fdf6e3"},
                    "darkTokens": {"--background": "#002b36"},
                }
            ]
        },
    )
    write_local({"plugins": {"skins": {"enabled": True}}})

    with gateway_test_client() as client:
        assert _themes(client) == [
            {
                "plugin": "skins",
                "name": "solarized",
                "tokens": {"--background": "#fdf6e3"},
                "dark_tokens": {"--background": "#002b36"},
            }
        ]


def test_disabled_plugin_contributes_nothing() -> None:
    _write_plugin("skins", {"themes": [{"name": "solarized", "tokens": {"--primary": "#111111"}}]})
    write_local({"plugins": {"skins": {"enabled": False}}})

    with gateway_test_client() as client:
        assert _themes(client) == []


def test_two_plugins_merge_and_same_name_stays_distinguishable() -> None:
    _write_plugin("skins", {"themes": [{"name": "solarized", "tokens": {"--primary": "#111111"}}]})
    _write_plugin("other", {"themes": [{"name": "solarized", "tokens": {"--primary": "#222222"}}]})
    write_local({"plugins": {"skins": {"enabled": True}, "other": {"enabled": True}}})

    with gateway_test_client() as client:
        by_plugin = {t["plugin"]: t for t in _themes(client)}
    assert set(by_plugin) == {"skins", "other"}
    assert by_plugin["skins"]["tokens"] == {"--primary": "#111111"}
    assert by_plugin["other"]["tokens"] == {"--primary": "#222222"}
    assert {t["name"] for t in by_plugin.values()} == {"solarized"}


def test_plugin_without_ui_is_skipped() -> None:
    _write_plugin("plain", None)
    _write_plugin("declares_other", {})
    write_local({"plugins": {"plain": {"enabled": True}, "declares_other": {"enabled": True}}})

    with gateway_test_client() as client:
        assert _themes(client) == []


def test_plugin_without_a_manifest_fails_the_endpoint_naming_it() -> None:
    _write_plugin("plain", None, manifest=False)
    write_local({"plugins": {"plain": {"enabled": True}}})

    with gateway_test_client() as client:
        r = client.get("/api/ui/contributions")
    assert r.status_code == 500, r.text
    assert "plain" in r.text
    assert "meiva-plugin.json not found" in r.text


def _stats(client: TestClient) -> list[dict[str, object]]:
    r = client.get("/api/ui/contributions")
    assert r.status_code == 200, r.text
    return r.json()["stats"]


def test_stat_cards_reach_the_console_attributed() -> None:
    _write_plugin(
        "usage",
        {
            "stats": [
                {"id": "codex-zhang0206", "label": "Codex zhiyuan0206"},
                {"id": "deepseek-balance", "label": "DeepSeek balance"},
            ]
        },
    )
    write_local({"plugins": {"usage": {"enabled": True}}})

    with gateway_test_client() as client:
        assert _stats(client) == [
            {"plugin": "usage", "id": "codex-zhang0206", "label": "Codex zhiyuan0206"},
            {"plugin": "usage", "id": "deepseek-balance", "label": "DeepSeek balance"},
        ]


def test_stat_cards_from_two_plugins_merge_in_plugin_order() -> None:
    _write_plugin("deepseek", {"stats": [{"id": "balance", "label": "DeepSeek balance"}]})
    _write_plugin("codex", {"stats": [{"id": "codex-a", "label": "Codex A"}]})
    write_local({"plugins": {"deepseek": {"enabled": True}, "codex": {"enabled": True}}})

    with gateway_test_client() as client:
        assert [(s["plugin"], s["id"]) for s in _stats(client)] == [
            ("codex", "codex-a"),
            ("deepseek", "balance"),
        ]


def test_plugin_without_stat_cards_yields_an_empty_list() -> None:
    _write_plugin("skins", {"themes": [{"name": "x", "tokens": {"--primary": "#111111"}}]})
    write_local({"plugins": {"skins": {"enabled": True}}})

    with gateway_test_client() as client:
        assert _stats(client) == []


def test_invalid_manifest_names_the_plugin() -> None:
    _write_plugin(
        "broken",
        None,
        raw=json.dumps(
            {
                "apiVersion": 2,
                "name": "broken",
                "version": "1.0.0",
                "engines": {"meiva": ">=0.1.0"},
                "contributions": {"ui": {"themes": [{"name": "x", "tokens": {"--nope": "#fff"}}]}},
            }
        ),
    )
    write_local({"plugins": {"broken": {"enabled": True}}})

    with gateway_test_client() as client:
        r = client.get("/api/ui/contributions")
    assert r.status_code == 500, r.text
    assert "broken" in r.text
    assert "--nope" in r.text
