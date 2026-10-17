from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "ui" / "app" / "src-tauri" / "scripts"
_WINDOW_RS = _REPO_ROOT / "ui" / "app" / "src-tauri" / "src" / "window.rs"


def test_no_injected_script_opens_the_app_settings_page() -> None:
    for script in sorted(_SCRIPTS.glob("*.js")):
        assert "app_open_settings" not in script.read_text(encoding="utf-8"), script.name


def test_the_console_gets_only_the_navigation_and_update_scripts() -> None:
    injected = [
        line.split("scripts/", 1)[1].split('"', 1)[0]
        for line in _WINDOW_RS.read_text(encoding="utf-8").splitlines()
        if "initialization_script(include_str!" in line
    ]
    assert injected == ["nav-guard.js", "notify-bridge.js", "update-check.js"]
