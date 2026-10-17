---
type: doc
title: Browser Service Gating
description: Capability gates for the headed browser and browser MCP services on agent-runner hosts.
tags: []
---

# Browser Service Gating

## Browser

`browser` requires `MEIVA_BROWSER_ENABLED` plus `browser_incapability()` (display,
Chrome, and npx). The converge browser step calls the same `browser_incapability()`
with the configured `MEIVA_CHROME_BINARY` from its read config.

On macOS, these static prongs are necessary but not sufficient at daemon launch.
`services/desktop/browser/macos_readiness.py` additionally waits for the current service
account to be the console GUI user, to have a `launchctl gui/<uid>` namespace,
and for its login Keychain to answer a read-only readiness query. The browser
session stays alive while waiting, and the healthcheck reports **DEGRADED**
instead of respawning it. This runtime gate never unlocks the Keychain or
changes Chrome profile data.

## Browser MCP

`browser-mcp` requires the same browser prongs
(`browser_incapability()`). The `chrome` MCP entry declares
`requires: {display}`.
