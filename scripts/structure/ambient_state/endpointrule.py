"""The ambient-endpoint rule: only a package's named roots build the endpoint table.

A daemon's port and pidfile come from `base.daemon.endpoints.ServiceEndpoints`, which reads the
settings and `AVA_HOME` once, in `ServiceEndpoints.from_settings()`. The named modules are every
daemon root plus the entry points of the commands and probes that read a daemon's row. See
`rootrule` for the shared shape.
"""

from __future__ import annotations

from scripts.structure.ambient_state.rootrule import FromSettingsRule

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
