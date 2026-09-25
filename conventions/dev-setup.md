# Dev setup

Generic dev procedures that sit on top of [`runbook.md`](runbook.md): the
platform-specific traps (WSL2's private-network identity), the first-time
agent-runner enrollment flow, and the per-worktree cluster dev loop. The
runbook stays role-agnostic; this doc covers the per-developer setup steps.

A specific deployment's concrete machine roster, SSH access pattern, cloud-host
access, gateway-host layout, and which local files hold which credentials are
operator-specific and not generic — they belong in your own private deployment
notes, not here. The placeholders to fill in: a gateway is reached at
`http://<gateway-host>:8000`, secrets live in per-machine `~/.ava/.env` +
`~/.ava/secrets/*.env`, and SSH keys are per dev machine.

## WSL2 needs its own private-network identity

Runbook §"WSL agent-runner host bring-up notes" point 3 mentions "WSL2 IP
drift"; the trap underneath is that WSL2 does **not** share the Windows
host's private-network client identity (VPN overlay, LAN, whatever the
deployment uses). Querying the private-network IP from WSL with no
in-distro client joined returns the Windows host's address (e.g. a
`100.x`-style CGNAT address), which WSL itself cannot reach. Install and
join your private-network client inside the distro itself, following that
client's own setup docs.

After that the distro gets its own identity and IP on the private network.
Choose the gateway URL and this host's reachable address before first start.
Repeated start does not change the recorded identity; conflicting inputs refuse.

## First start of a runner

Acquire the checkout's dependencies using the
[deployment procedure](../.agents/skills/deploy-ava-cluster/SKILL.md). A split
gateway must already be serving and have a cluster bearer. On the new host,
join the private network, read/export that bearer without echoing it, then run:

```bash
.venv/bin/ava start --serve-agent-runner --no-serve-gateway \
  --gateway-url http://<gateway-host>:8000 \
  --machine-name <new-name> --machine-host <this-host-addr>
```

Unset the bearer afterward. `--ssl-cert-file PATH` supplies a trusted CA bundle
when required. First start validates the runner projection, durably records
local identity, registers this host, and starts its selected root services.
Every runner process fetches current connection facts at Settings construction.
Use bare start thereafter; conflicting identity flags refuse.

## Per-worktree cluster dev flow

A worktree is a complete isolated deployment addressed by its checkout. Use its
own real `.venv`, never a symlink to another checkout's environment. The global
`ava` on PATH points at production; development invokes `.venv/bin/ava` directly.
Package acquisition and Git hooks are separate from starting a cluster.

Before a manual worktree dependency operation, clear inherited `VIRTUAL_ENV`
and run `scripts/guard_editable_venv.py`. `scripts/setup-worktree.sh` runs this
guard and prepares development dependencies without creating a cluster.

```bash
cd ~/Ava/.worktrees/<name>
scripts/setup-worktree.sh
.venv/bin/ava start --worktree
.venv/bin/ava status
.venv/bin/ava cluster down --path ~/.ava-<name>
```

First start selects `~/.ava-<worktree-dir>` by default, records the checkout's
`.ava_home` pointer, and durably binds identity, credentials, and a private port
block before resource effects. It defaults to gateway plus runner, creates
private native storage, and waits for the selected root tree to be ready. Port
allocation checks both registry reservations and live host listeners. No
production secrets or agent data are copied. Explicit `AVA_HOME` remains subject
to the checkout identity and override rules described in the runbook.

Add needed model-provider credentials to this home's private `.env`; do not
replace its generated identity or storage URLs. A first-start `--config-file`
can supply supported Settings fields from a file outside the home. Its exact
bytes are bound to the initialization and cannot change on a retry.

Bare start preserves desired service selection. Use repeatable `--only-service`
for an allowlist, repeatable `--disable-service` for exclusions, or explicit
`--all-services` to reset. Stop before replacing a running root generation.

`cluster down` performs normal local stop including this home's private storage,
retaining data and its reservation. `cluster destroy` additionally retires this
home's OS jobs and macOS helper and frees the reservation only after verified
cleanup. `--drop-db` explicitly removes private storage. The default production
home cannot be destroyed. A destroyed home refuses startup rather than silently
claiming a new identity over retained data.

Do not rebase or rewrite a checkout under a running cluster: its root manifest
and loaded source must remain coherent. Stop the dev cluster first. Source
checkout editing is development work; it is not a production update mechanism.

**Choose the check that proves the change:**

- Targeted tests need no running cluster. The test harness uses private native
  Postgres/Redis and isolated configuration. Never use a real cluster home for
  test imports or a production endpoint as a test fixture.
- A DB/SDK script requires the intended home to be initialized and explicitly
  bound. An unanchored checkout is not permission to load production Settings.
- End-to-end agent/frontend behavior needs a private running cluster and actual
  gateway scheduling. Start services first, then request agents through the
  gateway.

See [AGENTS.md](../AGENTS.md) for the worktree and PR workflow.

## Machine Python indexes

Dependency acquisition and updates share `cli.python_install`: operators invoke
its standalone script; the updater retains its imported functions before switching
source and passes the target repo explicitly. Historical canonical targets need
not contain the new helper. All updater uv steps share one process-tree deadline.
The committed `uv.lock` stays on canonical PyPI origins. A host mirror changes
artifact transport only: offline `uv export --locked` validates freshness and
exports exact requirements, hashes and markers; `uv pip install --no-deps
--require-hashes` installs them into the real checkout venv; a separate isolated
editable build points Ava at that same checkout. Exported requirements are
short-lived scratch files, never a second maintained lock. A stale or
noncanonical lock fails before the venv changes; mirror hash failures stop before
the editable build. A failed install is not a transactional rollback of every
package. The updater retains its existing editable-record recovery and bound.

Index precedence is explicit `UV_DEFAULT_INDEX` / `UV_INDEX_URL`, then uv
configuration, then `PIP_INDEX_URL`, then pip configuration, then PyPI. The
installer's explicit `--mirror cn` selects and persists its profile as before;
without that flag, the helper reads the unit's existing `mirror.env` without
replacing real environment values. Native command boot preserves this precedence
across both uv single-index aliases while loading `.env` and `mirror.env`, before
an update enters the helper. Additional index settings are not merged into them.
The pip bridge reads only index settings, with global, user, target-venv and
`PIP_CONFIG_FILE` precedence; `[install]` overrides `[global]`, an existing
explicit config file suppresses user files, and `PIP_CONFIG_FILE=/dev/null`
disables file discovery. uv itself does not read pip configuration. No tool
configuration is rewritten, and index credentials stay out of command arguments.
Multiple/additional/explicit-only indexes are rejected explicitly; this is not a
replacement for uv's multi-index/source resolver.

Updates exclude the dev group and preserve already-installed extras. Installer
runs include the project's default groups. Official PyPI uses native
`uv sync --locked --inexact`; both paths preserve the lock's versions and hashes.
The entry point ignores machine resolver configuration when validating the lock,
while uv still reads project metadata, default groups and dependency markers.
Only index selection is bridged from machine configuration files; non-index
uv.toml settings (including TLS/transport settings) are not translated. Existing
transport/cache/TLS environment variables remain inherited. A configured
`UV_CONFIG_FILE` is read for index discovery, then removed from child environments:
uv 0.10.2 reads that explicit file even alongside `--no-config`. Hosts requiring
custom certificates must provide supported uv environment settings; the helper
does not silently claim compatibility with every machine uv.toml option.
Build isolation remains enabled by default. Runtime lock hashes do not introduce
new build-backend pins; build dependencies retain uv's existing build semantics.

A running old updater cannot acquire this helper through a changed source tree.
In particular, its pre-update staging sync may still use the old mirror-incompatible
command. First rollout must verify which updater code executes prepare; a merged
change alone is not proof that an already-running updater can install itself.
