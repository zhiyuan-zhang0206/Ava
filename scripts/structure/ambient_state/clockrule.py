"""The ambient-clock rule: only a package's named roots build the cluster clock.

The cluster timezone and the agent-facing timestamp format come from `base.clock.Clock`, which
reads the settings once, in `Clock.from_settings()`. See `rootrule` for the shared shape.
"""

from __future__ import annotations

from scripts.structure.ambient_state.rootrule import FromSettingsRule

RULE = FromSettingsRule(
    rule="ambient-clock",
    class_name="Clock",
    modules={"base.clock"},
    registry="CLOCK_PACKAGES",
)
AMBIENT_CLOCK = RULE.rule
FIX = (
    "take the `Clock` from the composition root named in CLOCK_PACKAGES; only the root calls "
    "`Clock.from_settings()`"
)
hits = RULE.hits
