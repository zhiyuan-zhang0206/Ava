# Plugin-registered ops services

## Context

The ops service roster is single-sourced in `ops/spec.py:build_services()` (the
just-landed ops-module slice 1): every service — its launch command, probe
metadata, capability group, and watchdog healthcheck — is one `ServiceSpec`, and
`ava start` / the watchdog keepalive roster / `ava status` all derive from that
one tuple.

One of its entries did not belong there. `task-maintenance` — the gateway-side
daemon that nudges overdue task owners and escalates to their delegator — is
entirely fleet domain: it reads/writes `agent_tasks`, delivers through the fleet
notice path, and only makes sense when a human is supervising the fleet. That
domain already has a home, the `ava_fleet` plugin (`plugins/ava_fleet/`, which
owns `ava.tasks`, `ava.self`, `ava.ui`, the fleet system-prompt section). Yet its
`ServiceSpec` sat hardcoded in the core `ops/spec.py` roster and its daemon +
healthcheck lived under `services/`, and `_gate_reason` carried a fleet-specific
`AVA_TASK_MAINTENANCE_ENABLED` branch. A low layer (ops) was naming a high-layer
concept. The question raised: should plugins be able to register a service, with
a spec/contract that keeps the single-source roster intact?

## Decision

**Yes — a registration hook, not a plugin framework.** A plugin that runs its own
long-lived daemon ships a `plugins/<name>/services.py` exposing
`services() -> tuple[ServiceSpec, ...]`. `ops/spec.py:_plugin_services()`
discovers the plugins *present on this machine* (via
`shared.plugins_config.installed_plugin_dirs()`), loads each one's `services.py` by
file path, and folds the returned specs onto the tail of `build_services()`.
Session-name collisions (plugin↔core or plugin↔plugin) fail fast — the roster is
keyed on `session`, so a duplicate would silently shadow one entry.

Direction is **"plugin declares, ops discovers"**: ops never imports a specific
plugin's domain code. The `services.py` manifest is deliberately light — it
imports only `ops.spec.ServiceSpec` + `shared` — so the ops/CLI/watchdog process
that discovers it never drags the agent kernel in. The daemon and healthcheck
move under the plugin namespace (`plugins/ava_fleet/task_maintenance/{daemon,
healthcheck}.py`); they launch via `python -m` / `importlib`, which only touches
empty package `__init__.py`s, never the plugin's `plugin.py` (whose registration
side effects are agent-only). A plugin service may carry its own
`ServiceSpec.gate` so a fleet/plugin-domain toggle travels with the plugin instead
of adding a branch to `ops._gate_reason`.

Single-source is preserved: the watchdog / start / status still derive from
`build_services()`, only now its tail includes plugin-declared entries.

### Discovery keys on presence, not the agent enable-state

A tempting shortcut was to key discovery on the plugin *enable-state*
(`plugins_config.json`, what `ava plugins enable/disable` writes), so disabling
the `ava_fleet` plugin would drop task-maintenance from the roster — matching the
plugin's "disable the plugin, strip the whole surface" philosophy. Rejected.

The enable-state plane is the *agent-plugin-registration* plane: `agent/graph/
_build.py` reads it to decide which plugins an agent imports (hooks / namespaces /
prompt). The service roster is a *machine/cluster* plane. Coupling them means a
machine-level daemon's existence is decided by an agent-facing config surface —
cross-plane semantics that get murky (what does "the fleet plugin is disabled"
mean on a gateway-only unit that runs no agents?). Investigation confirmed the
enable-state is *technically* clean for this (it is a per-unit machine file with
no per-agent overlay — only plugin config *values*, not enable/disable, get
per-agent overlays via `plugin_config_registry`), so the shortcut would have
*worked*; it was rejected on semantics, not correctness.

Instead: **discovery keys on plugin code presence** (deterministic filesystem),
and a service's cluster-level on/off is an **explicit settings field read by
`ServiceSpec.gate`** — for task-maintenance, `AVA_TASK_MAINTENANCE_ENABLED`
(scope=host, remote-writable), evaluated deterministically at daemon-start and
unaffected by any per-agent overlay. Two clean planes: "is the plugin's code
here" (presence) and "should its daemon run" (explicit gate). `ava plugins
disable ava_fleet` strips the *agent* surface (namespaces, prompt) but does not by
itself stop the gateway daemon — that is `AVA_TASK_MAINTENANCE_ENABLED`.

### Why this mechanism (rejected alternatives)

- **ops imports each plugin's specs directly** (`from plugins.ava_fleet.services
  import services`). Rejected: a hardcoded reverse edge from the low ops layer into
  named plugin modules — the exact coupling being removed — and it would not reach
  external `~/.ava/plugins/` plugins off the `plugins.` package path.

- **Runtime registry via import side effect** (plugin `plugin.py` calls
  `ops.register_service(...)` when imported, like `register_namespace_member`).
  Rejected: `plugin.py` is imported only in the *agent* process; the ops/CLI/
  watchdog processes that call `build_services()` never import it, so the registry
  would be empty there. Forcing them to import every enabled `plugin.py` would pull
  the whole agent kernel into the CLI/watchdog — heavy and layer-violating.

- **File-path discovery of a light manifest** (chosen). Mirrors the existing
  `shared.plugins_config.update_all_disk_images()`, which already imports each
  plugin's `default_config.py` by file path precisely to avoid `plugin.py`'s side
  effects. Reuses `ServiceSpec` and the plugin-discovery machinery already in
  `shared.plugins_config` — no new framework, one small `_plugin_services()` helper
  plus one `ServiceSpec.gate` field.

### Config ownership: daemon config stays global

The daemon's operational config — `AVA_TASK_MAINTENANCE_ENABLED` / `_INTERVAL_S`
/ `AVA_TASK_NUDGE_BACKOFF_SECONDS` / `AVA_TASK_ESCALATE_N`, the pidfile, and the
`daemon_health` port 8108 — stays in the global env-driven `shared.config` /
`shared.daemon_health`, read by the daemon exactly as every other daemon
(heartbeat, labeler, memory-indexer…) reads its own. It was **not** moved into the
plugin's `default_config.py`: that config is *agent-scoped* (a per-agent schema
merged into agent config), and a gateway-side global daemon cannot read it.
Relocating these fields would mean inventing a "plugin declares global daemon
config" mechanism that does not exist — a new framework, contra the minimalism
goal. The port registry stays central for the same reason it exists: ports must be
unique across the whole machine, so one registry that sees all of them is correct.

Trade-off accepted: fleet-domain field *names* remain in the core config models.
That is a far weaker coupling than executable roster + domain code living in ops,
and it is uniform with every other daemon. If plugin-owned daemons proliferate, a
"plugin daemon config" surface can pull them out later.

## Consequences

- `build_services()` now depends on which plugins are present on disk, where it
  was a pure function. Builtin plugins (ava_fleet) are always checked in, so in the
  repo's canonical state it is deterministic; only an external `~/.ava/plugins/`
  plugin adds machine variance. The roster lint (`scripts/lint_doc_roster.py`) and
  roster tests run in that canonical state.

- The mechanism generalizes to any future plugin daemon (builtin or external under
  `~/.ava/plugins/`) — the plugin authors its own `ServiceSpec.cmd` (a venv-direct
  `.venv/bin/python -m …` launch, relative to the source checkout the service
  starts in; no `uv run` wrapper), so how its daemon is launched is the plugin's
  concern, not ops's.

- Core services (browser / telegram / heartbeat) keep flowing through the
  centralized `ops._gate_reason`; only plugin services use `ServiceSpec.gate`.
  Migrating the core gates onto the field too would make gating fully uniform but
  touches core with no functional gain — left as an optional follow-up.
