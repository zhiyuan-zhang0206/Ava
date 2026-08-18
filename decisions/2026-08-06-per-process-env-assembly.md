# Per-process env assembly — design doc

Date: 2026-08-06

## Background

Today every process (gateway / agent / runner daemon / CLI maintenance verbs) imports `shared.config`; `load_ava_env()` dumps all 231 fields of `$AVA_HOME/.env` into os.environ, then `_enforce_cluster_env_authority()` cleans up afterwards with an exemption list + a drop list.

Problems:
1. **gateway process carries agent-identity env** → plugin + agent core fully loaded, +11MB (F-config-1)
2. **env authority is maintained by patching "drop/exemption lists"** (F-config-2) — every new key needs a manual decision about which list it belongs to
3. **any process importing shared.config builds all 231 fields** (F-config-3) — no conceptual boundary exists
4. **sentinel/placeholder patches** (F-config-4) — `UNANCHORED_DB_SENTINEL`, `_LITE_REDIS_URL` scattered around

## Design goal

Each process only owns the environment variables it needs. Build positively per process profile (allow-list); abolish the exemption list.

## Process profiles (4)

| Profile | Trigger | env keys needed |
|---------|---------|-----------------|
| **gateway** | `config_source_is_local()` = True | gateway+common capability-domain cluster keys + host keys |
| **agent** | agent process (child process, native) | bootstrap/identity keys only (fetched after start) |
| **runner daemon** | serve_agent_runner and not agent | bootstrap + host-scope + agent-runner host keys |
| **CLI lite** | `AVA_CONFIG_FETCH=skip` | host-scope keys + placeholders that never dial |

## Gateway profile — env key set (Phase 1 target)

Fields the gateway process needs, grouped by (scope, capability):

**Needed (into os.environ):**
- (cluster-pinned, gateway): 42 fields — gateway's own runtime config
- (host, gateway): 38 fields — daemon service config
- (cluster-pinned, common): 17 fields — observability, general settings
- (host, common): 11 fields — machine identity, paths

**Not needed (excluded from os.environ):**
- (cluster-pinned, agent-runner): 86 fields — LLM API keys, model config, sandbox/web/feishu/telegram
- (cluster-default, agent-runner): 17 fields — agent behavior defaults
- (host, agent-runner): 16 fields — agent runner daemon config
- (agent, agent-runner): 4 fields — per-agent overrides

**Key guarantee:** `/api/bootstrap` reads the `.env` file directly (not os.environ) via `bootstrap_config_values()` → `runtime_config.read_env_aliases()`, so dropping agent keys does not affect the gateway's outward distribution.

## Implementation strategy

### Phase A: os.environ assembly (PR 1-3)

1. **Define profile key sets**: add `GATEWAY_PROFILE_ENV_KEYS` etc. in `shared/env_keys.py`
2. **Rework load_ava_env()**: after loading .env, filter by profile, keep only the current profile's allowed keys
3. **Abolish _enforce_cluster_env_authority()**: positive allow-list replaces negative drop-list

### Phase B: Settings sub-models built on demand (PR 4)

Each process builds only the Settings sub-models it needs. The gateway does not need agent/sandbox/web sub-models; note that the **lm/telegram/feishu domains ARE genuinely consumed by the gateway process** (see the consumption matrix below: gateway HTTP reads settings.lm in 6 places, im_bridge reads telegram in 7 + feishu in 1) — **they must stay in the gateway profile** — on 2026-08-06 deriving the pop set by capability wrongly deleted telegram/feishu keys and took IM fully down (P0, #1570); per-process env/profile sets must be derived from the consumption matrix, never approximated by the capability grouping axis.

### Consumption matrix (re-checked per cell via grep on 2026-08-06, PR-A)

`settings.<domain>` read counts (AST scan; the gateway side includes HTTP + all gateway daemons; `shared/` is imported by every process, its reads verified separately via import closure):

| domain | gateway | agent | runner | CLI |
|---|---|---|---|---|
| data_plane | 23 | 24 | 14 | 32 |
| gateway | 13 | 5 | 5 | 0 |
| general | 3 | 5 | 8 | 15 |
| observability | 0* | 2 | 0* | 9 |
| services | 22 | 8 | 70 | 5 |
| daemon | 13 | 4 | 14 | 0 |
| alerts | 2 | 0 | 0 | 1 |
| lm | 9 | 43 | 1 | 0 |
| sandbox | 0 | 20 | 2 | 1 |
| telegram | 7 | 0 | 0 | 0 |
| feishu | 1 | 0 | 0 | 0 |
| agent | 0 | 37 | 0 | 0 |
| web | 0 | 9 | 0 | 0 |

*observability has no direct reads, but `shared/telemetry` is imported by every process via `shared/log`, so all profiles keep it.

**Profile sub-model sets (landed in PR-B, guarded both ways):**
- **gateway** (HTTP + gateway daemon): data_plane, gateway, general, observability, services, daemon, alerts, lm, telegram, feishu — matches the first draft ✓
- **agent**: agent, lm, sandbox, web, data_plane, general, observability, gateway(5), services(8), **daemon(4)** — first draft missed daemon: the ava_fleet plugin's in-agent task_maintenance service reads settings.daemon.task_maintenance_* / task_reminder_backoff_seconds / task_escalate_n (the plugin runs inside the agent process) → the agent profile must include daemon.
- **runner**: services, daemon, general, data_plane, gateway(5), lm(1), observability, **sandbox(2)** — first draft missed sandbox: services/browser/mcp_daemon.py + mcp_wrapper.py read settings.sandbox.mcp_connect_timeout_seconds (browser MCP daemon, runner profile; ava/mcps.py on the agent side reads the same field — a shared agent+runner field) → the runner profile must include sandbox.

D3 leftover verified: the 2 sandbox references in services/milvus no longer exist on main (0 matches), nothing to clean.

**Cross-profile reads found and fixed during PR-B** (each caught by the guard's bidirectional assertions):
1. `ava/_commands.py`'s `settings.agent.commands_enabled` is genuinely consumed by the gateway via `gateway/routers/commands.py` (/api/commands dropdown) → changed to a `has_domain("agent")` guard + `.env` file fallback (same pattern as shared/lm/factory.py). Guard tests recognize reads inside `if settings.has_domain(...)` guards as a legitimate pattern.
2. `ava/web.py` reads `settings.web.*` at module level (`_MAX_COUNT` etc.) → gateway import of ava crashes → changed to lazy reads (values fetched inside functions). This is the structural fix for the "read config at module import time" class of problems.

### Phase C: assembly logic moves out of shared/ (PR 5)

Move profile determination and key-set definitions to each process side (gateway/, agent/, runner/); shared/ keeps only the infrastructure.

## Compatibility guarantees

- `.env` file format unchanged — still the only source of truth on the gateway side
- `/api/bootstrap` protocol unchanged — `bootstrap_config_values()` still returns all BOOTSTRAP_FIELDS
- Config panel / `ava config set` / per-agent overlay / birth_config all byte-compatible
- `restart_required` semantics unchanged

## Acceptance criteria

1. Gateway process os.environ contains only gateway+common capability-domain keys (test assertion + `ps eww` spot check)
2. Gateway memory drops (baseline 275-302MB → target < 250MB)
3. Known leak scenarios have regression tests (watcher tests, shell-env-leak test)
4. All 231 fields still readable/editable in the config panel
5. Full test suite green
6. CI adds a lint boundary contract (forbids cross-process imports)

## Risks and mitigations

- **Risk**: some gateway code path unexpectedly depends on agent-runner domain fields
  - **Mitigation**: full suite + CI green before enqueue; when a dependency is found, evaluate whether it is a bug (fix it) or a design oversight (adjust the key set)
- **Risk**: the memory drop falls short of expectations
  - **Mitigation**: measure with `ps eww` comparison first, confirm agent plugins are no longer loaded
