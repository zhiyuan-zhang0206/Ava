# MCP client credentials replace shared cluster identity

The optional gateway `/mcp` endpoint must not treat every external integration
as the cluster itself. A cluster cookie or secret grants one shared identity,
cannot revoke a single client, and cannot distinguish read-only inspection from
fleet mutations.

The endpoint therefore authenticates independently with one revocable token
per named MCP client. Tokens are stored only as SHA-256 hashes; client records
carry `read` or `write` scope, while their cluster-authenticated admin API shows
the plaintext token only at creation. This boundary remains active on
no-secret clusters.

Audit events identify the MCP client but never retain raw tool arguments. They
record only each argument's JSON type, character count, and SHA-256 so an
operator can correlate calls without copying prompts or message content into
the audit stream.
