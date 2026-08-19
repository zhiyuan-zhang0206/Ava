---
type: doc
title: Package Commands (plugins / skill / mcp)
description: The operator surface for installing external skills, Claude Code plugins, and MCP servers on one machine — `cli/commands/plugins.py`, `skill.py`, `mcp.py` — plus `ava mcp serve` (`cli/mcp_server.py`), which points the other way and exposes this cluster AS an MCP server. Installs are always local to the host the command runs on.
tags:
- cli
- tool
- packaging
---

# Package Commands (plugins / skill / mcp)

## What it is

The CLI surface over the [[install_registry.ava.okf.md|install registry]]. Every
verb here acts on **the machine it runs on** — to install elsewhere, spawn an
agent there (`ava.agents.spawn(machine=...)`).

There is deliberately **no install form in the UI**. The Skills / Plugins / MCP
sections of `/control` take a natural-language request; `POST
/api/packages/draft` spawns an `ava-package-installer` agent that owns the whole
lifecycle — find candidates, confirm before installing anything that runs code,
install, verify with a test agent, judge, report. No install-by-URL field
exists, because a user generally cannot tell a good package from a bad one,
which is the reason an agent is in the loop at all.

## Verbs

```bash
ava plugins install <git-url> [--path <subdir>] [--ref <tag|commit|branch>] [--accept-risk]
ava plugins installed | upgrade <name> | uninstall <name>
ava plugins inspect [<name>]                  # the extension-surface catalog (read-only)
ava skill install <src> [--path <subdir>] [--ref <ref>] [--accept-risk]
ava skill enable <name> | disable <name>      # toggle a tracked package in the scanner
ava skill register <name> [--accept-risk]     # adopt a hand-copied dir in $AVA_HOME/skills/
ava skill scan <name-or-path>                 # re-run the supply-chain scan (exit 2 on criticals)
ava skill trust <name> [--revoke]             # record that a human read it (trust=reviewed)
```

## `ava plugins inspect` — the catalog

The only verb here that installs, removes, or rewrites nothing. Bare, it prints the framework's
extension surfaces — each one's `register_*` signature rendered from the live
object, the contract a contributor must satisfy, and who is using it — then one
line per installed plugin. With a name, it prints that plugin's every
registration as a fact (surface, identifier, the class or function behind it) and
diffs those against its `ava-plugin.json` declarations when it ships one.

It answers two questions nothing else could: *what can a plugin extend* (for an
agent writing one, which otherwise means reading framework source) and *what is
this machine's agent actually composed of*. Both halves read the attribution
ledger (`shared/plugin_contributions.py`) that every `register_*` entry point
writes, so nothing here is transcribed. [[okf/plugins/plugins.ava.okf.md|Detail]].
Building the catalog means importing the plugins, so it can only report what was
REGISTERED; how often each row actually FIRED is a `note` line pointing at the
`plugin_activation` events [[okf/plugins/activation-telemetry.ava.okf.md]].

Reading registrations means loading the plugins, which means importing them — so
a **disabled** plugin is listed with its enable-state and nothing else, rather
than with a guess read off its source, and the load carries an agent boot's own
side effects (a plugin's missing config image is written from defaults, exactly
as a boot would write it).

## The install gate

Every ingestion path above runs `shared/skill_scan.py` over the package before
the first byte is copied, and **refuses on a critical finding** — the load dir is
left untouched and the report names file, line and matched text. `--accept-risk`
is the override; it installs and records the waived rule ids, and it never
promotes the package's trust tier. `register` is gated too, because `cp -r` into
the load dir otherwise routes around every check `install` makes.

Nothing installs at a tier better than `unreviewed`. `ava skill trust` is a
separate verb precisely so "a person read this" cannot be asserted by an install
that merely matched no rule. [[install_registry.ava.okf.md|The tiers]].

```bash
ava mcp install <source> [--path <subdir>] [--ref <ref>] [--env K=V]...
ava mcp uninstall <name> | upgrade <name>     # upgrade carries the recorded env forward
ava mcp add <name> --json '{"command": "npx", "args": ["-y", "server-x"]}'
ava mcp add <name> --command <cmd> [--arg=A]... [--env K=V]...
ava mcp list (ls) | remove <name> | enable <name> | disable <name>
```

`--arg` values starting with `-` need the `--arg=-y` form, or use `--json`.

```bash
ava mcp serve                                 # run THIS cluster as an MCP server (stdio)
```

## The two directions of `ava mcp`

Every verb above configures a server an Ava **agent connects out to**. `serve`
is the inverse — an MCP server whose tools are this cluster's own control plane,
so an external agent (Claude Code, Codex) drives the fleet. It shares nothing
with the machinery below: no registry, no merge layers, no venv.
[[mcp_server.ava.okf.md|Detail]].

## MCP merge layers

A server reaches an agent one of three ways, mirroring the skills native /
installed split, merged in this order — later shadows earlier:

1. **built-in** — ships in the repo (`<repo>/ava_builtins/mcps/*`; today just `chrome`)
2. **installed** — `ava mcp install` lands a self-contained package under
   `$AVA_HOME/mcps/<name>/` with its own `.venv`, tracked as `type="mcp"`
3. **machine** — `ava mcp add` writes an ad-hoc entry straight into
   `$AVA_HOME/mcp.json`

Because the machine layer wins, installing a package does **not** take over from
a stale machine entry of the same name — cut over explicitly: `ava mcp install
<source> --env <KEY>=<value>`, then `ava mcp remove <name>`, then confirm the
entry shows `[installed]` in `ava mcp list`.

A disabled server (`mcp_enabled.json`, per-host overlay) stays defined but is
dropped from the set the agent connects; `--include-disabled` callers still see
it. Plugin and MCP enable can also be toggled cluster-wide from the Control
page (`GET/PUT /api/inventory`).

MCP is a cross-vendor standard, so a `mcpServers` object pasted from any
vendor's README works as-is.

## Two invariants worth knowing

- **Launch form**: both server sets we own declare a *relative* interpreter
  (`.venv/bin/python`) and are spawned with a cwd that makes it resolve
  (`ava/_mcp_config.py:server_cwd`) — an installed package from its own dir, a
  built-in from the repo root. Never `uv run`: that leaves a resident wrapper
  process per agent per server, pure overhead at the target agent density.
  Plugin / machine entries keep cwd `None`; their command line is the third
  party's own.
- **Secrets**: a spawned server's environment is `get_default_environment()`
  (only `HOME` / `LOGNAME` / `PATH` / `SHELL` / `TERM` / `USER` on POSIX) merged
  with its `.mcp.json` `env` — so **that `env` dict is the only channel** by
  which a token reaches a server. A value in `$AVA_HOME/.env` does not
  propagate. For a machine entry use `ava mcp add --env`; for an installed
  package (whose committed `.mcp.json` must ship no secrets) use `ava mcp
  install --env K=V`, which writes it into the landed copy.

## Key Dependencies

- [[install_registry.ava.okf.md]] — the registry these verbs write, and the scanner gate
- [[okf/mcps/mcps.ava.okf.md|MCP integration]] — the MCP domain node
- [[plugins.ava.okf.md]] — the plugin system
