"""Contract: the themable token set equals ui/web/src/app/globals.css :root."""

import re
from pathlib import Path

from base.packages.plugins import ui_contributions as ui

_REPO_ROOT = Path(__file__).resolve().parents[3]


_GLOBALS_CSS = _REPO_ROOT / "ui" / "web" / "src" / "app" / "globals.css"


def test_theme_tokens_match_globals_css() -> None:
    """The themable set is exactly `globals.css` `:root` minus the non-colors.

    The point of a token pack is that it re-values properties the console
    already renders through. If the console grows a token and this tuple does
    not, skins silently cannot reach it; if this tuple names one the console
    dropped, a skin sets a property nothing reads. Either way the drift is
    invisible at runtime, so it is caught here.
    """
    body = re.search(r"^:root \{\n(.*?)^\}", _GLOBALS_CSS.read_text(encoding="utf-8"), re.S | re.M)
    assert body is not None, f"no :root block in {_GLOBALS_CSS}"
    declared = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", body[1], re.M))
    assert declared - set(ui.NON_THEMABLE_TOKENS) == set(ui.THEME_TOKENS)


def test_non_themable_tokens_are_real_properties() -> None:
    """An exclusion for a property that no longer exists hides a real gap."""
    body = re.search(r"^:root \{\n(.*?)^\}", _GLOBALS_CSS.read_text(encoding="utf-8"), re.S | re.M)
    assert body is not None
    declared = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", body[1], re.M))
    assert set(ui.NON_THEMABLE_TOKENS) <= declared
