# Install-script supply-chain hardening

## Context

The installer included an opt-in MCP package cache warm-up that invoked `npx`
against unpinned package names. An install could therefore download and execute
the current published releases with the operator's permissions.

## Decision

Remove the installer warm-up surface instead of pinning or otherwise retaining
it. The packages have no runtime callers, so installation does not need to
execute third-party MCP server code. The installer now rejects the former option
as an unknown argument, and its argument-contract test locks that behavior.
