---
type: doc
title: MCP OAuth 2.1 Flow
description: "OAuth 2.1 authorization-code + PKCE for remote MCP servers (`ava/_mcp_oauth.py`) — on 401 the provider discovers the authorization server, dynamically registers this client, opens the user's browser, captures the code on a loopback callback, stores tokens per server, and refreshes them transparently."
tags:
- mcp
- auth
---

# MCP OAuth 2.1 Flow

## What it is
The auth mode for remote (`url`) MCP servers that require OAuth rather than static API keys — implemented in `ava/_mcp_oauth.py` on top of the SDK's `OAuthClientProvider` (an `httpx2.Auth`). A server entry declares `"oauth": true` (mutually exclusive with `headers`, validated by `ava/_mcp_config.py:server_url`); the HTTP connect paths (`_connect_http` in both `ava/_mcps_daemon.py` and `ava/mcps.py`) build their client here instead of from the static-headers factory.

## The flow (authorization happens once per server)
1. A request to the endpoint draws a 401; the provider discovers the authorization server (RFC 8414 protected-resource metadata / RFC 9728) and dynamically registers this client (RFC 7591) with a redirect URI on the **fixed** loopback port 8931 — OAuth requires exact-match redirect URIs, so the port is a constant, and a taken port fails fast rather than registering a callback that can never be received
2. The authorization URL opens in the user's browser (`webbrowser.open` — the daemon runs on the headed machine, so the user's logged-in Chrome does the login); when no browser can be launched the URL is logged for a manual paste
3. `_CallbackServer` — a one-shot HTTP server bound to `127.0.0.1:8931/callback` — receives the redirect, parses `code` / `state` / `iss`, serves a "you can close this tab" page, and shuts down
4. The code is exchanged with PKCE; tokens and the registered client info are persisted per server at `$AVA_HOME/mcp_oauth/<server>.json`
5. Refresh tokens renew transparently (`grant_types` include `refresh_token`); stored tokens are reused across daemon restarts

## Concurrency
One in-flight flow per server, guarded by a module-level `asyncio.Lock` (`_oauth_locks`): concurrent connections for the same server wait on the lock and proceed with the cached client once tokens land; a failed flow releases the lock and surfaces the error.

## Timeouts
An OAuth connect envelope is 600 s (`_OAUTH_FLOW_TIMEOUT_S`) — the user has to click through a browser — versus the normal `sandbox.mcp_connect_timeout_seconds` for stdio/static-auth. An in-flight authorization must not be cut off by the request path's own timeout.

## Google issuer normalization shim
Google's protected-resource metadata advertises `https://accounts.google.com` while its AS metadata declares issuer `https://accounts.google.com/`; the SDK's strict string compare rejects that as a mismatch. `_install_issuer_normalization()` monkeypatches the SDK's `validate_metadata_issuer` (both modules that bind it) with a trailing-slash-stripping compare (RFC 3986 §6.2.1) — idempotent, applied when an OAuth client is built, and a real mismatch still raises.

## Storage discipline
Token files are created 0600 from the start via `os.open` mode (umask can only remove bits — no write-then-chmod window): the file holds OAuth tokens, so its mode must never depend on the umask.

## Entry Points
- `ava/_mcp_oauth.py:oauth_http_client(url, server)` — build the authenticated httpx client; the caller owns its lifecycle
- `ava/_mcps_daemon.py:_connect_http(..., oauth=True, server=...)` — daemon connect path
- `ava/mcps.py:_connect_http(..., oauth=True, server=...)` — local-fallback connect path (no daemon)
- `ava/_mcp_config.py:server_url` — config validation (`oauth` must be bool, exclusive with `headers`)

## Key Dependencies
- [[configuration.ava.okf.md]] — the `.mcp.json` auth modes this flow implements
- [[okf/mcps/mcps.ava.okf.md]] — the outbound MCP domain node
- SDK `mcp.client.auth` — `OAuthClientProvider` (discovery / DCR / token exchange), token + client-metadata models
