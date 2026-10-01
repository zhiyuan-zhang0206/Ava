# Ava Quickstart

From zero to your first AI agent in about 15 minutes (mostly dependency downloads).

Ava is a **code-execution** multi-agent system. Agents act by writing Python code —
they can spawn each other, communicate, and self-upgrade. One `ava` command manages
the entire cluster.

---

## Prerequisites

| You need | Details |
|----------|---------|
| macOS or Linux | This guide sets up a whole cluster, which requires hosting Postgres + Redis. Windows runs the agent-runner half natively and joins a cluster instead — see below. |
| Model API Key | Anthropic (`ANTHROPIC_API_KEY`), DeepSeek (`DEEPSEEK_API_KEY`), or OpenAI (`OPENAI_API_KEY`) |
| Git | For cloning the repo |
| A terminal | All commands in this guide run in a terminal |
| Homebrew (macOS) | The first `ava start` provisions Postgres/Redis via Homebrew — install it first: `https://brew.sh` |
| Non-root user (Linux) | The first `ava start` births a per-cluster Postgres via `initdb`, which **refuses to run as root**. Fresh VPS images land you as root — create a user with passwordless sudo first, then run the install as that user: `adduser ava && echo 'ava ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/ava && su - ava` |


> **Windows users**: Windows runs the `agent-runner` capability natively — no
> WSL2, no Docker — and enrolls against a gateway on macOS or Linux. It
> cannot host the cluster itself. Follow the
> [Windows setup guide](conventions/windows-setup.md) instead of this one.

---

## Step 1: Clone, install and initialize

```bash
# Clone to the canonical path
mkdir -p ~/.ava && cd ~/.ava
git clone https://github.com/zhiyuan-zhang0206/Ava.git source && cd source

# Install dependencies + the `ava` CLI; no cluster is created yet
uv sync

# Record this machine's identity once (single machine, no auth on loopback). Starts nothing.
.venv/bin/ava init --serve-gateway --serve-agent-runner --machine-name my-machine
```

`uv sync` installs the dependencies: uv, Python 3.12, Postgres 17, Redis, and
Node.js (Node is recommended on macOS; the web UI and browser tools need it).
`ava init` records the cluster's identity: its ports, its generated credentials and
`~/.ava/.env`. It starts no process and creates no database; an initialized home
refuses a second `ava init`.

---

## Step 2: Minimal config, then start

`ava init` already populated `~/.ava/.env` with database/Redis connection
strings, the gateway URL, and role toggles. **Edit it directly**
(do not use `cp .env.example` to overwrite it wholesale — that would clobber these
derived values). At minimum, add model configuration:

```ini
# Pick a model and its API key (choose one)
AVA_MODEL=deepseek-flash
DEEPSEEK_API_KEY=sk-your-key-here
```

Then start the cluster:

```bash
.venv/bin/ava start
```

The first `ava start` creates the cluster's own Postgres/Redis, applies the schema and
brings up services; its converge phase also links `~/.local/bin/ava` to this checkout's
CLI. You'll see `ready` when it succeeds. Later starts reconcile the running cluster
against the edited file.

> **After the first start**, reopen your terminal, or run `source ~/.bashrc` (Linux) /
> `source ~/.zshrc` (macOS), to put `~/.local/bin` on PATH. If `ava` is still not
> found (e.g. uv already existed), add it manually:
> `export PATH="$HOME/.local/bin:$PATH"`.

> On a single box the cluster runs unauthenticated and binds loopback only, so
> `AVA_CLUSTER_SECRET` is left empty. A gateway that serves other machines
> (`--serve-gateway --no-serve-agent-runner`) mints a secret at `ava init`. To set or replace the
> secret of a running cluster use
> [`scripts/data_plane_ops/rotate_cluster_secret.py`](scripts/data_plane_ops/rotate_cluster_secret.py).
>
> For the full config reference, see [`.env.example`](.env.example) and the
> [secrets reference](.agents/skills/deploy-ava-cluster/references/secrets.md).

---

## Step 3: Verify

```bash
ava status
```

---

## Step 4: Create your first agent

Open **http://localhost:3000** in your browser and chat with your agent in the Web UI.

Or use the command line:

```bash
# Create an agent via the API
curl -XPOST http://localhost:8000/api/agents \
  -H 'content-type: application/json' \
  -d '{"prompt":"Hello, introduce yourself","prompt_source":"user"}'
```

The agent starts in the background. You can see its reply in the Web UI.

---

## What just happened?

A complete Ava cluster is now running on your machine:

```
┌── Your machine ──────────────────────────────┐
│                                                │
│  Postgres 17  ←──  Persistent state            │
│  Redis 8.2    ←──  Real-time event stream      │
│  Gateway      ←──  HTTP API (:8000)            │
│  Agent Runner ←──  Runs agent processes        │
│  Frontend     ←──  Web UI (:3000)              │
│                                                │
│  ~/.ava/source/  ←──  Code (git repo)          │
│  ~/.ava/.env     ←──  Config                   │
└────────────────────────────────────────────────┘
```

---

## Next steps

- **[Deploy guide](.agents/skills/deploy-ava-cluster/SKILL.md)** — Multi-machine deployment, China mirrors, full config reference
- **[Architecture overview](okf/index.ava.okf.md)** — Understanding components and data flow
- **[Dev environment setup](conventions/dev-setup.md)** — If you want to contribute
- **[Skill system](okf/skills/skills.ava.okf.md)** — What agents can do

---

## FAQ

### `ava: command not found`

The production home's first `ava start` links `~/.local/bin/ava` to the checkout's CLI
(its converge phase). Put that path on PATH:

```bash
export PATH="$HOME/.local/bin:$PATH"
# To make it permanent, add the line to ~/.bashrc or ~/.zshrc
```

### `ava start` says "AVA_CLUSTER_SECRET is required"

On a single box the cluster is deliberately unauthenticated on loopback, so a
manually-added `AVA_CLUSTER_SECRET` that the data plane does not expect triggers
this. Remove the line from `~/.ava/.env` and restart. To enable auth on an
already-born no-auth cluster, provision a new authenticated cluster with the
Step 1 birth form and migrate deliberately: an in-place no-auth-to-auth
transition is not supported. Do not hand-edit only the secret or use the
secret-rotation script for that posture change. Split deployments (gateway-only
birth) mint a secret automatically.

### Postgres connection failed

The per-cluster data plane runs under `$AVA_HOME/pg`, driven directly by
`pg_ctl` — not as a system service, so `brew services` / `systemctl` report
nothing even on a healthy install. Run `ava status` to see whether the data
plane is up, and check `$AVA_HOME/logs/` for errors.

### Port conflict

Defaults are 8000 (Gateway) and 3000 (Frontend). To change them, set in `~/.ava/.env`:

```ini
AVA_GATEWAY_PORT=8001
AVA_FRONTEND_PORT=3001
```

### Model API errors

Verify the API key in `~/.ava/.env` is correct. Test:

```bash
# Check environment variable
grep API_KEY ~/.ava/.env
```

### How to stop

```bash
ava stop
```

### How to update

A production cluster runs every unit from its own `$AVA_HOME/source` checkout and
is updated from the operator's development checkout by stopping every unit,
switching every checkout and starting again:

```bash
.venv/bin/python -m cli.fleet_update down --new NEW_SHA --gateway GATEWAY --runner RUNNER --log-dir DIR
.venv/bin/python -m cli.fleet_update up --gateway GATEWAY --runner RUNNER --log-dir DIR
```

See [the runbook](conventions/runbook.md#updating-a-networked-cluster-in-source-mode)
for the two halves. Never `git pull` + `ava start` by hand on a production checkout.

### Windows hardware

Native Windows Ava services are retired. Install Ava inside a WSL2 Linux
distribution and follow the [Windows host guidance](conventions/windows-setup.md).
For unattended gateway boot, use the separate WSL distribution anchor described
there; `ava start` inside Linux uses the Linux service path.

---

## Getting help

- Read the [full documentation](conventions/)
- File a [GitHub Issue](https://github.com/zhiyuan-zhang0206/Ava/issues)
