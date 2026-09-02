---
type: doc
title: "External Caller Profiles"
description: "Explicit subprocess provenance for external tools, separate from authenticated credentials and gated by consumer compatibility."
tags:
- shared
- identity
---

# External caller profiles

`AVA_CALLER_IDENTITY` is an opt-in JSON handoff inherited by a coding tool's
subprocess tree. `shared.external_caller` validates it as an external-only
`CallerIdentity`; malformed profiles fail without falling back to human, system,
or inherited Ava agent identity. The environment value is not a credential.

Example profile: `{"kind":"external_agent","subject":"codex","instance":"run-42"}`.
Claude Code uses subject `claude_code`. Set the profile once in the external
tool's launch environment, not separately for every generated command. Do not
place secrets, user names, paths, or prompts in the bounded instance field.

CLI send can use that profile without repeating `--source`; an explicitly
conflicting source is rejected before network access. Restart, resurrect,
terminate and kill accept an explicit `--source` and forward the profile when
configured. SDK source selection prefers real hosted-turn context, then an
explicit external profile, then its established legacy actor. An external shell
cannot become its Ava parent merely by inheriting `AVA_AGENT_ID`.

## Compatibility and remaining work

This is opt-in producer plumbing, not activation. Servers without target-runtime
v1 support reject the source before delivery. Never retry with user/system/agent
as a fallback. Existing non-opted-in lifecycle CLI behavior remains unchanged
for the staged upgrade; its legacy server default is still a known attribution
gap, not a claim that an undeclared caller is human. MCP source defaults and
wrapper activation also remain pending the generation-bound write gate. No new
authorization scope, token store, or idempotency namespace is defined here.
