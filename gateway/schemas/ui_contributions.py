"""Console contribution aggregate — `GET /api/ui/contributions`.

The wire form of what the cluster's enabled plugins declare under
`contributions.ui` (`shared/plugin_ui_contributions.py`). Every entry is
name-attributed: the console labels provenance, and an operator tracing a
surface back to the plugin that put it there reads one field.

Themes only today — the slice that carries nav entries and agent-inspect
sections adds its own array beside this one (additive, so an alternative
frontend built against today's spec keeps working).
"""

from __future__ import annotations

from pydantic import BaseModel


class UiThemeContribution(BaseModel):
    """One named token pack a plugin offers the theme picker.

    `tokens` is a partial map of the console's own `:root` custom properties to
    color literals — validated at manifest load, so the console applies the
    values as given. Unset tokens keep the console default, which is why a pack
    that names three colors is a legitimate skin rather than a broken one.
    """

    plugin: str
    name: str
    tokens: dict[str, str]


class UiContributionsResponse(BaseModel):
    """Every console contribution the cluster's enabled plugins declare."""

    themes: list[UiThemeContribution]
