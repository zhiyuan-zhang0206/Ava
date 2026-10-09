---
type: doc
title: MCP Configuration Sources
description: "The four `.mcp.json` layers merged for an MCP server (builtin / plugin / installed / machine), the `~/.ava/mcp_enabled.json` enable toggle, `requires` preconditions, and remote-server auth modes (headers / OAuth 2.1)."
tags: []
---

# MCP Configuration Sources

## The four layers (merged, later overrides earlier)
1. **Builtin**: `<repo>/ava_builtins/mcps/<name>/.mcp.json` — shipped with code (currently only `ava_builtins/mcps/chrome/.mcp.json`)
2. **Plugin**: a plugin's `.mcp.json`
3. **Installed**: `$AVA_HOME/mcps/<name>/.mcp.json` — self-contained packages via `ava mcp install` from git URLs or local paths, gated by the install registry (`base/packages/extensions/install_registry.py`, `type="mcp"`)
4. **Machine**: user-custom `$AVA_HOME/mcp.json` — highest priority

Enable control: `~/.ava/mcp_enabled.json` — `ava mcp enable/disable` per-machine toggle across all four layers.

## `requires` preconditions
`.mcp.json` entries may declare an optional `requires` precondition map.
Missing, `null` or an empty object means no requirements. Otherwise the only
keys are `display` and `unix_socket`, and each value must be a JSON boolean.
Unknown keys, non-object maps and non-boolean values raise `MCPError` through
the shared `ava/mcp_config.py:validate_requirements` boundary before host probes.

`assert_requirements()` blocks connection when a requested host capability is
unavailable. The inventory panel's `server_capability()` returns a false verdict
for that expected absence, but propagates invalid declarations as operation
failures. Enabling an invalid declaration cannot write either inventory overlay.
`ava mcp list` validates all declarations before printing inventory, and
`ava mcp enable` validates the named declaration before writing its overlay.
These CLI checks do not probe host capabilities, and an override for a not-yet
defined server remains supported. Disabling a server does not validate its
requirements, so a broken declaration can still be explicitly turned off.

## Remote servers
An entry may declare `url` instead of `command` — a Streamable HTTP endpoint dialed directly (no child process). Two auth modes, mutually exclusive (fail-fast, `ava/mcp_config.py:server_url`):
- `headers` — static auth (`{"Authorization": "Bearer …"}` / `{"x-api-key": …}`) for API-key servers (Firecrawl / Exa)
- `"oauth": true` — full OAuth 2.1 authorization-code + PKCE flow (`ava/mcps/_oauth.py`): on 401 the provider discovers the authorization server (RFC 8414/9728), dynamically registers this client (RFC 7591), opens the user's browser at the authorization URL, receives the code on a loopback callback (`127.0.0.1:8931`), exchanges it for tokens and stores them at `$AVA_HOME/mcp_oauth/<server>.json` (0600); refresh tokens renew transparently and are reused across daemon restarts — authorization happens once per server, serialized per server so concurrent connections share one flow

The 2026-07-28 protocol revision is stateless over HTTP, so one endpoint serves every agent connection; `url` and `command` are mutually exclusive (fail-fast).
