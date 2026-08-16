### Agent-runner bring-up (`ava enroll`)

An agent-runner runs one inbound port: the ops server (`services/agent_ops`).
Gateway → agent-runner RPC is a direct `POST /ops` over the private network; the
gateway presents the cluster secret (`AVA_CLUSTER_SECRET`) as a bearer token and
the runner's `/ops` verifies it, so reachability on the network is not trust
(`/healthz` stays open for the watchdog). With a secret set the ops server binds
`0.0.0.0`; without one it stays on loopback. The host must therefore be reachable
on the private network at a stable address — which the operator ensures (an
overlay network / VPN that gives each node a stable address is the typical setup).

One-time setup per agent-runner:

1. **Install the runner role** from the canonical prod checkout at
   `$AVA_HOME/source` (default `~/.ava/source`) —
   `./scripts/install.sh --role agent-runner`. This installs the toolchain only;
   it births no cluster, because a runner's identity arrives from the gateway
   at enroll.

2. **Enroll**:

   ```bash
   printf 'Cluster secret: ' >&2
   IFS= read -rs AVA_CLUSTER_SECRET
   printf '\n' >&2
   export AVA_CLUSTER_SECRET
   ava enroll --gateway http://<gateway-host>:8000 \
              --machine-name machine-2 \
              --machine-host <this-host-addr>
   unset AVA_CLUSTER_SECRET
   ```

   The three identity/reachability flags and `AVA_CLUSTER_SECRET` are required.
   Environment injection is deliberate: it keeps the bearer secret out of
   shell history and process arguments. Every enrollable gateway is a split
   deployment, and a split gateway birth mints this secret; retrieve it through
   the operator's secret-transfer channel rather than copying the gateway's
   `.env` onto the runner. The legacy `--cluster-secret` input remains accepted
   for existing automation but is not the documented path.

   Enroll first verifies connectivity by fetching the full config bundle from
   `GET /api/bootstrap` (presenting the cluster secret), and only on success
   atomically writes a 0600 `~/.ava/.env` (gateway URL, machine name, role,
   `AVA_CLUSTER_SECRET`) plus a `~/.ava/machine_host` file holding
   `--machine-host` — this runner's reachable address, where the gateway dials
   its ops server. It lives in its own file (not the `.env`) so a re-enroll,
   which rewrites the `.env`, cannot wipe it; without it the runner would
   register its ops endpoint at localhost and the gateway would dial itself.
   Enroll refuses up front when the gateway URL is remote but `--machine-host`
   is loopback, rather than letting that misconfiguration surface weeks later.

   **No cluster connection facts are written** — there is no materialized
   `.env` cache of the db/redis URLs.

   Two optional flags: `--ssl-cert-file` points at a CA bundle for a corp
   TLS-MITM host, and `--health-port-base` moves this unit's daemon health-port
   block when another Ava unit shares the machine's localhost namespace (a
   second install, or a WSL2 distro whose loopback Windows can reach). Omit it
   on a machine that carries one unit.

3. **`ava start`** — a pure agent-runner fetches `GET /api/bootstrap`
   (`AVA_DB_URL`, `AVA_REDIS_URL`, secrets, channel names, …) at **every**
   process's Settings build (`shared.config` -> `shared.bootstrap`), with the
   fetched values authoritative over env/.env — there is no cache to go stale.
   The gateway being unreachable fails the start (exit 1, retried by the boot
   policy) rather than coming up on stale connection strings; a daemon dies at
   boot and is revived by the OS watchdog probe once the gateway is back. A
   data-plane re-key (new redis/pg URL) reaches the runner on its next process
   restart with no hand-editing. Agent processes self-fetch the same way at
   spawn (they carry only the bootstrap/identity keys, never the secret
   snapshot), so a restarted agent picks up a rotated key without restarting
   the host's daemons.

   `ava start` brings up the ops, restarter, and watchdog sessions. No local
   gateway — the ops server serves inbound `POST /ops` on the private network
   and runs each op in-process.

**Startup order matters**: bring the gateway up first, then the runners — a
runner cannot start (and its daemons cannot boot) until the gateway answers
`/api/bootstrap`.

The connection facts a runner needs are assembled gateway-side by
`bootstrap_config_values(role="runner")` (`shared/config/service_read.py`) and
shipped over `/api/bootstrap`. That role projection replaces the main Postgres
credential with the independent least-privilege `ava_runner` credential; its
password is carried only inside the served `AVA_DB_URL`. Test-database URLs are
not in the bundle, because agent-runners do not run tests.
