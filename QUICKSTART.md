# Meiva Quickstart

From zero to your first AI agent in about 15 minutes (mostly dependency downloads).

Meiva is a **code-execution** multi-agent system. Agents act by writing Python code —
they can spawn each other, communicate, and self-upgrade. One `meiva` command manages
the entire cluster.

---

## Prerequisites

| You need | Details |
|----------|---------|
| macOS or Linux | This guide sets up a whole cluster, which requires hosting Postgres + Redis. |
| Model API Key | Anthropic (`ANTHROPIC_API_KEY`), DeepSeek (`DEEPSEEK_API_KEY`), or OpenAI (`OPENAI_API_KEY`) |
| Git | For cloning the repo |
| A terminal | All commands in this guide run in a terminal |
| Homebrew (macOS) | The first `meiva start` provisions Postgres/Redis via Homebrew — install it first: `https://brew.sh` |
| Non-root user (Linux) | The first `meiva start` births a per-cluster Postgres via `initdb`, which **refuses to run as root**. Fresh VPS images land you as root — create a user with passwordless sudo first, then run the install as that user: `adduser meiva && echo 'meiva ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/meiva && su - meiva` |

---

## Step 1: Clone, install and initialize

```bash
# Clone to the canonical path
mkdir -p ~/.meiva && cd ~/.meiva
git clone https://github.com/zhiyuan-zhang0206/Meiva.git source && cd source

# Install dependencies + the `meiva` CLI; no cluster is created yet
uv sync

# Record this machine's identity once (single machine). Starts nothing.
.venv/bin/meiva init --serve-gateway --serve-agent-runner --machine-name my-machine
```

`uv sync` installs the dependencies: uv, Python 3.12, Postgres 17, Redis, and
Node.js (Node is recommended on macOS; the web UI and browser tools need it).
`meiva init` records the cluster's identity: its ports, its generated credentials
(including the cluster secret, `MEIVA_CLUSTER_SECRET`) and `~/.meiva/.env`. It starts no process and creates no database; an initialized home
refuses a second `meiva init`.

---

## Step 2: Minimal config, then start

`meiva init` already populated `~/.meiva/.env` with database/Redis connection
strings, the gateway URL, and role toggles. **Edit it directly**
(do not use `cp .env.example` to overwrite it wholesale — that would clobber these
derived values). At minimum, add model configuration:

```ini
# Pick a model and its API key (choose one)
MEIVA_MODEL=deepseek-flash
DEEPSEEK_API_KEY=sk-your-key-here
```

Then start the cluster:

```bash
.venv/bin/meiva start
```

The first `meiva start` creates the cluster's own Postgres/Redis, applies the schema and
brings up services; its converge phase also links `~/.local/bin/meiva` to this checkout's
CLI. You'll see `ready` when it succeeds. Later starts reconcile the running cluster
against the edited file.

> **After the first start**, reopen your terminal, or run `source ~/.bashrc` (Linux) /
> `source ~/.zshrc` (macOS), to put `~/.local/bin` on PATH. If `meiva` is still not
> found (e.g. uv already existed), add it manually:
> `export PATH="$HOME/.local/bin:$PATH"`.

> Every cluster authenticates its API with the cluster secret that `meiva init` mints into
> `~/.meiva/.env` (`MEIVA_CLUSTER_SECRET`); the CLI, SDK and local services present their own
> credentials automatically. It is also the password of the web UI login. Whether other machines
> can reach the cluster is a separate setting: a single box listens on loopback only until you
> declare a private-network address (`--machine-host` / `MEIVA_MACHINE_HOST`). To replace the secret
> of a running cluster use
> [`scripts/data_plane_ops/rotate_cluster_secret.py`](scripts/data_plane_ops/rotate_cluster_secret.py).
>
> For the full config reference, see [`.env.example`](.env.example) and the
> [secrets reference](meiva_builtins/skills/platform/meiva-guide/deploy/references/secrets.md).

---

## Step 3: Verify

```bash
meiva status
```

---

## Step 4: Create your first agent

Open the one-time login link that `meiva start` printed in its ready output (it looks like
`http://localhost:3000/#ticket=...`, is valid for two minutes and works once), and chat with
your agent in the Web UI. If the link expired, or `meiva start` ran without a terminal and
printed none, open **http://localhost:3000** and log in with the `MEIVA_CLUSTER_SECRET` value
from `~/.meiva/.env` instead.

Or use the command line:

```bash
# Create an agent via the API, presenting the cluster secret as a Bearer token
# (one Idempotency-Key per creation intent)
SECRET=$(grep '^MEIVA_CLUSTER_SECRET=' ~/.meiva/.env | cut -d= -f2)
curl -XPOST http://localhost:8000/api/keyed/v1/agents \
  -H "Authorization: Bearer $SECRET" \
  -H 'content-type: application/json' \
  -H "Idempotency-Key: $(uuidgen)" \
  -H 'Idempotency-Scope: principal-v1' \
  -d '{"prompt":"Hello, introduce yourself","prompt_source":"user"}'
```

The agent starts in the background. You can see its reply in the Web UI.

---

## What just happened?

A complete Meiva cluster is now running on your machine:

```
┌── Your machine ──────────────────────────────┐
│                                                │
│  Postgres 17  ←──  Persistent state            │
│  Redis 8.2    ←──  Real-time event stream      │
│  Gateway      ←──  HTTP API (:8000)            │
│  Agent Runner ←──  Runs agent processes        │
│  Frontend     ←──  Web UI (:3000)              │
│                                                │
│  ~/.meiva/source/  ←──  Code (git repo)          │
│  ~/.meiva/.env     ←──  Config                   │
└────────────────────────────────────────────────┘
```

---

## Next steps

- **[Deploy guide](meiva_builtins/skills/platform/meiva-guide/deploy/SKILL.md)** — Multi-machine deployment, China mirrors, full config reference
- **[Architecture overview](docs/index.meiva.okf.md)** — Understanding components and data flow
- **[Dev environment setup](docs/conventions/dev-setup.md)** — If you want to contribute
- **[Skill system](docs/skills/skills.meiva.okf.md)** — What agents can do

---

## FAQ

### `meiva: command not found`

The production home's first `meiva start` links `~/.local/bin/meiva` to the checkout's CLI
(its converge phase). Put that path on PATH:

```bash
export PATH="$HOME/.local/bin:$PATH"
# To make it permanent, add the line to ~/.bashrc or ~/.zshrc
```

### `meiva start` says "MEIVA_CLUSTER_SECRET is required"

Every gateway home needs a cluster secret, and `meiva init` mints one. A home whose `.env`
has no `MEIVA_CLUSTER_SECRET` (hand-edited, or born before this rule) refuses to start. Set the
line in `~/.meiva/.env` to a fresh URL-safe token, for example the output of
`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`, and run `meiva start` again.

### Postgres connection failed

The per-cluster data plane runs under `$MEIVA_HOME/pg`, driven directly by
`pg_ctl` — not as a system service, so `brew services` / `systemctl` report
nothing even on a healthy install. Run `meiva status` to see whether the data
plane is up, and check `$MEIVA_HOME/logs/` for errors.

### Port conflict

Defaults are 8000 (Gateway) and 3000 (Frontend). To change them, set in `~/.meiva/.env`:

```ini
MEIVA_GATEWAY_PORT=8001
MEIVA_FRONTEND_PORT=3001
```

### Model API errors

Verify the API key in `~/.meiva/.env` is correct. Test:

```bash
# Check environment variable
grep API_KEY ~/.meiva/.env
```

### How to stop

```bash
meiva stop
```

### How to update

A production cluster runs every unit from its own `$MEIVA_HOME/source` checkout and
is updated from the operator's development checkout by stopping every unit,
switching every checkout and starting again:

```bash
.venv/bin/python -m cli.fleet_update down --new NEW_SHA --gateway GATEWAY --runner RUNNER --log-dir DIR
.venv/bin/python -m cli.fleet_update up --gateway GATEWAY --runner RUNNER --log-dir DIR
```

See [the runbook](docs/conventions/runbook.md#updating-a-networked-cluster-in-source-mode)
for the two halves. Never `git pull` + `meiva start` by hand on a production checkout.

---

## Getting help

- Read the [full documentation](docs/conventions)
- File a [GitHub Issue](https://github.com/zhiyuan-zhang0206/Meiva/issues)
