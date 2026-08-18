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
| Homebrew (macOS) | `install.sh` provisions Postgres/Redis via Homebrew — install it first: `https://brew.sh` |
| Non-root user (Linux) | `install.sh` births a per-cluster Postgres via `initdb`, which **refuses to run as root**. Fresh VPS images land you as root — create a user with passwordless sudo first, then run the install as that user: `adduser ava && echo 'ava ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/ava && su - ava` |


> **Windows users**: Windows runs the `agent-runner` capability natively — no
> WSL2, no Docker — and enrolls against a gateway on macOS or Linux. It
> cannot host the cluster itself. Follow the
> [Windows setup guide](conventions/windows-setup.md) instead of this one.

---

## Step 1: Clone and install

```bash
# Clone to the canonical path
mkdir -p ~/.ava && cd ~/.ava
git clone https://github.com/zhiyuan-zhang0206/Ava.git source && cd source

# Run the install script (single-machine deployment, no auth on loopback)
./scripts/install.sh --role gateway,agent-runner

# To opt into auth at birth, run this secure form INSTEAD of the command above:
printf 'Install cluster secret: ' >&2
IFS= read -rs AVA_INSTALL_CLUSTER_SECRET
printf '\n' >&2
export AVA_INSTALL_CLUSTER_SECRET
./scripts/install.sh --role gateway,agent-runner
unset AVA_INSTALL_CLUSTER_SECRET
```

`install.sh` automatically installs the dependencies: uv, Python 3.12,
Postgres 17, Redis, and Node.js (Node is recommended on macOS; the
web UI and browser tools need it).

> **After install**, reopen your terminal, or run `source ~/.bashrc` (Linux) /
> `source ~/.zshrc` (macOS), to make the `ava` command available on PATH. If
> `ava` is still not found (e.g. uv already existed), add it manually:
> `export PATH="$HOME/.local/bin:$PATH"`.

---

## Step 2: Minimal config

The last step of `install.sh` births the cluster: `~/.ava/.env` is already populated
with database/Redis connection strings, the gateway URL, and role toggles.
**Edit it directly**
(do not use `cp .env.example` to overwrite it wholesale — that would clobber these
derived values). At minimum, add model configuration:

```ini
# Pick a model and its API key (choose one)
AVA_MODEL=deepseek-v4-pro
DEEPSEEK_API_KEY=sk-your-key-here
```

> On a single box the cluster runs unauthenticated on loopback, so
> `AVA_CLUSTER_SECRET` is left empty by default. The authenticated birth form
> in Step 1 keeps the secret out of shell history and process argv. Gateway-only
> split deployments mint a secret automatically.
>
> For the full config reference, see [`.env.example`](.env.example) and the
> [secrets reference](.agents/skills/deploy-ava-cluster/references/secrets.md).

---

## Step 3: Launch

```bash
ava start --machine-name my-machine --gateway-url http://localhost:8000
```

The cluster was already birthed during install (database and ports are ready);
`ava start` only brings up the services. You'll see `ready` when it succeeds.
(If the home was never birthed via `install.sh`, `ava start` will error and point
you to `install.sh` / `ava enroll`.)

Verify:

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
│  Redis 8.8    ←──  Real-time event stream      │
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

The install script symlinks `ava` to `~/.local/bin/ava`. Make sure that path is on PATH:

```bash
export PATH="$HOME/.local/bin:$PATH"
# To make it permanent, add the line above to ~/.bashrc or ~/.zshrc
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

```bash
ava cluster update   # rolls the latest merged main across the cluster
```

Never `git pull` + `ava start` on a production checkout — the update path is
`ava cluster update` only.

### Windows-specific

See [Windows setup guide](conventions/windows-setup.md). Common issues:
- Agent goes offline after a restart → `ava start` registers a logon autostart task; check `schtasks /Query /TN \Ava\`
- `ava start` refuses with "cannot host a per-cluster data plane" → this host was asked to serve the gateway capability; an enrolled agent-runner never sets `AVA_MACHINE_SERVE_GATEWAY`
- Gateway reports the machine offline → it dials the address you passed as `ava enroll --machine-host`; confirm it is reachable *from the gateway*

---

## Getting help

- Read the [full documentation](conventions/)
- File a [GitHub Issue](https://github.com/zhiyuan-zhang0206/Ava/issues)
