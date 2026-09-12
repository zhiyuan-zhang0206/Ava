"""ava_fleet inspector widgets — the fleet's embed in the Inspector Panel.

Registers the fleet plugin's per-agent task list (task #2909; reshaped for
task #3216, user request 2026-09-12): the inspector shows the agent's active
tasks — "today's tasks" — as a section, each row jumping to the task in the
fleet task view.

The kernel resolves the rows (its ownership rule and cap, see
``shared/plugin_inspector.py``), so this module carries no ids and no
callables. ``order=50`` puts the section first in the panel: it is the agent's
work queue, so it should be visible without scrolling (the order scale and the
built-in sections' keys are documented in ``conventions/plugin-spec-v2.md``).
"""

from shared.plugin_inspector import InspectWidgetSpec, register_inspect_widget

register_inspect_widget(
    InspectWidgetSpec(
        id="today-tasks",
        kind="taskList",
        order=50,
    )
)
