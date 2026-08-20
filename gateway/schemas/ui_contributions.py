"""Console contribution aggregate — `GET /api/ui/contributions`.

The wire form of what the cluster's enabled plugins declare under
`contributions.ui` (`shared/plugin_ui_contributions.py`). Every entry is
name-attributed: the console labels provenance, and an operator tracing a
surface back to the plugin that put it there reads one field.

Themes and nav entries today — the slice that carries agent-inspect sections
adds its own array beside these (additive, so an alternative frontend built
against today's spec keeps working).
"""

from __future__ import annotations

from pydantic import BaseModel


class UiThemeContribution(BaseModel):
    """One named token pack a plugin offers the theme picker.

    `tokens` is a partial map of the console's own `:root` custom properties to
    color literals — validated at manifest load, so the console applies the
    values as given. Unset tokens keep the console default, which is why a pack
    that names three colors is a legitimate skin rather than a broken one.

    `dark_tokens` is the pack's dark-mode half, and `null` is a MEANINGFUL
    value rather than a missing one: it declares that the pack deliberately
    pins both modes to `tokens`. That distinction has to survive onto the wire
    because it is what the picker tells the user — a pack applied over both
    palettes silently disables the light/dark toggle for every color it sets,
    and a skin and a mode that fight is the failure this field exists to make
    visible. With it set, `tokens` is the light half and the two stay
    orthogonal.
    """

    plugin: str
    name: str
    tokens: dict[str, str]
    dark_tokens: dict[str, str] | None = None


class UiNavContribution(BaseModel):
    """One nav entry opening a plugin-served page.

    `page` is a path under the plugin's own mount (`/api/plugin-ui/<plugin>/`),
    `icon` a lucide icon name from the closed set the console imports, and
    `location` names which of the console's nav surfaces carries the entry —
    all three validated at manifest load.
    """

    plugin: str
    location: str
    label: str
    icon: str
    page: str


class UiContributionsResponse(BaseModel):
    """Every console contribution the cluster's enabled plugins declare."""

    themes: list[UiThemeContribution]
    nav: list[UiNavContribution]
