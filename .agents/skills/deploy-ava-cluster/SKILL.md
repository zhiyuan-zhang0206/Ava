---
name: deploy-ava-cluster
description: Use when installing Ava on a fresh machine or joining one to an existing cluster — single-box install, the gateway/agent-runner split, ava enroll, and the --mirror cn path for restricted networks.
---

# Deploy an Ava cluster

> 🚀 **Want to get started quickly?** → Read [QUICKSTART.md](../../../QUICKSTART.md), up and running in 5 minutes.
> This guide covers full deployment scenarios (single-box, multi-box split, China mirror).

This is the operational walkthrough for bringing Ava up from a fresh clone. It
covers the two deployment shapes (single box vs split across machines), the
secrets you must fill, and how to verify. For the runtime internals behind these
commands (clusters, units, the per-cluster data plane, the upgrade flow) see
[`runbook.md`](../../../conventions/runbook.md).

## The role model: capabilities, not a single role

A machine carries a **set of capabilities**, not one role:

- **`gateway`** — owns the data plane (Postgres + Redis) and runs the HTTP
  gateway + its daemons (frontend, labeler, memory-indexer, …). It is the cluster
  control surface.
- **`agent-runner`** — runs agent processes, the ops server, and the restarter.
  Agents only ever execute on a host that carries this capability.

A host can carry one or both. Each capability is declared independently by its
own boolean: `AVA_MACHINE_SERVE_GATEWAY` and `AVA_MACHINE_SERVE_AGENT_RUNNER`
(CLI `--serve-gateway` / `--serve-agent-runner`). Both on = single box; only
`--serve-gateway` = gateway + daemons, no agents; only `--serve-agent-runner` =
compute only. There is **no default** — you state at least one explicitly,
because picking the wrong one silently is worse than an error.

Two deployment shapes:

| Shape | Machines | Roles |
|---|---|---|
| **Single box** (most installs) | one | `gateway,agent-runner` on one `$AVA_HOME` |
| **Split** (scale / isolation) | two or more | one `gateway` host + one or more `agent-runner` hosts |

Spawning is HTTP-uniform: the gateway always reaches a runner via that runner's
ops server over HTTP — localhost when co-located on a single box, the
private-network address when split. There is no special "local" path.

## Prerequisites

- **Platform**: macOS (Apple Silicon / Intel), Linux (x86_64 / arm64, incl. WSL2).
  For Windows, see **[Windows Setup](../../../conventions/windows-setup.md)**.
- **git** + a clone of this repo (the source tree must stay a git checkout — Ava
  updates itself with a force `git checkout` to the pinned target commit during
  `ava cluster update` / the watchdog self-heal; the CLI is the only entry point,
  `ava.self.update()` raises).
- **Postgres 17 + Redis (native, no Docker)** for the `gateway` capability:
  `install.sh` `brew install`s `postgresql@17 redis@8.2 pgbouncer` on macOS and
  apt-installs the equivalents on Linux. A pure `agent-runner` host needs neither.
- **Linux: a non-root user with passwordless sudo.** The install births a
  per-cluster Postgres via `initdb`, and Postgres refuses to run `initdb` as
  root — a fresh VPS usually lands you as root, and `install.sh` refuses that
  up front with the fix. The apt steps use `sudo -n` automatically when
  available; a host without passwordless sudo degrades to warnings (packages
  must then be installed by hand with `sudo`). A runner-only host
  (`--role agent-runner`, no local data plane) may install as root.
- **Homebrew** (macOS gateway only) — `install.sh` uses it for pg/redis.
- **session backend** — no external dependency; the native process supervisor (POSIX) / winproc (Windows) is in-repo.
- **Node ≥ 20.9** for the `gateway` web UI (Next.js frontend). On **Linux**
  `install.sh` installs node 22 via nodesource. On **macOS it does not install
  node** — it only warns when node is missing or too old, because it cannot
  install it silently; run `brew install node@22` yourself, or run the gateway
  headless (`ava start --disable-service frontend`).
- **uv** — `install.sh` bootstraps it from `scripts/provision/toolchain.sh`, which downloads the operator-pinned release (fixed version + sha256; canonical pin in `shared/brew_pin.py`).
- **Python 3.12** — `install.sh` calls `uv python install 3.12`.

## The canonical path

The install shape is `$AVA_HOME` — a directory that carries the cluster's
identity, its secrets, its data plane, and its runtime state:

| Path | Role |
|---|---|
| `$AVA_HOME/source/` (default `~/.ava/source/`) | **prod** — cwd of the long-running service sessions |
| `~/Ava/` | **dev clone** — worktree dev under `.worktrees/<task>/` |

## Single box

```bash
# 1. Clone and install (one machine does everything).
mkdir -p ~/.ava
git clone https://github.com/zhiyuan-zhang0206/Ava.git ~/.ava/source
cd ~/.ava/source
./scripts/install.sh --role gateway,agent-runner

# 2. Open the private env file and add your model key without putting it in
#    shell history. Add a line such as: DEEPSEEK_API_KEY=<your key>
${EDITOR:-vi} ~/.ava/.env
chmod 600 ~/.ava/.env

# 3. Start the cluster. --machine-name is the one identity field the install
#    cannot guess for a prod home; it is persisted, so later starts need no flags.
ava start --machine-name <name>
```

`install.sh` births the cluster: `~/.ava/.env` is populated, the database is
provisioned, and — for the single-box role this walkthrough uses — NO cluster
secret by default: the cluster runs fully unauthenticated on loopback (gateway
API, /ops, pg/redis). The last step prints the frontend URL. If you deliberately
turn auth on, follow the operator secret-handling guidance in
[`references/secrets.md`](references/secrets.md).

## Verify: spawn your first agent

The frontend UI is at `http://localhost:3000`, or use the API directly:

```bash
# Default single box (no cluster secret):
curl -XPOST http://localhost:8000/api/agents -H 'content-type: application/json' \
     -d '{"prompt":"say hi","prompt_source":"user"}'

# Authenticated cluster: read the secret without echo/history, then stream the
# Authorization header to curl so the expanded secret never appears in argv.
printf 'Cluster secret: ' >&2
IFS= read -rs AVA_CLUSTER_SECRET
printf '\n' >&2
export AVA_CLUSTER_SECRET
printf 'Authorization: Bearer %s\n' "$AVA_CLUSTER_SECRET" | \
  curl -XPOST http://localhost:8000/api/agents \
       -H @- -H 'content-type: application/json' \
       -d '{"prompt":"say hi","prompt_source":"user"}'
unset AVA_CLUSTER_SECRET
```

`prompt_source` (who the prompt arrived from, e.g. `user`) is required whenever
`prompt` is given — omitting it is a 422.

### Secrets reference

See [`references/secrets.md`](references/secrets.md) for all env vars and their
purposes (model keys, cluster secret, tracing, memory pool).

## First-time machine bring-up

**Setup path — agent-first, no TTY prompt**:

To bring up a machine for the first time, the setup fields are passed via `ava start` CLI flags;
the CLI writes the values into `$AVA_HOME/<field>` files, so subsequent `ava start` invocations
don't need to pass them again:

```bash
# This host's name in the roster; the next two flags state its capabilities.
# The optional memory remote initializes a central memory pool, and every unit
# records the gateway URL.
ava start \
  --machine-name machine-1 \
  --serve-gateway --serve-agent-runner \
  --memory-remote git@github.com:you/AvaMemory.git \
  --gateway-url http://localhost:8000
```

env equivalents: `AVA_MACHINE_NAME` / `AVA_MACHINE_SERVE_GATEWAY` +
`AVA_MACHINE_SERVE_AGENT_RUNNER` (bools) / `AVA_MEMORY_REMOTE` /
`AVA_GATEWAY_URL`. Precedence: env > file > CLI flag.

`--machine-name` and `--gateway-url` are both required fields, but on a fresh
single box **only `--machine-name` is genuinely unresolved**: the install
already wrote the serve flags and a loopback `AVA_GATEWAY_URL` into the
cluster's `.env` (`shared.cluster.derive_env`), and it deliberately does not
invent a machine name for a prod home. Pass the rest only when overriding.
`--machine-description` and `--memory-remote` are optional (an empty memory
remote inits the pool locally with no remote).

Missing values → CLI prints actionable error + exit 1. **Does not** enter a TTY
prompt — agents calling `ava` have no TTY and would hang.

### WSL agent-runner host bring-up notes

1. **Clone into `~/`, not `/mnt/c/`** — cross-filesystem IO is 10×+ slower.
2. **inotify watcher limit** — add `fs.inotify.max_user_watches=524288` to `/etc/sysctl.conf`.
3. **WSL2 IP drift** — for cross-machine use the private-network address, not WSL's internal IP.
4. **Docker is not required** — pg/redis is native.
5. **vhdx does not auto-shrink** — cap with `~/.wslconfig` and periodically `optimize-vhd`.

## Mirrors for restricted networks

See [`references/mirrors-cn.md`](references/mirrors-cn.md) for the `--mirror cn`
path that swaps pip/npm/brew sources to Chinese mirrors.

## Split across machines

See [`references/split-deployment.md`](references/split-deployment.md) for
gateway + agent-runner split deployment with `install.sh --role`.

## Agent-runner enroll

See [`references/enroll-a-runner.md`](references/enroll-a-runner.md) for the
`ava enroll` flow and the bootstrap config contract.

## One anchor: db/redis URLs are never configured

The gateway URL is the single anchor an operator configures at deploy
(`ava enroll --gateway <url>` on each runner, plus the gateway's own
`AVA_MACHINE_HOST` for a split box). Everything else derives:

- The cluster's db/redis URLs are born loopback at install
  (`shared.cluster.derive_env`) and are **never hand-edited** — they are
  cluster-pinned and not operator-writable. `AVA_DB_URL` is the one access URL
  every process dials as-is; with PgBouncer on (the default) it carries the
  pooler's port, not Postgres's own — migrations and `pg_dump` are the only
  callers that dial Postgres directly.
- A runner's `.env` caches no db/redis URL: every process fetches them from the
  gateway's `/api/bootstrap` at start, with the URL hosts rewritten to the
  gateway's reachable address (its `machine_host`) by
  `shared/config/service_read.py`. The data plane always lives on the gateway box.
- The db/redis host derives from the gateway's **machine_host**, not from the
  gateway URL itself: the URL may be a hostname/proxied while the data plane
  binds the private address. `enroll` refuses a remote gateway that serves
  loopback data-plane URLs (machine_host unset) instead of letting the runner
  dial its own loopback.

## What `install.sh` does and doesn't

`install.sh` is a one-shot birth: it installs toolchain deps (brew/apt + uv +
Python, and Node on Linux only), provisions the cluster's own pg/redis, creates
the database, writes the `.env`, and symlinks `ava` onto PATH. It is
idempotent — re-running it is safe.

It does **not** start any services — that's `ava start`. It does **not** touch
the frontend build — `ava start` handles that on first bring-up.

## Dev worktree clusters (`--worktree`)

For dev work, each worktree gets its own isolated cluster via
`scripts/install.sh --worktree` (home `~/.ava-<worktree-dir>` by default,
`--path` overrides), with its own pg/redis/ports/sessions. The install writes
the checkout's `.ava_home` pointer and anchors it to the worktree home. Start it
with the worktree's own `.venv/bin/ava start`.

## Not tested here

Parts of this walkthrough are pinned by tests: the `.env.example` template
loads as `Settings` verbatim (`tests/shared/test_env_example.py`),
`install.sh`'s `--role` / `--worktree` flag contract fails fast
(`tests/shared/test_install_sh_args.py`), the `--worktree` driver flow is
exercised against stub binaries (`tests/shared/test_install_sh_worktree.py`),
and the install-time birth logic
(idempotency, secret mint, seed allowlist, collision refusals) is unit-tested
with the data-plane steps stubbed (`tests/cli/test_install_cluster.py`). Every
relative link and CLI flag on this page is checked against the live argparse
tree by `scripts/check_doc_references.py`, which runs on every commit.

**Not** covered by automated tests: the real package-manager install path
(brew/apt + initdb on a fresh host), the install-time birth against a real
pg/redis (the birth primitives it calls are the same ones `ava start`'s
in-start birth uses), and a from-scratch split deployment across
two physical machines over a real private network — validate the enroll + ops
reachability manually on first setup. The `--mirror cn` path is likewise not
validated from inside the mirrored network: its mechanics are unit-tested
(arg parse + `mirror.env` load), but whether each mirror actually serves every
package is confirmed only by running the install behind a restricted network.
