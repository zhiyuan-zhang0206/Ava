# Asserted external caller identity

`CallerIdentity` is the bounded external/unknown provenance value object. It is
not an authenticated principal, capability, transport, request ID, or Ava agent
identity. Unknown fields and malformed identifiers are rejected. No secrets or
human identity belong in an instance identifier.

`external_agent:codex:run-42` and `unknown:legacy` are display projections.
`shared.envelope` reads them as explicitly asserted external or unknown callers,
never as User, Ava Agent, or system. Existing source formats remain readable.

## Rollout boundary

This reader foundation does not enable any producer. Older binaries reject the
new source formats, so producers must remain disabled until an explicit protocol
and consumer-convergence gate is deployed. Never wrap external provenance in
`system:*` or `agent:*` to bypass an old validator. A caller field accepted by an
HTTP schema but discarded before persistence is not structured audit storage.

Subsequent integration must persist nullable structured provenance at the
initiating write and audit boundary, reject conflicts with the display source,
and preserve null historical provenance as unverified legacy. CLI/MCP/SDK
producers must fail clearly against incompatible consumers. Shared cluster
credentials cannot prove the caller label: authorization and idempotency scopes
must derive from actual server-bound credentials, not this object.
