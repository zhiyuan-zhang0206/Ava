"""`contributions.ui` validation — the U1 slice of
`future/frontend-plugin-contributions.md`.

Every check here goes through the manifest validator (`pm._validate`) rather
than the sub-validator alone: `contributions.ui` is only worth anything if a
real `ava-plugin.json` carrying it validates, and the closed sets only hold if
the manifest layer refuses what they exclude.
"""

import re
from pathlib import Path
from typing import Any, cast

import pytest

from shared import plugin_manifest as pm
from shared import plugin_ui_contributions as ui
from shared.plugin_manifest import ManifestError

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GLOBALS_CSS = _REPO_ROOT / "ui" / "web" / "src" / "app" / "globals.css"


def _ui(**contributions: object) -> dict[str, object]:
    """A manifest carrying one `contributions.ui` declaration, validated."""
    manifest = pm._validate(
        cast(
            dict[str, Any],
            {
                "apiVersion": 2,
                "name": "acme",
                "version": "1.0.0",
                "engines": {"ava": ">=0.1.0"},
                "contributions": {"ui": contributions},
            },
        )
    )
    return cast(dict[str, object], manifest.contributions["ui"])


def _rejects(match: str, **contributions: object) -> None:
    with pytest.raises(ManifestError, match=match):
        _ui(**contributions)


# ── the token vocabulary is the console's own ──────────────────────────


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


def test_nav_icons_are_lucide_names() -> None:
    """Icon names are data — kebab-case lucide spellings the frontend maps."""
    for name in ui.NAV_ICONS:
        assert re.match(r"^[a-z][a-z0-9-]*$", name), name
    assert len(set(ui.NAV_ICONS)) == len(ui.NAV_ICONS)


# ── the declaration validates ──────────────────────────────────────────


def test_full_declaration_normalizes() -> None:
    parsed = _ui(
        agentInspect=[{"title": "Memory pool", "source": "api/inspect", "render": "kv"}],
        nav=[{"location": "sidebar", "label": "Task board", "icon": "kanban", "page": "board/"}],
        themes=[{"name": "solarized", "tokens": {"--background": "oklch(0.99 0.02 90)"}}],
    )
    assert parsed == {
        "agentInspect": [{"title": "Memory pool", "source": "api/inspect", "render": "kv"}],
        "nav": [{"location": "sidebar", "label": "Task board", "icon": "kanban", "page": "board/"}],
        "themes": [{"name": "solarized", "tokens": {"--background": "oklch(0.99 0.02 90)"}}],
    }


def test_ui_is_a_declared_contribution_key() -> None:
    assert "ui" in pm.CONTRIBUTION_KEYS
    assert _ui() == {}


def test_partial_token_pack_is_allowed() -> None:
    """Unset tokens keep the console default — a skin need not be exhaustive."""
    parsed = _ui(themes=[{"name": "warm", "tokens": {"--primary": "#a31515"}}])
    assert parsed["themes"] == [{"name": "warm", "tokens": {"--primary": "#a31515"}}]


def test_every_renderer_and_location_is_accepted() -> None:
    for render in ui.INSPECT_RENDERERS:
        _ui(agentInspect=[{"title": "T", "source": "x", "render": render}])
    for location in ui.NAV_LOCATIONS:
        _ui(nav=[{"location": location, "label": "L", "icon": "activity", "page": "p"}])


# ── the closed sets are closed ─────────────────────────────────────────


def test_unknown_contribution_type_fails() -> None:
    _rejects("unknown contribution type 'widgets'", widgets=[])


def test_unknown_renderer_and_location_fail() -> None:
    _rejects("not one of", agentInspect=[{"title": "T", "source": "x", "render": "chart"}])
    _rejects("not one of", nav=[{"location": "modal", "label": "L", "icon": "zap", "page": "p"}])


def test_unknown_icon_fails() -> None:
    _rejects("not one of", nav=[{"location": "sidebar", "label": "L", "icon": "pet", "page": "p"}])


def test_unknown_theme_token_fails() -> None:
    _rejects("unknown token", themes=[{"name": "x", "tokens": {"--wallpaper": "#fff"}}])


def test_non_themable_token_fails() -> None:
    """`--radius` is layout geometry, not a color — a skin may not move it."""
    _rejects("not themable", themes=[{"name": "x", "tokens": {"--radius": "2rem"}}])


# ── values are values, never CSS ───────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "var(--evil)",
        "url(https://example.com/x.png)",
        "red",
        "oklch(0.5 0 0); position: fixed",
        "oklch(0.5 0 0)}html{display:none",
        "#ff",
        "#12345",
        "rgb(1 2 3)",
        "",
    ],
)
def test_token_value_must_be_a_color_literal(value: str) -> None:
    _rejects("not a color literal", themes=[{"name": "x", "tokens": {"--background": value}}])


@pytest.mark.parametrize(
    "value",
    [
        "oklch(1 0 0)",
        "oklch(0.577 0.245 27.325)",
        "oklch(1 0 0 / 10%)",
        "oklch(1 0 0 / 0.1)",
        "oklch(0.5 0.1 120deg)",
        "#fff",
        "#ffffff",
        "#ffffff80",
    ],
)
def test_color_literal_forms_accepted(value: str) -> None:
    parsed = _ui(themes=[{"name": "x", "tokens": {"--background": value}}])
    assert cast(list[dict[str, Any]], parsed["themes"])[0]["tokens"]["--background"] == value


# ── paths name a place inside the plugin's own mount ───────────────────


@pytest.mark.parametrize(
    "path",
    ["/absolute", "../escape", "a/../b", "http://evil.example/x", "a\\b", "a/b?c=1", "a/b#frag"],
)
def test_source_path_must_stay_under_the_mount(path: str) -> None:
    _rejects(
        "(must be relative|invalid path segment|may not contain)",
        agentInspect=[{"title": "T", "source": path, "render": "markdown"}],
    )


@pytest.mark.parametrize("path", ["board/", "api/inspect", "x", "a/b/c.html", "a-b_c.~1"])
def test_mount_relative_paths_accepted(path: str) -> None:
    _ui(agentInspect=[{"title": "T", "source": path, "render": "markdown"}])


# ── entry shape ────────────────────────────────────────────────────────


def test_missing_and_unknown_entry_fields_fail() -> None:
    _rejects("missing required field 'render'", agentInspect=[{"title": "T", "source": "x"}])
    _rejects(
        "unknown field 'script'",
        agentInspect=[{"title": "T", "source": "x", "render": "kv", "script": "x.js"}],
    )
    _rejects("expected an object", nav=["sidebar"])
    _rejects("expected a list", themes={"name": "x"})


def test_ui_must_be_an_object() -> None:
    with pytest.raises(ManifestError, match=r"contributions\.ui must be an object"):
        pm._validate(
            cast(
                dict[str, Any],
                {
                    "apiVersion": 2,
                    "name": "acme",
                    "version": "1.0.0",
                    "engines": {"ava": ">=0.1.0"},
                    "contributions": {"ui": ["themes"]},
                },
            )
        )


def test_theme_name_format_and_duplicates() -> None:
    _rejects("must match", themes=[{"name": "Solarized", "tokens": {"--background": "#fff"}}])
    _rejects(
        "duplicate theme 'x'",
        themes=[
            {"name": "x", "tokens": {"--background": "#fff"}},
            {"name": "x", "tokens": {"--background": "#000"}},
        ],
    )


def test_empty_token_pack_fails() -> None:
    empty: dict[str, str] = {}
    _rejects("at least one token", themes=[{"name": "x", "tokens": empty}])


def test_every_problem_is_reported_at_once() -> None:
    """One report, like the rest of the manifest validator."""
    with pytest.raises(ManifestError) as exc:
        _ui(
            agentInspect=[{"title": "", "source": "../x", "render": "chart"}],
            nav=[{"location": "modal", "label": "L", "icon": "pet", "page": "p"}],
        )
    text = str(exc.value)
    assert "agentInspect[0].title" in text
    assert "agentInspect[0].source" in text
    assert "agentInspect[0].render" in text
    assert "nav[0].location" in text
    assert "nav[0].icon" in text
