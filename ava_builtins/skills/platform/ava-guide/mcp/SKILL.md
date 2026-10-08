---
name: mcp
description: "Manages Ava MCP connections and wrappers. Use when adding, configuring, inspecting, removing, or troubleshooting an MCP server."
---

# MCP — External Tool Integration

External tool servers (a browser, a language server, a vendor API) are wired in
as **MCP servers**. Once a server is configured on a machine, you call its tools
from code as `ava.mcps.<server>.<tool>(**args)` — `help(ava.mcps)` lists the
configured servers, `help(ava.mcps.<server>)` lists a server's tools.

Configuration is **machine-level**: each box has its own server set (the merged
view of the machine config + any plugin-bundled `.mcp.json`).

This skill is the CLI reference. To go from "I want to reach tool X" to a
server that is installed and proven to work, read
`ava.help(ava.skills.ava_guide.packages.install)` — it covers the major registries
and providers, approval of a specific process or remote connection, and a real
capability check. Its [source map](../packages/install/references/sources.md)
includes the official registry, hosted services, and community marketplaces.

## Add

Paste a server spec straight from a vendor README:

```bash
ava mcp add <name> --json '{"command": "npx", "args": ["-y", "some-mcp-server"]}'
```

Or build it from flags instead of JSON:

```bash
ava mcp add <name> --command npx --arg=-y --arg some-mcp-server --env KEY=VALUE
```

`--arg` takes one value; a value that starts with a dash needs the
`--arg=<value>` spelling (`--arg -y` reads as a new option).

`add` replaces an existing server of the same name. A newly added server
connects the next time you call one of its tools — no restart.

### Remote servers

Ava also accepts a remote **Streamable HTTP** server object. Copy the endpoint
from the vendor's current documentation, then add it with `--json`:

```bash
ava mcp add vendor --json '{"url":"https://vendor.example/mcp"}'
```

`url` and `command` are mutually exclusive. A remote entry can use static
`headers` or `"oauth": true`, not both. Follow the vendor's auth instructions
and the supported browser authorization flow; never put credentials into a
preset or a source repository. Check the running deployment's support and make
a real read-only tool call to verify access. An SSE-only endpoint is not a
Streamable HTTP endpoint.

## List / Remove / Enable / Disable

```bash
ava mcp list             # the merged MCP server set on this machine
ava mcp remove <name>    # remove a machine-config server
ava mcp enable <name>    # toggle a server on this machine
ava mcp disable <name>
```

`remove` deletes a server you added via machine config; `disable` keeps it but
stops it loading. A plugin-bundled server comes in with its plugin (see
[packages](../packages/SKILL.md)) rather than `ava mcp add`.

## When to Fix Behavior in a Wrapper Instead

If a first-party MCP server (one Ava ships with) has a behavior bug, the fix is
a thin wrapper MCP server that passes the upstream through and intercepts only
the broken call — not a special-case in the generic MCP call path. Isolate the
fix so it does not pollute the shared channel.
