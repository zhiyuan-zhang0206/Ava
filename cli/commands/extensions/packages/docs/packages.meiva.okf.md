---
type: doc
title: Package Commands (plugins / skill / mcp / packages)
description: Operator commands for external skills, plugins, MCP servers and package updates.
tags:
- cli
- tool
- packaging
---

# Package Commands (plugins / skill / mcp)

## What it is

The CLI surface over the [[install_registry.meiva.okf.md|install registry]]. Skill
installation publishes to the cluster registry before writing its local copy;
another home can materialize that content without seeing the original source.
The [two-home test](two-home-chain.meiva.okf.md) explains the
shared-database proof and its concurrency and credential limitations.

There is deliberately **no install form in the UI**. The Skills / Plugins / MCP
sections of `/control` take a natural-language request; `POST
/api/keyed/v1/packages/draft` spawns an `meiva-guide.packages.install` agent that owns the whole
lifecycle — find candidates, confirm before installing code, install, verify
with a test agent, judge, report. No install-by-URL field exists, because a
user generally cannot tell a good package from a bad one, which is why an agent
is in the loop.

`parsers/` owns settings-free builders and handlers; `cli.parsers` composes
them. `external_skills.py` delegates filesystem, ledger and recovery to
`external_skill_host/`.

## Verbs

```bash
meiva plugins install <git-url> [--path <subdir>] [--ref <tag|commit|branch>] [--accept-risk]
meiva plugins installed | upgrade <name> | uninstall <name>
meiva plugins inspect [<name>]                  # the extension-surface catalog (read-only)
meiva plugins verify  # load enabled plugins as agent boot does; exit 1 on failure
meiva skill install <src> [--path <subdir>] [--ref <ref>] [--accept-risk]
meiva skill enable <name> | disable <name>      # toggle a tracked package in the scanner
meiva skill register <name> [--accept-risk]     # adopt a hand-copied dir in $MEIVA_HOME/skills/
meiva skill scan <name-or-path>                 # re-run the supply-chain scan (exit 2 on criticals)
meiva skill trust <name> [--revoke]             # record that a human read it (trust=reviewed)
```

## `meiva plugins verify` — does every enabled plugin load

Runs the fail-soft loader (`meiva.sdk_surface.extensions.load_extensions`) and exits 1 when it skipped a plugin:
`RESULT enabled=N failed=M rc=R`, then `RED plugin=<name> <error> (at <file>:<line>)`. Read-only (no hook
runs, nothing written, no failure telemetry); covers the agent-boot load path, not a plugin's `services.py`
or `provider.py`. `python -m cli.fleet_update up` runs it on every host.

## `meiva plugins inspect` — the catalog

The only verb here that installs, removes, or rewrites nothing. Bare, it prints the framework's
extension surfaces — each one's `register_*` signature rendered from the live
object, the contract a contributor must satisfy, and who is using it — then one
line per installed plugin. With a name, it prints that plugin's every
registration as a fact (surface, identifier, the class or function behind it) and
diffs those against its `meiva-plugin.json` declarations.

It answers two questions nothing else could: *what can a plugin extend* (for an
agent writing one, which otherwise means reading framework source) and *what is
this machine's agent actually composed of*. Both halves read the plugins' declarations
(`PluginContributions.as_records`, `base/packages/plugins/extensions.py`), so nothing
here is copied. [[docs/plugins/plugins.meiva.okf.md|Detail]].
Building the catalog means importing the plugins, so it can only report what was
DECLARED; how often each row actually FIRED is a `note` line pointing at the
`plugin_activation` events [[docs/plugins/activation-telemetry.meiva.okf.md]].

Reading registrations means loading the plugins, which means importing them — so
a **disabled** plugin is listed with its enable-state and nothing else, rather
than with a guess read off its source, and the load carries an agent boot's own
side effects (a plugin's missing config image is written from defaults, exactly
as a boot would write it).

## `meiva packages`

The per-machine content-update plane over registry schema v2 — `status` /
`refresh` / `rollback` / `policy` (verb details in the child node below); the
full per-package state lives in `status --json`:
[[update-policy.meiva.okf.md|Update policy & channels]] ·
[[install_registry.meiva.okf.md|Schema]].

## The install gate

Every ingestion path above runs `base/packages/skills/scan.py` over the package before
the first byte is copied, and **refuses on a critical finding** — the load dir is
left untouched and the report names file, line and matched text. `--accept-risk`
is the override; it installs and records the waived rule ids, and it never
promotes the package's trust tier. `register` is gated too, because `cp -r` into
the load dir otherwise routes around every check `install` makes.

Nothing installs at a tier better than `unreviewed`. `meiva skill trust` is a
separate verb precisely so "a person read this" cannot be asserted by an install
that merely matched no rule. [[install_registry.meiva.okf.md|The tiers]].

```bash
meiva mcp install <source> [--path <subdir>] [--ref <ref>] [--env K=V]...
meiva mcp uninstall <name> | upgrade <name>     # upgrade carries the recorded env forward
meiva mcp add <name> --json '{"command": "npx", "args": ["-y", "server-x"]}'
meiva mcp add <name> --command <cmd> [--arg=A]... [--env K=V]...
meiva mcp list (ls) | remove <name> | enable <name> | disable <name>
```

`--arg` values starting with `-` need the `--arg=-y` form, or use `--json`.


## MCP merge layers

A server reaches an agent one of three ways, mirroring the skills native /
installed split, merged in this order — later shadows earlier:

1. **built-in** — ships in the repo (`<repo>/meiva_builtins/mcps/*`; today just `chrome`)
2. **installed** — `meiva mcp install` lands a self-contained package under
   `$MEIVA_HOME/mcps/<name>/` with its own `.venv`, tracked as `type="mcp"`
3. **machine** — `meiva mcp add` writes an ad-hoc entry straight into
   `$MEIVA_HOME/mcp.json`

Because the machine layer wins, installing a package does **not** take over from
a stale machine entry of the same name — cut over explicitly: `meiva mcp install
<source> --env <KEY>=<value>`, then `meiva mcp remove <name>`, then confirm the
entry shows `[installed]` in `meiva mcp list`.

A disabled server (`mcp_enabled.json`, per-host overlay) stays defined but is
dropped from the set the agent connects; `--include-disabled` callers still see
it. Plugin and MCP enable can also be toggled cluster-wide from the Control
page (`GET/PUT /api/inventory`).

MCP is a cross-vendor standard, so a `mcpServers` object pasted from any
vendor's README works as-is.

## Two invariants worth knowing

- **Launch form**: both server sets we own declare a *relative* interpreter
  (`.venv/bin/python`) and are spawned with a cwd that makes it resolve
  (`meiva/mcps/config.py:server_cwd`) — an installed package from its own dir, a
  built-in from the repo root. Never `uv run`: that leaves a resident wrapper
  process per agent per server, pure overhead at the target agent density.
  Plugin / machine entries keep cwd `None`; their command line is the third
  party's own.
- **Secrets**: a spawned server's environment is `get_default_environment()`
  (only `HOME` / `LOGNAME` / `PATH` / `SHELL` / `TERM` / `USER` on POSIX) merged
  with its `.mcp.json` `env` — so **that `env` dict is the only channel** by
  which a token reaches a server. A value in `$MEIVA_HOME/.env` does not
  propagate. For a machine entry use `meiva mcp add --env`; for an installed
  package (whose committed `.mcp.json` must ship no secrets) use `meiva mcp
  install --env K=V`, which writes it into the landed copy.

## Key Dependencies

- [[install_registry.meiva.okf.md]] — the registry these verbs write, and the scanner gate
- [[docs/mcps/mcps.meiva.okf.md|MCP integration]] — the MCP domain node
- [[docs/plugins/plugins]] — the plugin system
