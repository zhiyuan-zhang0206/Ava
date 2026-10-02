"""The ambient-bus rule: only a package's named roots build the event bus.

Redis and the live-events channel come from `base.events.live.bus.EventBus`, which reads the
settings once, in `EventBus.from_settings()`. The gateway builds its bus in the lifespan
(`app.state.bus`) and its routers read it from there, so those packages name no root. See
`rootrule` for the shared shape.
"""

from __future__ import annotations

from scripts.structure.ambient_state.rootrule import FromSettingsRule

RULE = FromSettingsRule(
    rule="ambient-bus",
    class_name="EventBus",
    modules={"base.events.live.bus", "base.events.live"},
    registry="BUS_PACKAGES",
)
AMBIENT_BUS = RULE.rule
FIX = (
    "take the `EventBus` from the composition root named in BUS_PACKAGES (a daemon root, or "
    "`request.app.state.bus` in the gateway); only the root calls `EventBus.from_settings()`"
)
hits = RULE.hits
builds = RULE.builds
package_of = RULE.package_of
