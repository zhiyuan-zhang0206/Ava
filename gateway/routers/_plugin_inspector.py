"""Inspector plugin-widget loading + per-agent resolution — helper for agent_inspect (task #2909).

Not a router: ``gateway/routers/agent_inspect.py`` mounts the single endpoint
``GET /api/agents/{id}/inspect/widgets`` and delegates the blocking work here
(kept as its own module so agent_inspect stays under the per-file line budget).
The extension surface of the inspector panel: a plugin embeds widgets for
every agent from its own Python half (``register_inspect_widget`` at
``inspector.py`` import — see ``shared/plugin_inspector.py``).

The registry is built **in process** like the plugin-metric one (task #180
PR D): the shipped builtin plugins' ``inspector.py`` modules are imported
under their plugin context, module caching makes repeated loads free, and no
snapshot file exists to go stale. The enable-state is consulted per request
(``plugins_config``), so ``ava plugins disable`` takes effect without a
gateway restart — a plugin disabled after its module was imported has its
widgets filtered out here even though its registration objects remain in the
process registry.

Resolution happens server-side because the payloads are *data*, not routes:
a ``taskList`` widget lists the agent's active tasks (owned by the agent, not
done/cancelled, newest first, capped). A widget with an empty payload drops
out of the response entirely (the panel's empty-section rule). Import errors are fail-soft (the plugin-load contract —
2026-08-28 ava_ledger incident, restated for plugins 2026-09-11): a plugin
whose ``inspector.py`` fails to import, or whose registration raises, is
reported loudly and skipped, and the endpoint keeps serving the remaining
plugins' widgets; an unknown agent is a 404 like the rest of the /inspect
family.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from psycopg import Cursor
from psycopg_pool import ConnectionPool

from gateway.schemas import InspectWidgetResult, InspectWidgetTask
from shared import plugin_load_report, plugins_config
from shared.plugin_context import PluginContext
from shared.plugin_inspector import (
    InspectWidgetSpec,
    drop_plugin_inspect_widgets,
    registered_inspect_widgets,
)

# The shipped-plugin inspector directory — every builtin plugin dir with an
# inspector.py is part of the in-process registry (the metric loader's
# convention; external plugin code is not imported by the gateway).
_PLUGINS_DIR = Path(__file__).resolve().parents[2] / "ava_builtins" / "plugins"


def _enabled_inspector_modules() -> list[Path]:
    """``inspector.py`` of every ENABLED builtin plugin, sorted — the import
    set of one registry build, and the enabled-set the caller filters the
    process registry against."""
    if not _PLUGINS_DIR.is_dir():
        return []
    installed = plugins_config.installed_plugin_dirs()
    config = plugins_config.load_for_runtime(set(installed))
    modules: list[Path] = []
    for plugin_dir in sorted(_PLUGINS_DIR.iterdir()):
        module = plugin_dir / "inspector.py"
        if not module.is_file():
            continue
        entry = config.plugins.get(plugin_dir.name)
        if entry is None or not entry.enabled:
            continue
        modules.append(module)
    return modules


def _load_inspect_widgets() -> list[InspectWidgetSpec]:
    """The in-process widget registry, restricted to the plugins enabled right
    now. Importing a module is cached; a plugin disabled since its first
    import is filtered out by the enabled-set check rather than unregistered.

    Fail-soft per plugin (user ruling 2026-09-11): a plugin whose
    ``inspector.py`` fails to import, or whose registration raises, is
    reported loudly and skipped — the remaining widgets still serve. The
    half-executed module is dropped from ``sys.modules`` and its partial
    registrations are dropped too (``drop_plugin_inspect_widgets`` — a module
    can raise mid-registration), so a fixed file is picked up cleanly on a
    later request."""
    modules = _enabled_inspector_modules()
    enabled: set[str] = set()
    for module in modules:
        plugin = module.parent.name
        module_name = f"ava_builtins.plugins.{plugin}.inspector"
        try:
            with PluginContext(plugin):
                importlib.import_module(module_name)
        except (KeyboardInterrupt, SystemExit):
            sys.modules.pop(module_name, None)
            raise
        except BaseException as exc:
            sys.modules.pop(module_name, None)
            plugin_load_report.report_plugin_load_failure(plugin, exc)
            drop_plugin_inspect_widgets(plugin)
            continue
        enabled.add(plugin)
    return [spec for spec in registered_inspect_widgets() if spec.plugin in enabled]


# The taskList cap: a compact "what is this agent working on" overview — the
# full board lives in the fleet task view.
TASK_LIST_LIMIT = 8


def _resolve_tasks(cur: Cursor[Any], agent_id: int) -> list[InspectWidgetTask]:
    """The agent's active tasks, newest first — the ``taskList`` payload.

    "Active" = ``in_progress`` / ``ongoing``; the system root is excluded by
    its NULL owner, and the kernel-side cap (``TASK_LIST_LIMIT``) keeps the
    panel section an overview, not a board."""
    cur.execute(
        "SELECT id, title FROM agent_tasks "
        "WHERE owner = %s AND status IN ('in_progress', 'ongoing') "
        "ORDER BY updated_at DESC, id DESC LIMIT %s",
        (agent_id, TASK_LIST_LIMIT),
    )
    return [InspectWidgetTask(id=int(row[0]), title=str(row[1])) for row in cur.fetchall()]


def widgets_for_agent(pool: ConnectionPool[Any], agent_id: int) -> list[InspectWidgetResult]:
    """Sync twin of the widgets endpoint — runs via asyncio.to_thread."""
    specs = _load_inspect_widgets()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM agents_meta WHERE id = %s", (agent_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
        tasks = _resolve_tasks(cur, agent_id)

    results: list[InspectWidgetResult] = []
    for spec in specs:
        if spec.kind == "taskList" and tasks:
            results.append(
                InspectWidgetResult(
                    plugin=spec.plugin,
                    id=spec.id,
                    kind=spec.kind,
                    order=spec.order,
                    title=spec.title,
                    tasks=tasks,
                )
            )
    return results
