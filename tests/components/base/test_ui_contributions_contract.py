import re
from pathlib import Path

from base.packages.plugins.surface import ui_contributions as ui

_REPO_ROOT = Path(__file__).resolve().parents[3]


_GLOBALS_CSS = _REPO_ROOT / "ui" / "web" / "src" / "app" / "globals.css"


def test_theme_tokens_match_globals_css() -> None:
    body = re.search(r"^:root \{\n(.*?)^\}", _GLOBALS_CSS.read_text(encoding="utf-8"), re.S | re.M)
    assert body is not None, f"no :root block in {_GLOBALS_CSS}"
    declared = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", body[1], re.M))
    assert declared - set(ui.NON_THEMABLE_TOKENS) == set(ui.THEME_TOKENS)


def test_non_themable_tokens_are_real_properties() -> None:
    body = re.search(r"^:root \{\n(.*?)^\}", _GLOBALS_CSS.read_text(encoding="utf-8"), re.S | re.M)
    assert body is not None
    declared = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", body[1], re.M))
    assert set(ui.NON_THEMABLE_TOKENS) <= declared
