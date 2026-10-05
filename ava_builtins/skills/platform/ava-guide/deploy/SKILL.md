---
name: deploy
description: Sets up dependencies and starts Ava on a fresh machine or joins a runner to an existing gateway. Use for single-box or split deployment, WSL, private-network configuration, or package mirrors.
---

# Deploy an Ava cluster

`ava init` owns first initialization and its interrupted resume; `ava start` owns
every startup of an initialized home. Package acquisition is a separate operation.
Use the same two entries for a single box, a gateway, and a runner joining a gateway.

The `docs/conventions/runbook.md` in the Ava source checkout describes the runtime contract.
Use [secrets](references/secrets.md) for credential handling and
[split deployment](references/split-deployment.md) for private-network setup.

## Acquire dependencies

Use macOS or Linux, including WSL2. Native Windows application startup is not
supported by the root service owner; see
`docs/conventions/windows-setup.md` in the Ava source checkout.

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
uv python install
env -u VIRTUAL_ENV uv run --no-project --python 3.12 python cli/python_install.py \
  --locked --inexact
```

The dependency tool installs the canonical locked Python graph into the
checkout's `.venv`, created on the exact version `.python-version` pins (the
same version `uv python install` fetches there). It does not create a cluster
registry entry, provision storage, or start services. Package-manager mirrors
are configured separately; see [mirrors](references/mirrors-cn.md).

## Initialize and start a single box

From the canonical checkout:

```bash
.venv/bin/ava init --serve-gateway --serve-agent-runner --machine-name machine-1
```

`ava init` durably records identity and credentials and publishes `~/.ava/.env`. It
starts nothing and allocates no resource. Add the model-provider keys you need to the
private home environment now, before the first start:

```bash
${EDITOR:-vi} ~/.ava/.env
chmod 600 ~/.ava/.env
.venv/bin/ava start
.venv/bin/ava status
```

The first start converges host prerequisites, starts private storage, prepares the
owned database and checkpoints, applies migrations and runner grants, and only
then starts PgBouncer and the root-owned application services. Exit 0 requires
all selected services to be ready. macOS root is a child of the signed
permissions helper; Linux root runs directly or under systemd.

A single-box init defaults to an empty control-plane bearer and loopback-only
access. The runner DB projection still receives its own independent credential.
A gateway-only init mints a bearer and independent data-plane credentials.
Neither a repeated start nor an interrupted init rotates the recorded identity.

Edit the generated file; do not overwrite its identity or derived storage URLs
with `.env.example`. Secret values belong in private files or environment input,
not shell history, command arguments, or logs.

## Configuration and retries

Use `ava init --config-file PATH` for initial Settings configuration. The dotenv input
must be outside the home and the home must have no `.env` yet. Unknown keys, identity
keys, and derived resource ports are rejected. `ava init` runs once: an initialized
home refuses it, and an interrupted init resumes with no flags, from the payload it
recorded. Provider API keys that are not Settings aliases are
local environment credentials, not generic configuration fields.

`ava start` takes no identity flag. A bare repeated `.venv/bin/ava start` retains
capabilities, credentials, ports, and desired services. `--only-service NAME` is a repeatable allowlist;
`--disable-service NAME` is a repeatable exclusion list. These choices are
mutually exclusive and persist. `--all-services` explicitly resets selection.
A changed running root generation requires a normal stop before replacement.

A failed first start keeps its journal and resource custody. Repeating start
resumes the same initialization. A start on a home that was never initialized, or
whose `ava init` was interrupted, refuses and names `ava init`. Do not erase the
journal, overwrite the home, or free its reservation manually. To decommission a host's cluster, `ava cluster
destroy` (at a terminal; you type the home path) performs verified cleanup and marks
the home detached; a destroyed home is not silently reused.

## Join a runner

Start the gateway first, then initialize and start each runner:

```bash
printf 'Capability transport key: ' >&2
IFS= read -rs AVA_DB_CAPABILITY_KEY
printf '\n' >&2
export AVA_DB_CAPABILITY_KEY
.venv/bin/ava init --serve-agent-runner --no-serve-gateway \
  --gateway-url http://<gateway-host>:8000 \
  --machine-name machine-2 --machine-host <this-host-addr> \
  --db-capability /path/to/machine-2.bundle
unset AVA_DB_CAPABILITY_KEY
.venv/bin/ava start
```

The bundle comes from `ava cluster db-authority issue-unit --machine machine-2
--home <the runner's $AVA_HOME> --out machine-2.bundle` on the gateway, which
prints its transport key once. The runner never needs the gateway's cluster
secret: the capability's API token authenticates it. `ava init` installs that
capability and persists local identity; the first start registers the host and waits
for its selected services. It
creates no local cluster data plane. To refresh the unit's capability (a rotated human secret changes its telemetry token), install the
newly issued bundle on the stopped unit with `ava cluster db-authority install-unit
<bundle>` (the key in `AVA_DB_CAPABILITY_KEY` again), then `ava start`. Each runner process fetches current
connection facts from the gateway at Settings construction. See
[join a runner](references/join-a-runner.md) for health-port and TLS inputs.

## Dev worktrees

A worktree owns no cluster. Acquire dependencies into its own real `.venv` and
verify with selected tests and CI (`docs/conventions/dev-setup.md#development-in-a-worktree` in the Ava source checkout).
A home that carries its own `source` checkout (`~/.ava`) is operated only by that
checkout's `ava`; any other checkout's `ava` refuses every command.

## Verify the result

Check the exit status, the printed frontend URL, and `.venv/bin/ava status`.
Request a small agent task through the frontend or the gateway API only after
the gateway and runner are ready. Agent processes are always created through
the gateway; never launch them directly.

Unit contracts cover identity recovery, input conflicts, service selection,
provisioning order, readiness failure, and exact cleanup. Native init plus first-start and
split-host deployment still need their own runtime evidence; passing a package
installation or mocked test is not evidence that a cluster is serving.
