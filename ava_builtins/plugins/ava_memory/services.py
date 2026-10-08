"""ava_memory — ops service declarations (the plugin's `build_services()` hook).

The memory indexer is the pool's search side: it watches the gateway's
consolidated checkout and keeps the memory search index current, which is what makes
`ava.memory.search` — and therefore passive recall — return anything. It is
declared here rather than hardcoded into `ops/roster/__init__.py` because the pool is this
plugin's. Its host-scoped `indexer_enabled` config controls the daemon separately
from whether agents inject the standing memory index into their prompts.

Discovery keys on this plugin's code being PRESENT on the machine (see
`ops.spec.plugin_services`), so the cluster-level on/off is the explicit gate
below rather than the presence check.

Deliberately light, like `ava_fleet/services.py`: it imports the ops service
contract and roster probe helper plus `base` — never `plugin.py` or the memory
domain code — so the ops/CLI/watchdog process that discovers it does not pull in
the agent kernel. `services()` is a function so probe ports derived from settings
are read at use-time.
"""

from __future__ import annotations

from functools import partial

from ava_builtins.plugins.ava_memory.default_config import MemoryConfig
from base.cluster.machine import MachineRole
from base.packages.plugins.config_registration import disk_image_path, read_config_image
from ops.roster import healthz_daemon
from ops.roster.service_spec import ServiceSpec

# The indexer runs on the gateway capability: it indexes the gateway's
# consolidated checkout, which only a gateway-capable unit has. Declared here
# rather than reaching into ops's private `_GATEWAY` so the plugin owns its own
# capability decision.
_GATEWAY: frozenset[MachineRole] = frozenset({"gateway"})


def _memory_indexer_gate(config: MemoryConfig) -> str | None:
    """Gate the indexer from the validated host config, independently of agent injection."""
    if not config.indexer_enabled:
        return "disabled (ava_memory.indexer_enabled off)"
    return None


def services() -> tuple[ServiceSpec, ...]:
    """The ops services the memory plugin contributes to the roster.

    The gateway-side indexing daemon. Ordering against memory-search (which it
    cold-start-connects to) is preserved by `ops.spec.plugin_services()` folding plugin
    services onto the tail of the roster, well after the gateway group.
    """
    config_path = disk_image_path("ava_memory")
    config = read_config_image(MemoryConfig, config_path)
    return (
        healthz_daemon(
            "memory-indexer",
            module="services.derived.memory_indexer.daemon",
            capabilities=_GATEWAY,
            # The pool is a markdown checkout on disk and the default index backends
            # (numpy) never open the main DB (which is also why it is the one
            # daemon that skips `assert_schema_current`). So a pg outage or a schema
            # mismatch is not its concern and the watchdog keeps reviving it. The
            # selectable pgvector backend does dial it, so the launcher still
            # delivers the gateway login.
            requires_db=False,
            db_access="gateway",
            gate=partial(_memory_indexer_gate, config),
            config_inputs=(config_path,),
        ),
    )
