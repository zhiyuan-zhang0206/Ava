---
type: doc
title: MCP endpoint model capability failures
description: Which MCP tools keep working without the model catalog, the audit-failure rule, and the lazy-SDK-types invariant of the disabled endpoint.
tags: [gateway, mcp]
---

# MCP endpoint model capability failures

Registration of all seven tools and MCP authentication do not read the catalog.
`list_agents` and `get_agent` preserve database facts and return null vision
metadata when it is unavailable, using the same projection as HTTP reads.
`spawn_agent_guarded_v1` still requires the complete catalog and translates its
unavailable state into a safe tool error. `cluster_status`,
`send_message`, `get_messages` and `terminate_agent` retain their independent
requirements and remain callable. Scope checks still precede mutation.

An audit failure after a successful tool is logged rather than changing that
success into a tool error: retrying an already committed mutation could repeat
its effect. Client labels are asserted display data; the authenticated credential
binding supplies provenance. The disabled endpoint does not construct an MCP server or session manager. The manager's public app builder initializes its session manager;
the gateway uses the resulting bare ASGI transport under its own authentication.

A disabled endpoint answers 404 without constructing a server or session manager; the enabled manager exposes the seven guarded control tools. The builders import their concrete SDK return classes locally, so those type names are unavailable in the module signature namespace and their return annotations remain omitted. This does not imply that the SDK module itself is unloaded.

## Relationship to Other Nodes

- [[gateway/mcp_server/docs/mcp-endpoint/mcp-endpoint.meiva.okf.md|Gateway /mcp endpoint]] — the node these failure behaviors belong to.
