"""An agent's framework config pins: its two stored maps merged into the flat pin map.

Many agents' turns share one host process, so a `per_agent=True` field cannot live in the
process-global `settings` singleton alone. The host resolves each agent's pins

    config_overlay (agents_meta)  >  birth_config (frozen fields)  >  cluster default (live)

and builds the agent's `AgentSlices` from them (`base/host/env/agent_slices.py`): a pin wins for
the agent, an unpinned field falls through to the live singleton, so a cluster-default edit keeps
reaching agents as the `lifecycle: live` contract promises. Pin values stay raw: validation
happened before they reached agents_meta, and reading the stored value must not silently coerce
it a second time.

Scope: framework `Settings` fields only. Plugin-scope config has the same problem and its own
resolution (`base/packages/plugins/config_view.py`).

Enforcement: `scripts/lint/turn_scoped_config.py` forbids reading a `per_agent` field through the
bare singleton from the turn-scoped packages — those reads come from the agent's slices.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import base.host.env.config_registry as _config_registry


def resolve_agent_config_pins(
    config_overlay: Mapping[str, Any] | None,
    birth_config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge an agent's two stored config maps into the flat pin map.

    birth_config first, config_overlay on top. Only framework `Settings` fields are kept:
    plugin-scope overlay keys (dotted `plugin.field` or plugin-owned flat keys)
    are not pinnable through this view yet and are dropped here — the hosted
    host applies them via the plugin path separately.

    Unknown keys are dropped rather than raised: the maps were validated at
    write time (`validate_config_overlay`), so an unknown key here means the
    field was deleted from Settings after the overlay was stored. Reading old
    stored configuration does not restore a deleted field.
    """
    fields = _config_registry.fields()
    pins: dict[str, Any] = {}
    for source in (birth_config, config_overlay):
        if not source:
            continue
        for key, value in source.items():
            if key in fields:
                pins[key] = value
    return pins
