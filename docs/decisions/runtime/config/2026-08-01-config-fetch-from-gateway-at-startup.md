# Every runner process fetches cluster config from the gateway at startup

## Context

A split deployment's agent-runner held only a bootstrap env (gateway URL +
secret, written by `ava enroll`); its cluster connection facts (db/redis URLs,
channels, provider keys — 102 keys) were materialized into its `$AVA_HOME/.env`
once at enroll and re-materialized on every `ava start` (`cli/start_refresh.py`:
fetch `GET /api/bootstrap` → rewrite `.env` → re-exec). The `.env` copy was
authoritative: `shared.dotenv_boot._enforce_cluster_env_authority` forced file
values into `os.environ` on every boot, and the `AVA_CONFIG_SOURCE=gateway`
fetch (`inject_config_from_gateway`) only filled ABSENT keys, so it could never
override them. Consequences:

- A data-plane re-key (new redis/pg URL) stranded a runner until someone
  hand-edited its `.env` — the very cache the start-refresh existed to refresh,
  but only on `ava start`, and only for the 3 keys in `_CLUSTER_ENV_KEYS`
  (db/redis URLs + events channel); the other ~99 served keys were never
  materialized at all, so a rotated provider key reached a runner only through
  the agent self-fetch path.
- The `.env` cache was a second copy of cluster state that could drift, and the
  re-exec mechanism made `ava start` a process-image-replacing step.
- `AVA_CONFIG_SOURCE` was an operator-invisible env var whose value decided the
  config source — a third thing to get wrong.

## Decision

Delete the cache and the source marker. From 2026-08-01:

1. **`AVA_CONFIG_SOURCE` is gone.** The config source is derived from the unit's
   role, settings-free (`shared.bootstrap.config_source_is_local` +
   `should_fetch_from_gateway`): `AVA_MACHINE_SERVE_GATEWAY=true` (env, or the
   `$AVA_HOME/machine_serve_gateway` file) → the local `.env` is the source,
   never fetched; a CONFIGURED pure agent-runner (serve_agent_runner flag on AND
   a gateway URL present — `ava enroll` writes both, fetch-first) → every process
   fetches `GET /api/bootstrap` at Settings build. A bare checkout with no role
   flags (CI, lint scripts, dev tools) and a not-yet-enrolled runner are not
   configured units: they construct Settings from local env/.env with no fetch
   and no error, so any tool that imports `shared.config` works on any machine —
   `ava start`'s preflight gate (AVA_GATEWAY_URL check) is the fail-fast for an
   unenrolled runner, not the Settings import.
2. **Fetched values are authoritative.** `inject_config_from_gateway` overwrites
   `os.environ` for every payload key, running after `load_ava_env`'s
   `_enforce_cluster_env_authority`. A stale pre-cutover `.env` residue is
   tolerated by construction (pushed at load, overridden at fetch).
3. **`cli/start_refresh.py` and `enroll`'s cluster-facts materialization are
   deleted.** `ava enroll` verifies connectivity (one successful fetch) before
   writing the bootstrap env, and writes identity/host-scope facts only.
4. **Maintenance verbs are settings-lite.** `ava stop` / `status` / `restart` /
   the `cluster`/`agents`/`config`/`schedules`/`presets`/`mcp`/`memory`/
   `plugins`/`skill` verb families set `AVA_CONFIG_FETCH=skip` in `cli.main`
   before dispatch: their Settings build skips the fetch and plants never-dialed
   placeholders for the required data-plane URLs, so they work while the gateway
   is down. `shared.session_env` never forwards the flag — a daemon/agent a lite
   verb spawns still fetches per its own role.
5. **Failure posture.** A configured runner whose gateway is unreachable fails
   fast with an actionable `BootstrapFetchError` (`ava start` exits 1; the boot
   policy retries). A daemon dies at import and is revived by the OS watchdog
   probe (itself lite) until the gateway answers — a gateway restart
   self-heals on the runner within the probe cadence. An UNENROLLED runner is
   refused earlier, by the settings-free preflight gate, so the Settings import
   itself never throws on a bare or partial host (tools and lint scripts must
   keep importing).
6. **Startup order is documented, not enforced:** gateway first, runners after
   (runbook.md, the deploy skill). No preflight was added (user ruling).

The gateway unit's behavior is unchanged: it never fetches (its `.env` IS the
cluster's config) and still serves `/api/bootstrap` from `bootstrap_config_values`.

## Consequences

- A cluster edit (pinned value, rotated key, re-keyed data plane) reaches a
  runner on its next process restart — daemons included — with no cache to
  refresh and no hand edit.


- `ava stop`/`status`/watchdog-probe on a runner work with the gateway down
  (the runner's recovery path does not depend on the thing it recovers from).
- A runner's `.env` shrinks to bootstrap/identity facts (~7 keys); the
  migration is tolerant (stale keys are simply overridden at fetch).
- The test rig (multihost containers) and e2e pin `AVA_CONFIG_FETCH=skip`
  where they used to pin `AVA_CONFIG_SOURCE=local`, and e2e's serve-gateway
  flag handling is unchanged (agents fetch from the e2e gateway).

## Follow-up (2026-08-02): session-env stops carrying cluster-scope values

With the fetch in place, `shared.session_env`'s session-env handoffs
(`forward_env_prefix` for the watchdog respawn + interactive/schedule/cluster
sessions, `forward_env_dict` for `ava start`'s daemon children) stopped
forwarding the cluster-scope keys entirely (`_SESSION_ENV_DROP` — exactly the
BOOTSTRAP_FIELDS aliases, derived from the same scope metadata). A daemon that
used to receive a spawner's frozen copy of the cluster config — correct only
because its own boot fetch overwrote it — now starts with host-scope facts
(machine identity, paths, health ports, the gateway URL) and re-sources the
cluster values itself. Two payoffs: the redundant relay layer is gone, and a
third-party library that reads `os.environ` directly (the provider SDKs) can no
longer see a stale cluster value in a pre-fetch window. Agent processes were
already on this policy (`agent_spawn_env_dict` drops the same keys except the
bootstrap guide keys); this closes the daemon/session side.
