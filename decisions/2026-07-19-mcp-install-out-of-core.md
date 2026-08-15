# MCP install mechanism + Discord out of core

Date: 2026-07-19

> **Note (2026-08-12, user ruling):** The Discord MCP is **deleted entirely
> and no longer maintained** — the user ruled it is not needed and Ava will
> not maintain it. The standalone repo
> `ava-mcp-discord` (created earlier the same
> day) is left in place but unmaintained; this machine's installed copy,
> registry entry and token are removed; every current-state reference is
> cleaned up. All Discord sections
> below are retained as history only.
>
> **Note (2026-08-13):** Discord was promoted to its own standalone repo
> (`ava-mcp-discord`, **private**; made public at
> the open-source day) — superseded the same day by the ruling above: the
> MCP is no longer maintained (the repo itself was left in place, unmaintained).
> `contrib/mcp/x` was likewise promoted the same day to its own repo
> `ava-mcp-x` — see the x externalization
> evaluation below.
>
> **Note (2026-07-25):** The `contrib/mcp/` packages described below were
> later removed from the repo. `ava mcp install` now supports auto-detection
> from `pyproject.toml` when a package lacks `.mcp.json`, so MCP servers are
> installed directly from their own repositories.
>
> **Note (2026-08-12):** The standalone repos the 2026-07-25 removal assumed
> never materialised, so installed packages stayed pinned to the historical
> ref they were installed from — `ava mcp upgrade` kept re-fetching that ref.
> `contrib/mcp/discord` was briefly restored to the repo (mcp SDK migrated to
> 2.x, `mcp>=2.0.0,<3`) so the package had a live source again — superseded
> the same day by the deletion ruling above. `contrib/mcp/x` is stdlib-only
> and unaffected (still pinned to its historical ref).

## Decision

MCP servers gain a **native vs installed** split mirroring skills. Native
servers ship in the repo (`<repo>/mcps/*/.mcp.json`, today just `chrome`).
Everything else is installed out-of-core via a unified command,
`ava mcp install <source>`, landing a self-contained package under
`$AVA_HOME/mcps/<name>/` and tracked in the install registry (`type="mcp"`).
Discord — previously a built-in with `discord-py` in core deps — is the first
server moved out, to `contrib/mcp/discord/`.

Trigger: Discord's MCP had been committed straight into core (`mcps/discord/` +
`discord-py>=2.3.0` in the core `pyproject.toml`). That is the wrong shape — an
integration one deployment wants should not tax every deployment's dependency
closure. Skills already solved the native-vs-installed problem; MCP should
mirror it rather than grow a parallel design.

## What landed

- **Installed layer.** `ava/_mcp_config.py:load_mcp_config` gains a fourth merge
  layer between plugin and machine: `$AVA_HOME/mcps/*/.mcp.json`
  (`_installed_mcp_paths`), gated by an enabled `type="mcp"` registry row
  (`shared/install_registry.py:installed_mcp_names`) so a stray dir never
  silently contributes a server. Precedence: built-in < plugin < installed <
  machine.
- **Command family.** `ava mcp install/uninstall/upgrade` (+ `ls` alias),
  reusing `cli/commands/_pkg_source.py` (git URL or local path) — the same
  source-acquire primitive `ava plugins install` now uses. `ava plugins install`
  signposts a bare `.mcp.json` package to `ava mcp install`.
- **No `uv run` for any MCP server we own.** MCP servers are spawned *per agent*,
  so launching through `uv run` hangs one resident uv wrapper process per agent
  per server — at the 100-300 agent density target, pure overhead and the largest
  remaining per-agent resident-memory item. Both our layers now declare a
  **relative** interpreter path (`.venv/bin/python`) and are spawned with a cwd
  that makes it resolve (`server_cwd`): an installed package from its own dir, a
  built-in (`chrome`) from the repo root. A relative argv[0] resolving against the
  *child's* cwd was verified empirically before being relied on. Absolute paths
  were rejected for built-ins: the repo root differs per machine/cluster, so it
  cannot be committed, and templating `.mcp.json` was already ruled out. Pinning
  cwd for built-ins additionally removes their former reliance on whatever cwd the
  agent process happened to have.
- **Dependency isolation.** An installed MCP is a self-contained uv project
  (own `pyproject.toml`). `ava mcp install` runs `uv sync` once at install time
  to build `$AVA_HOME/mcps/<name>/.venv`. Its `.mcp.json` command is a **relative**
  `.venv/bin/python -m <module>`; the daemon spawns it with
  `cwd = installed_mcp_dir(name)` (the package dir), so the relative path resolves
  to that package's isolated venv. Built-in / plugin / machine servers get
  `cwd = None` (the daemon's own cwd, the repo root) — unchanged, zero risk to
  chrome.
- **Discord out of core.** `mcps/discord/` → `contrib/mcp/discord/` (package
  renamed `mcps.discord` → `discord_mcp`); `discord-py` (and transitive
  `audioop-lts`) dropped from the core `pyproject.toml` / `uv.lock`. No core code
  or test imports it. `contrib/` is not vendored into the `ava` wheel and is not
  a uv workspace member, so its deps never re-enter core.

## Alternatives rejected

- **`uv run python -m <module>` as the installed command.** `uv run` is a
  *resident wrapper process* — for a long-lived MCP server it would hang one uv
  wrapper per server for the process's whole life. Banned repo-wide. `uv` is only
  invoked as `uv sync` at install time; the runtime command is a direct
  `.venv/bin/python`.
- **Baking an absolute `uv run --directory <abs>` into the installed
  `.mcp.json` at install time** (no spawn-path change). Rejected: it puts a
  machine-specific absolute path inside a generated file (breaks if `$AVA_HOME`
  moves) and is less faithful to skills' "location-independent installed
  content." The cwd approach keeps the on-disk `.mcp.json` portable and only adds
  an opt-in `cwd` kwarg that is `None` for everything pre-existing.
- **A second enable surface.** Runtime on/off stays the existing
  `mcp_enabled.json` overlay (`ava mcp enable/disable`), uniform across all four
  layers. The registry's `enabled` field is not consulted for `type="mcp"` — a
  registry row means "installed," not "enabled."
- **Discord in a separate git repo now.** Kept in-repo under `contrib/` so the
  mechanism is testable end-to-end in CI (local-path install, no network / second
  repo). *Executed 2026-08-13:* Discord moved to its own repo
  (`ava-mcp-discord`), a move + a source-URL change exactly as anticipated.

## Not covered (follow-ups)

- **Discord token delivery + the prod cutover it forces.** The token channel is
  the `.mcp.json` `env` dict, and only that: the stdio transport spawns the
  server with `{**get_default_environment(), **server.env}`, and
  `get_default_environment()` inherits just `HOME`/`LOGNAME`/`PATH`/`SHELL`/
  `TERM`/`USER` on POSIX. Before this change the token was supplied by a
  **machine-layer** entry in `$AVA_HOME/mcp.json` — a full `discord` override
  (highest precedence) whose `env` carried `DISCORD_TOKEN` and whose command was
  `uv run python -m mcps.discord`. That is a working, actively used integration,
  and it is why the built-in `.mcp.json` carrying no token was never a
  functional gap.

  Consequence: moving Discord out of core invalidates that machine entry's
  command (`mcps.discord` and `discord-py` leave core), and because the machine
  layer shadows the installed layer, installing the package alone does not take
  over.

  **Resolved here:** `ava mcp install --env K=V` injects machine-local values
  into the landed copy's `env` (`$AVA_HOME/mcps/<name>/.mcp.json`, outside the
  repo, so no secret enters git), and `ava mcp upgrade` carries that env across a
  re-land so re-fetching never drops a token. Secrets-in-`env` was already the
  established pattern for machine-configured servers (the `x` server uses it
  too); this gives an installed package the same channel. A package's committed
  `.mcp.json` ships no secrets.

  **Remaining (ops, per machine running Discord):** the cutover itself —
  `ava mcp install contrib/mcp/discord --env DISCORD_TOKEN=<token>` followed by
  `ava mcp remove discord` to drop the stale machine entry. Documented in
  `contrib/mcp/discord/README.md` and the runbook. Not exercised by CI.
- The `scope` axis (per-machine MCP daemon) and per-entry `{source, scope}`
  schema in [`../future/infra/mcp-scope-and-bundling.md`](../future/infra/mcp-scope-and-bundling.md)
  remain open; this change closes that doc's "registry-package source" item.

## x externalization evaluation (2026-08-13)

`contrib/mcp/x` is a stdlib-only stdio bridge to X's official hosted MCP
endpoint (`https://api.x.com/mcp`); the tools live server-side at X. Its
install-registry entry pins `path=contrib/mcp/x` at ref `87646baa`, a
historical Ava commit whose `contrib/mcp/` content was deleted from `main`
(2026-07-25, #861) — so `ava mcp upgrade x` re-fetches a frozen snapshot that
can never receive updates.

**Verdict: externalize — executed 2026-08-13.** Same move as Discord: new
private repo `ava-mcp-x` (repo root = package),
installed-source swapped to that URL (`path`/`ref` dropped), `ava mcp upgrade
x` verified end-to-end (24 official tools listed through the bridge). The
evaluation above judged it non-urgent (stdlib-only, thin, stable, frozen
source idempotent) — correct, and it made the move a 15-minute mechanical
task once the Discord pattern existed.
