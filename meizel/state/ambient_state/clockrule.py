from __future__ import annotations

from meizel.state.ambient_state.rootrule import FromSettingsRule

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
