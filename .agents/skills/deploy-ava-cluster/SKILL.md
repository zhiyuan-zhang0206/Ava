---
name: deploy-ava-cluster
description: Sets up dependencies and starts Ava on a fresh machine or joins a runner to an existing gateway. Use for single-box or split deployment, WSL, private-network configuration, or package mirrors.
---

# Deploy an Ava cluster

`ava start` owns first initialization, interrupted initialization, and repeated
startup. Package acquisition is a separate operation. Use the same lifecycle
entry for a single box, a gateway, and a runner joining a gateway.

The [runbook](../../../conventions/runbook.md) describes the runtime contract.
Use [secrets](references/secrets.md) for credential handling and
[split deployment](references/split-deployment.md) for private-network setup.

## Acquire dependencies

Use macOS or Linux, including WSL2. Native Windows application startup is not
supported by the root service owner; see
[Windows setup](../../../conventions/windows-setup.md).

Acquire Git, uv, Python 3.12, and the host packages independently. A gateway with
local storage requires Postgres 17 with pgvector, Redis 8.2, and PgBouncer
(default enabled). Run Postgres under a non-root user. The frontend requires
Node supported by the repository's frontend dependencies. macOS additionally
requires the signed permissions helper and a logged-in GUI session for desktop
capabilities. A pure runner does not acquire local Postgres or Redis.

Clone the production source into the canonical home source directory:

```bash
mkdir -p ~/.ava
git clone https://github.com/zhiyuan-zhang0206/Ava.git ~/.ava/source
cd ~/.ava/source
uv python install 3.12
env -u VIRTUAL_ENV uv run --no-project --python 3.12 python cli/python_install.py \
  --locked --inexact --python 3.12
```

The dependency tool installs the canonical locked Python graph into the
checkout's `.venv`. It does not create a cluster registry entry, provision
storage, or start services. Package-manager mirrors are configured separately;
see [mirrors](references/mirrors-cn.md).

## Start a single box

From the canonical checkout:

```bash
.venv/bin/ava start --serve-gateway --serve-agent-runner --machine-name machine-1
```

The first start durably records identity and credentials before allocating
resources. It converges host prerequisites, starts private storage, prepares the
owned database and checkpoints, applies migrations and runner grants, and only
then starts PgBouncer and the root-owned application services. Exit 0 requires
all selected services to be ready. macOS root is a child of the signed
permissions helper; Linux root runs directly or under systemd.

A single-box start defaults to an empty control-plane bearer and loopback-only
access. The runner DB projection still receives its own independent credential.
A gateway-only first start mints a bearer and independent data-plane credentials.
Neither repeated nor interrupted start rotates the recorded identity.

Add the model-provider keys you need to the private home environment before
requesting agent work, then restart to load them:

```bash
${EDITOR:-vi} ~/.ava/.env
chmod 600 ~/.ava/.env
.venv/bin/ava restart
.venv/bin/ava status
```

Edit the generated file; do not overwrite its identity or derived storage URLs
with `.env.example`. Secret values belong in private files or environment input,
not shell history, command arguments, or logs.

## Configuration and retries

Use `--config-file PATH` for first-start Settings configuration. The dotenv input
must be outside the home. Unknown keys, identity keys, and derived resource
ports are rejected. The exact bytes are bound to the initialization journal;
retry with the same file or omit it. A changed file cannot silently rewrite a
partially initialized home. Provider API keys that are not Settings aliases are
local environment credentials, not generic first-start configuration fields.

A bare repeated `.venv/bin/ava start` retains capabilities, credentials, ports,
and desired services. `--only-service NAME` is a repeatable allowlist;
`--disable-service NAME` is a repeatable exclusion list. These choices are
mutually exclusive and persist. `--all-services` explicitly resets selection.
A changed running root generation requires a normal stop before replacement.

A failed first start keeps its journal and resource custody. Repeating start
resumes the same initialization. Do not erase the journal, overwrite the home,
or free its reservation manually. For a disposable non-default home, normal
`ava cluster destroy --path PATH` performs verified cleanup before freeing the
reservation; a destroyed home is not silently reused.

## Join a runner

Start the gateway first, then use the same entry on each runner:

```bash
printf 'Cluster secret: ' >&2
IFS= read -rs AVA_CLUSTER_SECRET
printf '\n' >&2
export AVA_CLUSTER_SECRET
.venv/bin/ava start --serve-agent-runner --no-serve-gateway \
  --gateway-url http://<gateway-host>:8000 \
  --machine-name machine-2 --machine-host <this-host-addr>
unset AVA_CLUSTER_SECRET
```

First start validates the gateway's authenticated runner projection, persists
local identity, registers the host, and waits for its selected services. It
creates no local cluster data plane. Each runner process fetches current
connection facts from the gateway at Settings construction. See
[join a runner](references/join-a-runner.md) for health-port and TLS inputs.

## Dev worktrees

Acquire dependencies into the worktree's own real `.venv`, then run:

```bash
.venv/bin/ava start --worktree
```

This selects `~/.ava-<worktree-dir>` unless an explicit `AVA_HOME` already selects
an allowed home, records the checkout pointer, and defaults to gateway plus
runner. It allocates an isolated port block against both reservations and live
listeners. No production credentials or agent data are copied. An unanchored
source checkout must select its home explicitly through this first-start path.

## Verify the result

Check the exit status, the printed frontend URL, and `.venv/bin/ava status`.
Request a small agent task through the frontend or the gateway API only after
the gateway and runner are ready. Agent processes are always created through
the gateway; never launch them directly.

Unit contracts cover identity recovery, input conflicts, service selection,
provisioning order, readiness failure, and exact cleanup. Native first-start and
split-host deployment still need their own runtime evidence; passing a package
installation or mocked test is not evidence that a cluster is serving.
