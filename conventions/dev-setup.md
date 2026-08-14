# Dev setup

Generic dev procedures that sit on top of [`runbook.md`](runbook.md): the
platform-specific traps (WSL2 Tailscale), the first-time agent-runner enrollment
flow, and the per-worktree cluster dev loop. The runbook stays role-agnostic;
this doc covers the per-developer setup steps.

A specific deployment's concrete machine roster, SSH access pattern, cloud-host
access, gateway-host layout, and which local files hold which credentials are
operator-specific and not generic — they belong in your own private deployment
notes, not here. The placeholders to fill in: a gateway is reached at
`http://<gateway-host>:8000`, secrets live in per-machine `~/.ava/.env` +
`~/.ava/secrets/*.env`, and SSH keys are per dev machine.

## WSL2 Tailscale must be installed inside WSL

Runbook §"WSL agent-runner host bring-up notes" point 3 mentions "WSL2 IP
drift"; the trap underneath is that WSL2 does **not** share the
Windows-host Tailscale identity. `tailscale ip` run from WSL with no
in-distro Tailscale returns the Windows host's `100.x` address, which
WSL itself cannot reach. Install Tailscale into the distro:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

After that the distro gets its own Tailscale identity and IP. If you've already
brought the agent-runner up with a wrong `AVA_GATEWAY_URL`, edit `~/.ava/.env`
and re-run `ava start` so the `machines` table UPSERT overwrites the stale row.

## First time bringing up a new dev / agent-runner

0. On the **gateway**: a split deployment always has a cluster secret (a
   gateway-only birth mints one; `--cluster-secret` states it explicitly), and
   `/api/bootstrap` + each runner's `/ops` authenticate against it as a bearer
   token. A single-box cluster with an empty secret is fully unauthenticated on
   loopback — nothing here is conditional on
   the cluster being multi-host. The agent-facing machine surface (`spawn(machine=)`,
   `list_machines`, the roster) is always present too; a single box just sees one
   machine.
1. Install Tailscale, join the tailnet. (WSL2: install inside the
   distro — see above.)
2. Clone the repo to `~/Ava` for dev work, or `~/.ava/source` for
   agent-runner duty: `git clone https://github.com/<user>an-zhang0206/Ava.git ~/.ava/source` (the path matters
   — see runbook §"Prod and dev clone paths").
3. `uv sync` in the clone.
4. Make sure the new host is joined to the tailnet (the gateway is reachable
   only over it). The runner authenticates to `/api/bootstrap` with the cluster
   secret passed in the next step.
5. On the new host: `ava enroll --gateway http://<gateway-host>:8000
   --machine-name <new-name> --machine-host <this-host-addr> --cluster-secret <secret>`. Add `--ssl-cert-file
   <ca-bundle>` if you're behind a TLS-MITM proxy. The `/api/bootstrap` response
   carries the cluster connection facts (db/redis URLs etc. — no name travels).
6. `ava start`. Every process on the host re-fetches its config from
   `/api/bootstrap` at Settings build (no `.env` cache since the 2026-08-01
   refactor; so a data-plane re-key reaches an already-enrolled runner on its
   next restart; fails fast if the gateway is unreachable), then start brings
   up the ops / restarter / watchdog sessions (`ava-ops` etc.).

## Per-worktree cluster dev flow

**Mental model — a dev copy is a full, isolated, prod-like deployment addressed
by location.** Prod and every worktree are the *same shape*: a complete unit with
its own database, redis namespace, ports, home, and running processes. They differ
only in whether the code was copied to a canonical dir (prod: `install.sh` ->
`~/.ava/source`) or referenced in place (dev: the worktree). This is `pip install
.` vs `pip install -e .`, or a system `ffmpeg` vs a dev build run as `./ffmpeg` —
the dev build is reached by *location*, never by a global name competing with the
installed one. The more a dev copy resembles a real deployment, the fewer prod/dev
surprises.

The location-addressed entrypoint is **`.venv/bin/ava`, run from inside the
worktree** (the `./ffmpeg` analog — a literal path into this checkout, no resident
`uv run` wrapper). `uv sync` does the editable package install and creates that
console script, so `.venv/bin/ava` runs the worktree's code live; the host-global
`ava` on PATH always means prod and is never shadowed (converge skips the
host-global symlink/PATH wiring for dev clusters). There is deliberately **no
`./ava` / global `ava-<name>` dev shim** — addressing is by location: the `.venv`
physically lives in this worktree (a literal `./ava` is also blocked: the repo root
already has an `ava/` package dir).

The first `ava start` from a worktree is the "editable install + deploy" step: it
brings the in-place tree up as its own cluster (name defaults to the worktree dir
`<name>`), giving it:

- its own Postgres + Redis instance under the cluster's `$AVA_HOME`, holding
  database `ava_<name>` owned by the per-cluster `ava_<name>` role
- fixed redis channels (`ava:events` / `ava:inbound:<id>`) — same names in every
  cluster, but each cluster has its own redis instance so they never collide
- Port block (gateway, frontend, daemons, milvus, and its own pg/redis at
  base+11 / base+12 — non-overlapping with prod)
- service sessions `ava-<name>-gateway`, `ava-<name>-ops`, etc. (POSIX: detached native processes; agent interactive shells run in per-session pty hosts)
- a `.ava_home` pointer written into the worktree (gitignored) — the
  editable-install anchor (cf. pip's `.pth`/`.egg-link`), so **bare invocations
  from this tree resolve to this cluster's home**. This is the DB-layer analog of
  static-by-default linking: a process running this tree structurally cannot reach
  the prod data plane (see runbook §"How a unit finds its home").

```bash
cd ~/Ava/.worktrees/<name>
scripts/install.sh --worktree                # births cluster <name>: uv sync --frozen + own DB/redis/ports/home
                                             # (~/.ava-<name>), NO cluster secret by default (single-machine
                                             # no-auth; --cluster-secret to turn auth on), seeded LLM/web
                                             # keys from ~/.ava/.env, and the .ava_home pointer. --path P
                                             # overrides the home; --no-seed skips the key copy. No host-global
                                             # steps. Idempotent — re-run freely.
.venv/bin/ava start                          # brings up its gateway + agent-runner (ops/restarter/watchdog + agent
                                             # processes); still births in-start if the worktree skipped install.sh
.venv/bin/ava status                         # sessions/probes for this cluster
.venv/bin/ava cluster down --path ~/.ava-<name>  # stop the cluster's sessions (its own Postgres/Redis + registry slot stay up)
```

`ava cluster down` stops only this cluster's sessions; its own native
Postgres/Redis instance and its registry slot stay up.
`ava cluster destroy --path ~/.ava-<name>` additionally frees the slot (its port
block) and deregisters that cluster's OS-scheduled jobs (health probe, both
watchdog probes, autostart), and `--drop-db` drops its database. (A bare `ava stop` here tears down
only this worktree cluster's own private pg/redis instance — it can no longer
touch the prod data plane, since every cluster owns a separate instance; still,
don't run prod-affecting commands from a worktree.)

A worktree cluster is a single-machine birth, so it carries NO cluster secret
by default — the whole cluster (gateway API, /ops, pg/redis) serves
unauthenticated on loopback (user decision: off is fully off). `install.sh
--worktree --cluster-secret TOKEN` states one explicitly if you want auth on a
dev cluster. The secret is never inherited from prod; this is only a manual step for a
worktree that skipped the install.

**What needs a cluster, what doesn't** (pick the lightest loop that covers the
change):

- **Tests** (`.venv/bin/pytest ...`) need **no** cluster. `tests/conftest.py` starts
  throwaway native Postgres/Redis (per worker, no Docker) and overrides the db/redis
  URLs, so the suite never touches a real cluster or prod. Fastest inner loop — reach for it
  first. Pick the dir by change type (see CLAUDE.md "Workflow": DB -> `tests/ava` +
  `tests/gateway`, wire -> `tests/agent` + `tests/gateway`, agent core ->
  `tests/agent`, frontend -> `cd frontend && npm test`).
- **A bare DB poke / SDK script** (`.venv/bin/python -c "...shared.db.connect()..."`)
  needs the worktree to have been `ava start`'d (born). Before that the worktree is
  *unanchored*: home falls back to `~/.ava` but `AVA_DB_URL` is the unanchored
  sentinel, so a connection fails fast with `UnanchoredHomeError` (telling you to
  run `ava start`) instead of silently hitting the prod DB the host `~/.ava/.env`
  points at.
- **The full running stack** (gateway + agents + frontend, exercising spawn /
  inbound / SSE end to end) needs `ava start` (single-box gateway,agent-runner).
  This is the "full deployment" tier.

For PR / dev workflow conventions (worktree + PR for code changes, doc-axis
direct edits in main), see [`CLAUDE.md`](../CLAUDE.md) "Workflow".
