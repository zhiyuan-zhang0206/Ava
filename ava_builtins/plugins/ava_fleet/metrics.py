"""ava_fleet Grafana + inspector metrics — registered at import time.

``scripts/gen_plugin_dashboard.py`` imports this module (inside a
PluginContext) to collect the registrations. Three metrics:

- ``ava_fleet_task_done_rate`` — dual-surface (grafana + inspector): a
  cluster-wide task-completion rate panel, and the same query the future
  inspector surface can render per agent.
- ``ava_fleet_spawn_rate`` — grafana-only spawn frequency.
- ``ava_fleet_agent_task_done_rate`` — inspector-only: demonstrates the
  ``{{agent_id}}`` placeholder semantics reserved in the registry snapshot
  (the gateway renders it to ``agent_id = <n>``); it is exported to
  the registry JSON but never becomes a Grafana panel.

Data provenance (verified against the live DB, 2026-08-04): ``task_update``
is category='audit' with a ``status`` attribute present only when the status
changed (so queries filter on ``attributes ? 'status'``); ``spawn`` is
category='audit' (~1.1k rows/30d).
"""

from shared.events.contract import TASK_UPDATE_KEYS
from shared.plugin_metrics import MetricSpec, register_metric

register_metric(
    MetricSpec(
        name="ava_fleet_task_done_rate",
        title="Task completion rate",
        description=(
            "Share of task_update events with status='done' — only updates carrying "
            "a status count toward the denominator (event_name='task_update', "
            "category='audit')."
        ),
        event_name="task_update",
        category="audit",
        unit="percent",
        panel="timeseries",
        query=(
            "SELECT $__timeGroup(ts, $__interval) AS time, "
            "100.0 * count(*) FILTER (WHERE " + TASK_UPDATE_KEYS["status"] + " = 'done') "
            '/ NULLIF(count(*), 0) AS "done %" '
            "FROM events "
            "WHERE event_name = {event_name} AND category = {category} "
            "AND attributes ? 'status' AND $__timeFilter(ts) "
            "GROUP BY 1 ORDER BY 1"
        ),
        output=["grafana", "inspector"],
    )
)

register_metric(
    MetricSpec(
        name="ava_fleet_spawn_rate",
        title="Agent spawn rate",
        description=(
            "Spawn events over time — the rate at which new agents are born "
            "(event_name='spawn', category='audit')."
        ),
        event_name="spawn",
        category="audit",
        unit="ops",
        panel="barchart",
        query=(
            'SELECT $__timeGroup(ts, $__interval) AS time, count(*) AS "spawns" '
            "FROM events "
            "WHERE event_name = {event_name} AND category = {category} AND $__timeFilter(ts) "
            "GROUP BY 1 ORDER BY 1"
        ),
        output=["grafana"],
    )
)

register_metric(
    MetricSpec(
        name="ava_fleet_agent_task_done_rate",
        title="Agent task completion rate",
        description=(
            "Inspector-only: the same query parameterized by agent. The {{agent_id}} "
            "placeholder is rendered by the gateway as agent_id = <n>; metrics "
            "carrying the placeholder must not also be emitted to Grafana panels."
        ),
        event_name="task_update",
        category="audit",
        unit="percent",
        panel="timeseries",
        query=(
            "SELECT $__timeGroup(ts, $__interval) AS time, "
            "100.0 * count(*) FILTER (WHERE " + TASK_UPDATE_KEYS["status"] + " = 'done') "
            '/ NULLIF(count(*), 0) AS "done %" '
            "FROM events "
            "WHERE event_name = {event_name} AND category = {category} "
            "AND attributes ? 'status' AND {{agent_id}} AND $__timeFilter(ts) "
            "GROUP BY 1 ORDER BY 1"
        ),
        output=["inspector"],
    )
)
