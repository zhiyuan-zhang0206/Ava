"""ava_memory — ops service declarations (the plugin's `build_services()` hook).

The memory indexer is the pool's search side: it watches the gateway's
consolidated checkout and keeps the Milvus index current, which is what makes
`ava.memory.search` — and therefore passive recall — return anything. It is
declared here rather than hardcoded into `ops/roster.py` because the pool is this
plugin's, end to end: disable ava_memory and there is no pool to index, no
`ava.memory` to search it with, and now no daemon indexing it either.

Discovery keys on this plugin's code being PRESENT on the machine (see
`ops.spec._plugin_services`), so the cluster-level on/off is the explicit gate
below rather than the presence check.

Deliberately light, like `ava_fleet/services.py`: it imports the ops service
contract and roster probe helper plus `shared` — never `plugin.py` or the memory
domain code — so the ops/CLI/watchdog process that discovers it does not pull in
the agent kernel. `services()` is a function so probe ports derived from settings
are read at use-time.
"""

from __future__ import annotations

import os

from ops.roster import daemon_identity
from ops.service_spec import ServiceSpec
from shared.config import settings
from shared.daemon_health import health_port
from shared.machine import MachineRole

# The indexer runs on the gateway capability: it indexes the gateway's
# consolidated checkout, which only a gateway-capable unit has. Declared here
# rather than reaching into ops's private `_GATEWAY` so the plugin owns its own
# capability decision.
_GATEWAY: frozenset[MachineRole] = frozenset({"gateway"})


def _memory_indexer_gate() -> str | None:
    """Gate reason for memory-indexer, or None when it will start.

    Indexing exists to serve recall and `ava.memory.search`. With the shared pool
    not injected anywhere, the index has no consumer in this cluster, so the
    daemon (and its embedding spend) is dead work — the same env switch that
    silences the shared index switches it off.
    """
    # The toggle lives in the 'agent' config domain (AgentMemorySettings), which
    # a gateway-profile process — the gateway watchdog evaluating this gate —
    # does not construct (per-process config, Task #856): `settings.agent`
    # raises there. Read the cluster-pinned env alias directly so the gate
    # answers the same in every process kind (start enables the daemon, the
    # watchdog revives it). ava_builtins is outside the lint-no-os-environ scan
    # (plugin boundary), and this is the one read that cannot go through the
    # per-profile Settings singleton.
    inject = os.environ.get("AVA_MEMORY_INDEX_INJECT", "true")
    if inject.strip().lower() in ("0", "false", "off", "no"):
        return "disabled (AVA_MEMORY_INDEX_INJECT off — nothing consumes the index)"
    return None


def services() -> tuple[ServiceSpec, ...]:
    """The ops services the memory plugin contributes to the roster.

    The gateway-side indexing daemon. Ordering against milvus (which it
    cold-start-connects to) is preserved by `_plugin_services()` folding plugin
    services onto the tail of the roster, well after the gateway group.
    """
    return (
        ServiceSpec(
            session="memory-indexer",
            cmd=".venv/bin/python -m services.memory_indexer.daemon",
            capabilities=_GATEWAY,
            # The pool is a markdown checkout on disk and the index is Milvus — the
            # daemon never opens the main DB (which is also why it is the one daemon
            # that skips `assert_schema_current`). So a pg outage or a schema
            # mismatch is not its concern and the watchdog keeps reviving it.
            requires_db=False,
            curl_url=f"http://localhost:{health_port('memory_indexer')}/healthz",
            # Same identity contract as every core /healthz daemon: a 2xx is only
            # believed once name/home/pid say the answering process is this unit's.
            identity_probe=daemon_identity(
                "memory_indexer", settings.services.memory_indexer_pidfile
            ),
            healthcheck_module="services.healthchecks.memory_indexer",
            gate=_memory_indexer_gate,
        ),
    )
