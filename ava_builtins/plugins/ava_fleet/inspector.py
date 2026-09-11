"""ava_fleet inspector widgets — the fleet's embed in the Inspector Panel.

Registers the fleet plugin's per-agent jump buttons (task #2909, user request
2026-09-11): from an agent's inspector panel, one click opens the agent's
open notice in the fleet inbox and one opens the task it is working on
(the notice's task, else the agent's first real task — the queue's own
ownership rule, resolved kernel-side).

The targets are closed vocabulary (see ``shared/plugin_inspector.py``); the
kernel resolves their per-agent data, so this module carries no ids and no
callables. ``order=50`` puts the buttons first in the panel: they are
navigation, so they should be visible without scrolling (the order scale and
the built-in sections' keys are documented in ``conventions/plugin-spec-v2.md``).
"""

from shared.plugin_inspector import InspectButtonSpec, InspectWidgetSpec, register_inspect_widget

register_inspect_widget(
    InspectWidgetSpec(
        id="jump-buttons",
        kind="jumpButtons",
        order=50,
        buttons=[
            InspectButtonSpec(target="notice"),
            InspectButtonSpec(target="task"),
        ],
    )
)
