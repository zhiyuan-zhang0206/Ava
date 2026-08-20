"""GET /api/plugin-ui/{plugin}/... — the plugin page mount.

Real plugin packages on disk with real `ui/` directories, read back through the
endpoint: what a plugin ships is what the console can embed, and what it does
not ship is a 404 rather than a path into the rest of the filesystem.
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


def _write_plugin(name: str, *, files: dict[str, str] | None = None) -> Path:
    """A plugin package, with `files` laid out under its `ui/` directory."""
    directory = paths.repo_plugins_dir() / name
    directory.mkdir()
    (directory / "plugin.py").write_text('"""A test plugin."""\n', encoding="utf-8")
    for rel, content in (files or {}).items():
        target = directory / "ui" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return directory


_BOARD = "<!doctype html><title>Board</title><h1>Task board</h1>"


def test_serves_the_mount_root_index() -> None:
    _write_plugin("board", files={"index.html": _BOARD})
    write_local({"plugins": {"board": {"enabled": True}}})

    with TestClient(app) as client:
        r = client.get("/api/plugin-ui/board/")
    assert r.status_code == 200, r.text
    assert r.text == _BOARD
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["x-content-type-options"] == "nosniff"


def test_bare_mount_redirects_to_the_trailing_slash() -> None:
    """A page's relative asset links have to resolve against the mount."""
    _write_plugin("board", files={"index.html": _BOARD})
    write_local({"plugins": {"board": {"enabled": True}}})

    with TestClient(app) as client:
        r = client.get("/api/plugin-ui/board", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/api/plugin-ui/board/"


def test_serves_a_subdirectory_index_and_a_nested_asset() -> None:
    """`page: "board/"` in a nav declaration is a directory URL, and the assets
    beside it come through the same mount."""
    _write_plugin(
        "board",
        files={"board/index.html": _BOARD, "board/app.js": "console.log(1)\n"},
    )
    write_local({"plugins": {"board": {"enabled": True}}})

    with TestClient(app) as client:
        page = client.get("/api/plugin-ui/board/board/")
        asset = client.get("/api/plugin-ui/board/board/app.js")
    assert page.status_code == 200, page.text
    assert page.text == _BOARD
    assert asset.status_code == 200, asset.text
    assert asset.text == "console.log(1)\n"
    assert asset.headers["content-type"].startswith("text/javascript")


def test_directory_without_a_trailing_slash_redirects() -> None:
    """A page's relative asset links resolve against the URL it loaded from, so
    `…/board` would send `app.js` one level above where the plugin put it."""
    _write_plugin("board", files={"board/index.html": _BOARD, "board/app.js": "1\n"})
    write_local({"plugins": {"board": {"enabled": True}}})

    with TestClient(app) as client:
        r = client.get("/api/plugin-ui/board/board", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/api/plugin-ui/board/board/"


def test_dash_spelled_plugin_name_reaches_the_underscore_directory() -> None:
    """The directory is a Python package; the name a URL carries is dash-spelled."""
    _write_plugin("ava_board", files={"index.html": _BOARD})
    write_local({"plugins": {"ava_board": {"enabled": True}}})

    with TestClient(app) as client:
        r = client.get("/api/plugin-ui/ava-board/")
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("path", ["", "index.html"])
def test_disabled_plugin_has_no_mount(path: str) -> None:
    _write_plugin("board", files={"index.html": _BOARD})
    write_local({"plugins": {"board": {"enabled": False}}})

    with TestClient(app) as client:
        r = client.get(f"/api/plugin-ui/board/{path}")
    assert r.status_code == 404, r.text


def test_unknown_plugin_and_a_plugin_without_pages_answer_alike() -> None:
    """Same 404 detail for both: the mount does not report a plugin's
    enable-state (or existence) to a caller that could not otherwise see it."""
    _write_plugin("plain")
    write_local({"plugins": {"plain": {"enabled": True}}})

    with TestClient(app) as client:
        missing = client.get("/api/plugin-ui/nope/")
        no_pages = client.get("/api/plugin-ui/plain/")
    assert missing.status_code == 404
    assert no_pages.status_code == 404
    assert missing.json()["detail"] == "no plugin page mount for 'nope'"
    assert no_pages.json()["detail"] == "no plugin page mount for 'plain'"


# Percent-encoded, because an HTTP client collapses a literal `..` before the
# request leaves it — the segment guard exists for the paths that do arrive.
@pytest.mark.parametrize("rest", ["%2e%2e/plugin.py", "board/%2e%2e/%2e%2e/plugin.py", "a\\b"])
def test_traversal_segments_are_refused(rest: str) -> None:
    _write_plugin("board", files={"index.html": _BOARD})
    write_local({"plugins": {"board": {"enabled": True}}})

    with TestClient(app) as client:
        r = client.get(f"/api/plugin-ui/board/{rest}")
    assert r.status_code == 400, r.text
    assert "invalid page path segment" in r.text


def test_a_symlink_out_of_the_ui_dir_is_refused() -> None:
    """The segment check cannot see this one — only the resolved path can."""
    directory = _write_plugin("board", files={"index.html": _BOARD})
    secret = directory.parent.parent / "secret.txt"
    secret.write_text("cluster secret\n", encoding="utf-8")
    (directory / "ui" / "escape.txt").symlink_to(secret)
    write_local({"plugins": {"board": {"enabled": True}}})

    with TestClient(app) as client:
        r = client.get("/api/plugin-ui/board/escape.txt")
    assert r.status_code == 404, r.text
    assert "cluster secret" not in r.text


def test_missing_file_is_404() -> None:
    _write_plugin("board", files={"index.html": _BOARD})
    write_local({"plugins": {"board": {"enabled": True}}})

    with TestClient(app) as client:
        r = client.get("/api/plugin-ui/board/nope.html")
    assert r.status_code == 404, r.text


def test_nav_declarations_reach_the_console_attributed() -> None:
    """The mount serves the page; the aggregation endpoint says where the
    console links to it."""
    directory = _write_plugin("board", files={"board/index.html": _BOARD})
    (directory / "ava-plugin.json").write_text(
        json.dumps(
            {
                "apiVersion": 2,
                "name": "board",
                "version": "1.0.0",
                "engines": {"ava": ">=0.1.0"},
                "contributions": {
                    "ui": {
                        "nav": [
                            {
                                "location": "sidebar",
                                "label": "Task board",
                                "icon": "kanban",
                                "page": "board/",
                            }
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    write_local({"plugins": {"board": {"enabled": True}}})

    with TestClient(app) as client:
        r = client.get("/api/ui/contributions")
    assert r.status_code == 200, r.text
    assert r.json()["nav"] == [
        {
            "plugin": "board",
            "location": "sidebar",
            "label": "Task board",
            "icon": "kanban",
            "page": "board/",
        }
    ]
