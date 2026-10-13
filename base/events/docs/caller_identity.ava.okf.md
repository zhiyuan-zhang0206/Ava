---
type: doc
title: "Asserted External Caller Identity"
description: "How an external agent declares itself as the sender of a message, and how the target and the UI read the declaration."
tags:
- base
- identity
---

# Asserted external caller identity

An agent outside Ava (Claude Code, Codex, an MCP client) that sends a message to
an Ava agent chooses one of two attributions through the `source` field:

| `source` | The target reads it as | When to use it |
|---|---|---|
| `user` | an ordinary user message, no sender label | the external tool speaks *for* the user (the user drives Ava through Claude Code) |
| `external_agent:<subject>[:<instance>]` | "External agent (subject / instance; asserted provenance)" | the external tool collaborates *beside* the user and the user stays a separate party |

`CallerIdentity` is the bounded value object behind the second form. `subject`
and `instance` are 1 to 20 characters of `[a-zA-Z0-9_.-]` starting alphanumeric;
unknown fields and malformed identifiers are rejected at every boundary
(`validate_source`: the CLI parser, the gateway message/system-note/lifecycle
schemas, the spawn prompt source, the direct inbound writers). No secrets or
human identity belong in an instance identifier. `external_agent:codex:run-42`
is an example; a raw 36-character UUID is not accepted.
The shared caller parser also recognizes the retired `unknown:` namespace and
rejects it before persistence or lifecycle dispatch; it never falls back to
`user`. Other lifecycle audit labels remain opaque.

The declaration is self-asserted. It is not an authenticated principal,
capability, transport, request ID or Ava agent identity, and it grants no
authority. `source=user` is equally an assertion: whoever holds the gateway
credential can already send as the user, and the server-recorded credential and
transport facts (`inbound-provenance`) are the audit evidence, not the label.
The envelope never renders a declared external agent as the user, system or an
Ava agent, and an Ava agent is not told a declared sender is verified.

Chat and lifecycle inbound writes persist the parsed object in the existing
JSONB `payload.caller_identity` sidecar. Audit events carry the same reserved
structured attribute. Conflicting or malformed reserved metadata is rejected
before writing and other payload fields are preserved; chat reconciliation
normalizes the sidecar the same way as insertion, so retries compare the same
immutable payload. Other sources receive no inferred identity.

## Where it is read

- **Target agent**: `base.agents.messages.envelope.inbound_head` prefixes the
  message with `External agent (<subject>[ / <instance>]; asserted provenance)`
  and the timestamp (when message timestamps are on). `user` keeps the bare
  timestamp header.
- **Context breakdown**: an external-agent message is bucketed with peer agent
  messages (`agent_messages`); `user` is `user_input`.
- **Timeline / UI**: the inbound row carries `source`; the web timeline shows a
  declared external agent as its own sender kind, not as the human or system.
- **MCP**: `send_message` attributes to the user by default. With
  `as_external_agent=true` the server derives
  `external_agent:mcp:<client-id>` from the authenticated client row; the caller
  cannot pass `source` or `instance`.

Never wrap external provenance in `system:*` or `agent:*`. A caller field
accepted by an HTTP schema but discarded before persistence is not structured
audit storage. Shared cluster credentials cannot prove the caller label:
authorization and idempotency scopes derive from actual server-bound
credentials, not this object.
