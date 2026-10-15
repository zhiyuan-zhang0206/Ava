from __future__ import annotations

from meizel.state.ambient_state.rootrule import FromSettingsRule

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
