from __future__ import annotations

from meizel.state.ambient_state.rootrule import FromSettingsRule

RULE = FromSettingsRule(
    rule="ambient-endpoint",
    class_name="ServiceEndpoints",
    modules={"base.daemon.endpoints"},
    registry="ENDPOINT_PACKAGES",
)
AMBIENT_ENDPOINT = RULE.rule
FIX = (
    "take the daemon's `ServiceEndpoint` (or the `ServiceEndpoints` table) from the composition "
    "root named in ENDPOINT_PACKAGES; only the root calls `ServiceEndpoints.from_settings()`"
)
hits = RULE.hits
