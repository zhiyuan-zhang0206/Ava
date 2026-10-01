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
uv python install
env -u VIRTUAL_ENV uv run --no-project --python 3.12 python cli/python_install.py \
  --locked --inexact
```

The dependency tool installs the canonical locked Python graph into the
checkout's `.venv`, created on the exact version `.python-version` pins (the
same version `uv python install` fetches there). It does not create a cluster
registry entry, provision storage, or start services. Package-manager mirrors
are configured separately; see [mirrors](references/mirrors-cn.md).

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
or free its reservation manually. To decommission a host's cluster, `ava cluster
destroy` (at a terminal; you type the home path) performs verified cleanup and marks
the home detached; a destroyed home is not silently reused.

## Join a runner

Start the gateway first, then use the same entry on each runner:

```bash
printf 'Capability transport key: ' >&2
IFS= read -rs AVA_DB_CAPABILITY_KEY
printf '\n' >&2
export AVA_DB_CAPABILITY_KEY
.venv/bin/ava start --serve-agent-runner --no-serve-gateway \
  --gateway-url http://<gateway-host>:8000 \
  --machine-name machine-2 --machine-host <this-host-addr> \
  --db-capability /path/to/machine-2.bundle
unset AVA_DB_CAPABILITY_KEY
```

The bundle comes from `ava cluster db-authority issue-unit --machine machine-2
--home <the runner's $AVA_HOME> --out machine-2.bundle` on the gateway, which
prints its transport key once. The runner never needs the gateway's cluster
secret: the capability's API token authenticates it. First start installs that
capability, persists local identity, registers the host, and waits for its
selected services. It
creates no local cluster data plane. Each runner process fetches current
connection facts from the gateway at Settings construction. See
[join a runner](references/join-a-runner.md) for health-port and TLS inputs.

## Dev worktrees

A worktree owns no cluster. Acquire dependencies into its own real `.venv` and
verify with selected tests and CI ([development in a worktree](../../../conventions/dev-setup.md#development-in-a-worktree)).
A home that carries its own `source` checkout (`~/.ava`) is started, stopped and
updated only by that checkout's `ava`.

## Verify the result

Check the exit status, the printed frontend URL, and `.venv/bin/ava status`.
Request a small agent task through the frontend or the gateway API only after
the gateway and runner are ready. Agent processes are always created through
the gateway; never launch them directly.

Unit contracts cover identity recovery, input conflicts, service selection,
provisioning order, readiness failure, and exact cleanup. Native first-start and
split-host deployment still need their own runtime evidence; passing a package
installation or mocked test is not evidence that a cluster is serving.
