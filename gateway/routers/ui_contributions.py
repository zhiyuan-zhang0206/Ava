"""Console contributions — GET /api/ui/contributions.

The aggregation half of `future/frontend-plugin-contributions.md`: the console
asks one endpoint what the enabled plugins declare under `contributions.ui`,
and gets back the merged, plugin-attributed declaration set. There is no
per-plugin frontend code and no build step — a plugin's contribution is data
that the console's own components render.

**Cluster-scoped, read from manifests.** UI contributions describe the console,
which is per-cluster, so this reads the enabled set rather than any per-agent
overlay (per-agent activation, issue #39-S3, filters agent-inspect sections
only — nav and themes are cluster-level surfaces). The enabled set is this
gateway host's `plugins_config.json` until the cluster registry rows of #39-S4
land. Nothing here imports plugin code: a manifest is a file, and reading it is
all this endpoint does.

A manifest that no longer validates is a 500 naming the plugin, not a plugin
silently dropped from the list — the install gate already validated it, so a
manifest that fails here means the on-disk copy changed under the cluster.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, HTTPException

from gateway.schemas import (
    UiContributionsResponse,
    UiNavContribution,
    UiThemeContribution,
)
from shared import plugins_config
from shared.plugin_manifest import ManifestError, load_manifest

router = APIRouter()


def _enabled_ui_declarations() -> list[tuple[str, dict[str, Any]]]:
    """`(plugin, contributions.ui)` for every enabled plugin that declares one."""
    installed = plugins_config.installed_plugin_dirs()
    config = plugins_config.load_for_runtime(set(installed))
    declarations: list[tuple[str, dict[str, Any]]] = []
    for name in sorted(installed):
        entry = config.plugins.get(name)
        if entry is None or not entry.enabled:
            continue
        try:
            manifest = load_manifest(installed[name])
        except ManifestError as e:
            raise HTTPException(
                status_code=500,
                detail=f"plugin {name!r} has an invalid ava-plugin.json: {e}",
            ) from e
        if manifest is None:
            continue
        ui = manifest.contributions.get("ui")
        if ui is None:
            continue
        declarations.append((name, cast(dict[str, Any], ui)))
    return declarations


@router.get("/api/ui/contributions")
def get_ui_contributions() -> UiContributionsResponse:
    """Every console contribution the cluster's enabled plugins declare."""
    themes: list[UiThemeContribution] = []
    nav: list[UiNavContribution] = []
    for plugin, ui in _enabled_ui_declarations():
        for theme in cast(list[dict[str, Any]], ui.get("themes", [])):
            # Absent darkTokens is carried as None, not as an empty map: the
            # picker distinguishes "pins both modes" from "has a dark half",
            # and an empty map would read as the latter.
            dark = cast(dict[str, str], theme["darkTokens"]) if "darkTokens" in theme else None
            themes.append(
                UiThemeContribution(
                    plugin=plugin,
                    name=cast(str, theme["name"]),
                    tokens=cast(dict[str, str], theme["tokens"]),
                    dark_tokens=dark,
                )
            )
        for entry in cast(list[dict[str, Any]], ui.get("nav", [])):
            nav.append(
                UiNavContribution(
                    plugin=plugin,
                    location=cast(str, entry["location"]),
                    label=cast(str, entry["label"]),
                    icon=cast(str, entry["icon"]),
                    page=cast(str, entry["page"]),
                )
            )
    return UiContributionsResponse(themes=themes, nav=nav)
