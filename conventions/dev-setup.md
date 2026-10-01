# Dev setup

Generic dev procedures that sit on top of [`runbook.md`](runbook.md): the
platform-specific traps (WSL2's private-network identity), the first-time
agent-runner enrollment flow, and development in a worktree. The
runbook stays role-agnostic; this doc covers the per-developer setup steps.

A specific deployment's concrete machine roster, SSH access pattern, cloud-host
access, gateway-host layout, and which local files hold which credentials are
operator-specific and not generic — they belong in your own private deployment
notes, not here. The placeholders to fill in: a gateway is reached at
`http://<gateway-host>:8000`, secrets live in per-machine `~/.ava/.env` +
`~/.ava/secrets/*.env`, and SSH keys are per dev machine.

## Tool caches

Every tool's cache lives under `.cache/<tool>/` (`.cache/pytest`,
`.cache/ruff`, `.cache/import-linter`, ...), ignored by the single root
`/.cache/` line in `.gitignore`. Configure a newly added tool the same way
instead of letting it drop a cache dir at the repo root.

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

## Joining a runner

Acquire the checkout's dependencies using the
[deployment procedure](../.agents/skills/deploy-ava-cluster/SKILL.md). A split
gateway must already be serving and have a cluster bearer. On the new host,
join the private network, then run (the runner never needs the gateway's
bearer: its capability bundle authenticates it):

```bash
.venv/bin/ava init --serve-agent-runner --no-serve-gateway \
  --gateway-url http://<gateway-host>:8000 \
  --machine-name <new-name> --machine-host <this-host-addr> \
  --db-capability <bundle>
.venv/bin/ava start
```

The bundle comes from `ava cluster db-authority issue-unit --machine <new-name>
--home <this unit's $AVA_HOME> --out <bundle>` on the gateway; export its
printed transport key as `AVA_DB_CAPABILITY_KEY` without echoing it, and unset
it afterward. `--ssl-cert-file PATH` supplies a trusted CA
bundle when required. `ava init` installs the capability (and deletes the
bundle) and durably records local identity; the first `ava start` registers this
host and starts its selected root services.
Every runner process fetches current connection facts at Settings construction.
Use bare start thereafter; `ava init` refuses an initialized home. A later bundle
(after a write-generation rotation) goes to `ava cluster db-authority install-unit
<bundle>` with the unit stopped.

## Development in a worktree

A worktree is a checkout, not a deployment: it owns no cluster. Use its own real
`.venv`, never a symlink to another checkout's environment. Development invokes
`.venv/bin/ava` directly; the host's bare `ava` runs the cluster `$AVA_HOME` names.
Package acquisition and Git hooks are separate from starting a cluster.

Before a manual worktree dependency operation, clear inherited `VIRTUAL_ENV`
and run `scripts/host_ops/guard_editable_venv.py`. `scripts/setup-worktree.sh` runs this
guard and prepares development dependencies without creating a cluster.

```bash
cd ~/Ava/.worktrees/<name>
scripts/setup-worktree.sh
.venv/bin/pytest <selected test files>
```

**Which home a worktree reads.** With `AVA_HOME` unset the home is `~/.ava`; on a
development machine that also runs production, that is the production cluster. A
worktree's CLI may read it (`status`, `ls`, `get`), but a home that carries its own
`<home>/source` checkout is started, stopped and reconfigured only by that
checkout's CLI: every other verb refuses and names the CLI to run.

**The convention: a development tool that imports application code runs in a home of
its own.** Any script, test run or subagent that imports application code, run in a
development checkout, sets a temporary `AVA_HOME` first, once, at the top of the process
tree (or runs inside the Docker or Tart verification boundary). Left unset, the home is
`~/.ava`, and on a host that runs production that is production:

```bash
export AVA_HOME="$(mktemp -d)" AVA_CONFIG_FETCH=skip   # a throwaway home: no .env, no gateway fetch
```

Code enforces this in one place only, where the convention is sure to be broken by
nobody's choice: the git hooks. pre-commit and pre-push launch scripts on every commit,
so each hook script that reaches application code calls `dotenv_boot.enter_scratch_home()`
itself (only when run as a program; `scripts/tests/test_hooks_scratch_home.py` derives the
hook scripts from `.pre-commit-config.yaml`). The test harness sets a temporary home
before any import. Every other script is run by a person or an agent on purpose, and the
convention is its only guard.

Do not rebase or rewrite a checkout while a cluster runs from it: its root manifest
and loaded source must remain coherent. Source checkout editing is development work;
it is not a production update mechanism.

**Choose the check that proves the change:**

- Targeted tests need no running cluster. The test harness uses private native
  Postgres/Redis and isolated configuration. Never use a real cluster home for
  test imports or a production endpoint as a test fixture.
- A DB/SDK script needs a home that has been started and explicitly named with
  `AVA_HOME`. A throwaway home carries no `.env`: its `AVA_DB_URL` is the
  placeholder URL, so a connection fails fast with `PlaceholderDbUrlError`.
  Imports, lint scripts and codegen hooks work there as they do in CI.
- End-to-end agent/frontend behavior runs in CI's e2e job (a real gateway, agent
  subprocess and browser against throwaway Postgres and Redis).

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
configuration, then `PIP_INDEX_URL`, then pip configuration, then PyPI. A
profile exported into the shell (`scripts/mirrors/cn.env`) is real environment;
the helper also reads the unit's existing `mirror.env` without replacing real
environment values. Native command boot preserves this precedence
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
