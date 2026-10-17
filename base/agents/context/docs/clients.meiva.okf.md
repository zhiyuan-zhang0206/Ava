---
type: doc
title: "Context clients"
description: "The connections one context owns: built on first use, closed together, and derived rather than swapped when a caller routes the gateway client."
tags:
- base
- agents
- context
---

# Context clients

An exec child (or any process that runs as an agent) depends on a SQL connection, a Redis client
and a gateway HTTP client, plus whatever its SDK layer adds (the MCP clients). None of it persists
and none of it is a graph-state fact, so `MeivaContext.clients` holds it as a `ClientSet`:

- nothing is built at construction: importing psycopg / redis / httpx waits for the first call that
  needs it, which keeps a child that never touches the database off that stack;
- a set built in a child resolves credentials from the child's own settings and environment; the
  description a host sends (`MeivaContext.describe`) carries endpoints, never a secret; its
  composition root supplies lazy builders and the endpoint resolver;
- `close()` releases everything the set built, the gateway client included: a set's builders are
  its only source of clients. Constructor-specific factory builders remain configured after close,
  so a later use builds fresh clients;
- a caller that needs a different gateway client derives a sibling with
  `ClientSet.with_gateway(builder)` instead of rewriting a live set: the sibling keeps the endpoint
  and the other builders, builds and closes the clients its builders return (the gateway client
  among them), and carries no event-pipeline factory (a process has one writer).

A failure to build a client raises at the call that needed it; nothing here swallows one.

## One owner per set

`meiva.gateway_client.use_client` routes the SDK's gateway calls through a client the caller hands
in — a FastAPI `TestClient` for an in-process gateway, or any `httpx.Client` — by binding a derived
context for the block and restoring the previous binding afterwards. A caller on another thread
keeps the client its own set answers. `use_client` never closes the derived sibling, so the client
it hands in stays the caller's; closing a sibling would close whatever its builder returned.
Because the sibling builds its gateway client from the shared endpoint, a parent set without a
resolvable gateway endpoint cannot derive one; the block's builder still receives that endpoint and
ignores it.

A ClientSet closes every client it builds, the gateway client included; its constructors' builders are the only way a client enters it. A with_gateway sibling shares the endpoint and the other builders and builds no event pipeline; it closes the gateway client its builder returns, so use_client — which hands in a caller-owned client — never closes the sibling, and the caller keeps ownership of that client.

Parent: [[../../docs/agents.meiva.okf.md|agent contracts]].
