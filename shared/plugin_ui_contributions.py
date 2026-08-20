"""`contributions.ui` — the declarative console-contribution schema (slice U1).

Design: [`future/frontend-plugin-contributions.md`](../future/frontend-plugin-contributions.md).
A plugin contributes to the web console as **data**: agent-inspect sections,
nav entries, and theme token packs that the console's own generic components
render. The frontend never executes third-party JavaScript as part of its own
composition, so every field validated here is a closed-set enum, a relative
path the gateway proxies, or a CSS color literal — nothing that can carry
behavior into the app bundle.

Declaration only, exactly like the shipped validator slices of plugin spec v2:
nothing in this module renders, mounts, or serves anything. The aggregation
endpoint, the page proxy, and the renderers are slices U2-U4. And like
`skills`/`commands`, UI contributions are consumed straight from the manifest
with no runtime `register_*` call, so the declared-vs-registered diff over
`shared/plugin_contributions.py` has no registered side to compare against.

Three vocabularies are closed, each for its own reason:

- `THEME_TOKENS` — the console's own custom properties
  (`ui/web/src/app/globals.css` `:root`). A skin re-values existing tokens; it
  cannot invent one, and it cannot ship a selector or a rule. That is what
  makes a skin survive UI refactors (the token layer is the stable interface,
  component markup is not) and incapable of injecting layout or behavior.
  `tests/shared/test_plugin_ui_contributions.py` locks the tuple against the
  stylesheet, so a token added to the console fails the suite until it is
  either offered to skins or listed as deliberately non-themable.
- `NAV_ICONS` — lucide icon names, i.e. data rather than markup: the frontend
  maps a name onto a component it already imports, so an unknown name has to
  be a validator error instead of a blank nav row.
- `UI_KEYS` / `INSPECT_RENDERERS` / `NAV_LOCATIONS` — v1 closed sets. Adding a
  contribution type is a deliberate change to this file *and* to the component
  that renders it; this is not an extension point.
"""

from __future__ import annotations

import re
from typing import Any, cast

# Contribution types (v1). Closed — see the module docstring.
UI_KEYS = ("agentInspect", "nav", "themes")

# How an agent-inspect section's `source` payload is rendered by the host's
# generic components. `page` is the escape hatch: the section embeds the
# plugin's own page in a sandboxed iframe instead of rendering data.
INSPECT_RENDERERS = ("markdown", "kv", "table", "page")

# Where a nav entry appears in the console.
NAV_LOCATIONS = ("sidebar", "settings", "fleet-toolbar")

# lucide icon names (kebab-case, the canonical lucide spelling). Curated
# rather than "any lucide name": the frontend imports the components it can
# render, so the vocabulary is exactly what it imports.
NAV_ICONS = (
    "activity",
    "app-window",
    "bell",
    "book-open",
    "bot",
    "calendar",
    "chart-column",
    "chart-line",
    "clock",
    "coins",
    "cpu",
    "database",
    "eye",
    "file-text",
    "folder",
    "gauge",
    "git-branch",
    "info",
    "kanban",
    "layers",
    "layout-dashboard",
    "list",
    "lock",
    "message-square",
    "monitor",
    "notebook-text",
    "package",
    "puzzle",
    "search",
    "server",
    "settings",
    "sparkles",
    "table",
    "terminal",
    "trending-up",
    "users",
    "wallet",
    "waypoints",
    "workflow",
    "zap",
)

# The themable custom properties of `ui/web/src/app/globals.css` `:root`.
THEME_TOKENS = (
    "--background",
    "--foreground",
    "--card",
    "--card-foreground",
    "--popover",
    "--popover-foreground",
    "--primary",
    "--primary-foreground",
    "--secondary",
    "--secondary-foreground",
    "--muted",
    "--muted-foreground",
    "--accent",
    "--accent-foreground",
    "--destructive",
    "--border",
    "--input",
    "--ring",
    "--chart-1",
    "--chart-2",
    "--chart-3",
    "--chart-4",
    "--chart-5",
    "--series-1",
    "--series-2",
    "--series-3",
    "--series-4",
    "--series-5",
    "--sidebar",
    "--sidebar-foreground",
    "--sidebar-primary",
    "--sidebar-primary-foreground",
    "--sidebar-accent",
    "--sidebar-accent-foreground",
    "--sidebar-border",
    "--sidebar-ring",
    "--syntax-keyword",
    "--syntax-string",
    "--syntax-comment",
    "--syntax-number",
    "--syntax-function",
    "--syntax-builtin",
    "--syntax-punct",
)

# `:root` properties a theme pack may NOT set, with the reason. A skin is a
# color pack; `--radius` is a length that the component geometry is tuned
# around, so re-valuing it is a layout change wearing a theme's clothes.
NON_THEMABLE_TOKENS = ("--radius",)

_THEME_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Color literals a token value may take: the two forms the console's own token
# layer uses. Deliberately narrow — a theme is a token pack, never CSS, and
# `var(...)` / `url(...)` / bare keywords are how a value stops being a value.
_NUM = r"-?(?:\d+(?:\.\d+)?|\.\d+)"
_OKLCH_RE = re.compile(
    rf"^oklch\(\s*{_NUM}%?\s+{_NUM}%?\s+{_NUM}(?:deg)?\s*(?:/\s*{_NUM}%?\s*)?\)$"
)
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

# One segment of a path under the plugin's own gateway mount. Same shape as
# the `pages.py` proxy already enforces on the URL it forwards, applied one
# step earlier: a path that could escape the mount is refused at validation
# instead of at request time.
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~-]+$")


def _entry_fields(
    entry: object,
    what: str,
    required: tuple[str, ...],
    errors: list[str],
    optional: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """The entry as a dict once it is an object with exactly the known fields.

    None when it is not usable at all (the caller skips it); missing/unknown
    fields are reported here so one bad entry yields one readable report.
    An `optional` field may be absent but may not be misspelled: it counts as
    known for the unknown-field report and is never required.
    """
    known = required + optional
    if not isinstance(entry, dict):
        errors.append(f"{what}: expected an object with {', '.join(required)}")
        return None
    data = cast(dict[str, Any], entry)
    for field in data:
        if field not in known:
            errors.append(f"{what}: unknown field {field!r} (one of {', '.join(known)})")
    for field in required:
        if field not in data:
            errors.append(f"{what}: missing required field {field!r}")
            return None
    return data


def _non_empty_str(value: object, what: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{what}: expected a non-empty string; got {value!r}")
        return None
    return value


def _enum(value: object, allowed: tuple[str, ...], what: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or value not in allowed:
        errors.append(f"{what}: {value!r} is not one of {', '.join(allowed)}")
        return None
    return value


def _mount_path(value: object, what: str, errors: list[str]) -> str | None:
    """A relative path under the plugin's own gateway mount.

    Relative, no scheme, no query or fragment, no `.`/`..` segment — the
    declaration names a place inside the plugin's mount and nothing else. A
    trailing slash is allowed (`"board/"`); the plugin's page backend decides
    what it means.
    """
    path = _non_empty_str(value, what, errors)
    if path is None:
        return None
    segments = path.split("/")
    if path.startswith("/"):
        errors.append(f"{what}: {path!r} must be relative to the plugin's mount, not absolute")
        return None
    for i, seg in enumerate(segments):
        if seg == "" and i == len(segments) - 1:
            continue  # a trailing slash
        if _PATH_SEGMENT_RE.match(seg) is None:
            errors.append(f"{what}: invalid path segment {seg!r} in {path!r}")
            return None
        if seg in (".", ".."):
            errors.append(f"{what}: {path!r} may not contain a {seg!r} segment")
            return None
    return path


def _color_literal(value: object, what: str, errors: list[str]) -> str | None:
    """A token value: an `oklch(...)` or hex literal, nothing else."""
    if not isinstance(value, str):
        errors.append(f"{what}: expected a color literal string; got {value!r}")
        return None
    literal = value.strip()
    if _OKLCH_RE.match(literal) is None and _HEX_RE.match(literal) is None:
        errors.append(
            f"{what}: {value!r} is not a color literal — a theme token takes "
            "oklch(L C H[/ A]) or #hex (a token pack is values, never CSS)"
        )
        return None
    return literal


def _validate_agent_inspect(value: object, errors: list[str]) -> list[dict[str, Any]]:
    """`agentInspect` — sections appended to the agent-inspect view."""
    if not isinstance(value, list):
        errors.append("contributions.ui.agentInspect: expected a list of sections")
        return []
    sections: list[dict[str, Any]] = []
    for i, entry in enumerate(cast(list[Any], value)):
        what = f"contributions.ui.agentInspect[{i}]"
        data = _entry_fields(entry, what, ("title", "source", "render"), errors)
        if data is None:
            continue
        title = _non_empty_str(data["title"], f"{what}.title", errors)
        source = _mount_path(data["source"], f"{what}.source", errors)
        render = _enum(data["render"], INSPECT_RENDERERS, f"{what}.render", errors)
        if title is None or source is None or render is None:
            continue
        sections.append({"title": title, "source": source, "render": render})
    return sections


def _validate_nav(value: object, errors: list[str]) -> list[dict[str, Any]]:
    """`nav` — entries opening the plugin's own page in a sandboxed iframe."""
    if not isinstance(value, list):
        errors.append("contributions.ui.nav: expected a list of nav entries")
        return []
    entries: list[dict[str, Any]] = []
    for i, entry in enumerate(cast(list[Any], value)):
        what = f"contributions.ui.nav[{i}]"
        data = _entry_fields(entry, what, ("location", "label", "icon", "page"), errors)
        if data is None:
            continue
        location = _enum(data["location"], NAV_LOCATIONS, f"{what}.location", errors)
        label = _non_empty_str(data["label"], f"{what}.label", errors)
        icon = _enum(data["icon"], NAV_ICONS, f"{what}.icon", errors)
        page = _mount_path(data["page"], f"{what}.page", errors)
        if location is None or label is None or icon is None or page is None:
            continue
        entries.append({"location": location, "label": label, "icon": icon, "page": page})
    return entries


def _validate_theme_tokens(value: object, what: str, errors: list[str]) -> dict[str, str] | None:
    if not isinstance(value, dict):
        errors.append(f"{what}: expected an object of token -> color literal")
        return None
    tokens = cast(dict[str, Any], value)
    if not tokens:
        errors.append(f"{what}: a theme must set at least one token")
        return None
    parsed: dict[str, str] = {}
    for token, raw in tokens.items():
        if token in NON_THEMABLE_TOKENS:
            errors.append(f"{what}: {token!r} is not themable — a theme pack sets colors only")
            continue
        if token not in THEME_TOKENS:
            errors.append(
                f"{what}: unknown token {token!r} — the vocabulary is the console's "
                "own custom properties (ui/web/src/app/globals.css)"
            )
            continue
        color = _color_literal(raw, f"{what}.{token}", errors)
        if color is not None:
            parsed[token] = color
    return parsed


def _validate_themes(value: object, errors: list[str]) -> list[dict[str, Any]]:
    """`themes` — named token packs the settings theme picker offers.

    `darkTokens` is optional and is the pack's dark-mode half: with it, `tokens`
    applies in light mode and `darkTokens` in dark, so the skin and the
    light/dark toggle stay orthogonal. **Omitting it is a deliberate
    declaration, not an oversight** — it means the pack PINS BOTH MODES to
    `tokens`, and the console says so in the picker, because a pack applied
    over both palettes silently disables the mode toggle for every color it
    sets. Both halves validate identically: same closed token vocabulary, same
    color-literal rule.
    """
    if not isinstance(value, list):
        errors.append("contributions.ui.themes: expected a list of token packs")
        return []
    themes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, entry in enumerate(cast(list[Any], value)):
        what = f"contributions.ui.themes[{i}]"
        data = _entry_fields(entry, what, ("name", "tokens"), errors, optional=("darkTokens",))
        if data is None:
            continue
        name = data["name"]
        if not isinstance(name, str) or _THEME_NAME_RE.match(name) is None:
            errors.append(f"{what}.name: must match ^[a-z0-9][a-z0-9_-]*$; got {name!r}")
            continue
        if name in seen:
            errors.append(f"{what}.name: duplicate theme {name!r}")
            continue
        seen.add(name)
        tokens = _validate_theme_tokens(data["tokens"], f"{what}.tokens", errors)
        if tokens is None:
            continue
        theme: dict[str, Any] = {"name": name, "tokens": tokens}
        if "darkTokens" in data:
            dark = _validate_theme_tokens(data["darkTokens"], f"{what}.darkTokens", errors)
            if dark is None:
                continue
            theme["darkTokens"] = dark
        themes.append(theme)
    return themes


def validate_ui_contributions(value: object, errors: list[str]) -> dict[str, object]:
    """Validate `contributions.ui`, appending every problem found to `errors`.

    Returns the normalized declaration — the same JSON shape with entries that
    validated, so a caller can consume it without re-reading the manifest. The
    manifest validator raises on a non-empty `errors`, so a partially parsed
    return is never handed to a consumer.
    """
    if not isinstance(value, dict):
        errors.append(f"contributions.ui must be an object with {'/'.join(UI_KEYS)}")
        return {}
    parsed: dict[str, object] = {}
    for key, raw in cast(dict[str, Any], value).items():
        if key not in UI_KEYS:
            errors.append(
                f"contributions.ui: unknown contribution type {key!r} (one of {', '.join(UI_KEYS)})"
            )
            continue
        if key == "agentInspect":
            parsed[key] = _validate_agent_inspect(raw, errors)
        elif key == "nav":
            parsed[key] = _validate_nav(raw, errors)
        else:
            parsed[key] = _validate_themes(raw, errors)
    return parsed
