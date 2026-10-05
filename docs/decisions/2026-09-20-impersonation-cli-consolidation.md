# An attested `ava impersonate send` — impersonation scope is the lease, not the source

## Context

The impersonation subsystem lets a trusted external process (Codex, Claude
Code) take over an Ava agent under a lease and act with its identity. One
operation the SDK attachment already supported had no CLI form: sending a
message to another agent as the borrowed identity
(`ava.agents.send_message` inside `ava.external.attach`).

An earlier cut of this work (v1, same day) over-collected: it also moved the
`user` and `agent:N` sources off `ava agents send` into an `impersonate send`
declared form. The user corrected the scope on 2026-09-20 02:39 (translated):
generic source sends are general behavior — a Codex debugging session sending
as `user`, or a Codex messaging the agent that delegated to it under its own
custom source — and have nothing to do with impersonation. Only one scenario
is impersonation: a session that takes over an agent's identity through a
lease.

The corrected boundary, stated once (task #4102):

- **In** — operations performed under a recorded takeover: the session id plus
  caller presence in the controller's process tree attest the borrowed
  identity.
- **Out** — every general provenance declaration on `ava agents send`
  (`user` / `agent:N` / `external_agent:*` / `unknown:*`). That source face is
  untouched: no migration, no narrowing, no rejection pointers; callers of
  the general paths are not affected.

## Decision

Add one verb — `ava impersonate send <session_id> --agent <agent_id> --to
<target_agent_id> --content '<text>'` — the attested CLI form of speaking as
the leased identity to another agent. Authorization is the session's standard
caller-presence rule (`private_id` + `require_active`); the delivered source
is `agent:<agent_id>`, the borrowed identity, identical to what the SDK
attachment stamps for `ava.agents.send_message`. `--content -` reads stdin;
the command answers `{status, to, source}`.

There is no declared form and no `--source` parameter on the verb: sending
under an identity that is not one's own is exactly what the lease attests.
The transport (idempotency keying, deferred-delivery outbox, tail rider) is
shared with `ava agents send` through one extracted helper — a behavior-
neutral refactor; the `agents send` surface itself is unchanged.

The same ruling batch keeps the impersonate tree's parameters explicit (no
defaults): `request --ttl` / `--batch-window` and `renew --ttl` are required,
the request's relay-shape rules (codex needs `--thread-id`, a remote must be
`unix://` or `ws://`, claude drops both) are validated at the CLI boundary,
and `--name` / `--as` reject blanks. Four display/internal defaults stay,
each approved with its reason: `list`/`inbox --limit` 100 (presentation,
#3696 inventory), `inbox --wait` 0 (the unique "return immediately" value),
`say --phase commentary` (the routine phase), `relay --debounce` 0.5
(internal machinery). The takeover launch message — the one generated call
site — now spells out the `--ttl 3600` / `--batch-window 0` it previously
inherited from the parser defaults, and a launch test pins them.

## Alternatives rejected

- **The v1 declared form (`impersonate send --source user`) plus the
  `agents send` narrowing.** Rejected by the user's 02:39 correction: they
  fold general provenance flows into the impersonation subtree and change a
  wire-facing surface for a scenario that is not impersonation. Deleted
  entirely — no aliases, no rejection pointers.
- **Declared agent form (`--source agent:N` without a lease).** The lease
  plus caller attestation is the subsystem's authority model; a declared
  form would bypass it for the one operation that most needs it.
- **Environment fallback** (`AVA_CALLER_IDENTITY` filling a missing source).
  Rejected earlier in the same line of work (#2942: explicit parameters
  only); identity belongs in explicit flags, never in ambient state.
- **Configurable defaults for `--ttl` / `--batch-window`.** Each call states
  its recovery deadline and merge window; a config default would recreate the
  implicit path the explicit-parameter ruling removes.

## Consequences

- One new lease-scoped entry point; `ava agents send` and every source
  consumer are unaffected (the transport-helper extraction is
  behavior-neutral).
- Docs synced: both impersonation convention pages, the impersonator guide
  and ava-guide skills, `shared/impersonation.ava.okf.md`,
  `ava/external.ava.okf.md`, `cli/commands/commands.ava.okf.md`, CHANGELOG.
- The 02:39 boundary — general source flows vs lease-scoped impersonation —
  is the durable part: future impersonation work sorts by scenario (is a
  recorded takeover acting?), not by whether a command mentions a source.
  The v1 over-collection is recorded here so the boundary is not re-broken
  from scratch.
