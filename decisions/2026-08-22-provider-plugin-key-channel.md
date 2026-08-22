# Provider-plugin keys use the existing `.env` and bootstrap channel

## Context

`ProviderBinding.key_env` gives a removable provider plugin an API-key name,
but deliberately does not make that key a core Settings field. Core provider
keys receive cluster scope, bootstrap distribution, single-box process delivery,
and worktree seeding through Settings metadata; plugin keys need the equivalent
path without widening core configuration.

The cluster `.env` file and authenticated `/api/bootstrap` are already the
secret-delivery channel. Plugin configuration images are plaintext disk JSON,
have no scope axis, and have no bootstrap distribution. The process environment
is also deliberately assembled with positive allowlists, so forwarding every
ambient variable to an agent would widen its secret boundary.

## Decision

`ProviderBinding.key_env` is the sole declaration for a plugin provider key.
The gateway reads a declared key from its cluster `.env` file at the spawn
boundary. Bootstrap adds only enabled bindings' present keys to the authenticated
payload for split runners, preserving their raw file values. On a single box,
the agent child environment adds only enabled bindings' declared keys from the
runner's live environment. The worktree seed allowlist admits declared plugin
credentials while retaining its identity, data-plane, singleton-credential, and
runner-password exclusions.

## Alternatives rejected

- **Store the key in a plugin config image.** That creates a second secret
  channel with neither a scope model nor bootstrap distribution, and writes the
  credential into the plugin's plaintext configuration image.
- **Add every plugin key as a core Settings field.** A removable plugin would
  widen the core schema and configuration panel merely by existing, contradicting
  the provider-plugin boundary.
- **Forward arbitrary parent environment variables to agents.** That defeats the
  per-process positive allowlist and lets unrelated ambient credentials cross
  into every child process.
- **Keep plugin keys local to the gateway.** A split agent-runner could validate
  a plugin model at spawn but not construct it, and a seeded worktree would lose
  its usable provider credential surface.

## Consequences

- Plugin provider keys remain `.env`-managed and intentionally do not appear in
  the generic Settings/config-panel field walk.
- A rotated or newly added key reaches a split runner through its next bootstrap
  fetch; a single-box agent receives the declared live-environment value at
  spawn.
- The declaration is a secret-delivery capability, not authority to seed an
  arbitrary modeled setting or cluster credential.
