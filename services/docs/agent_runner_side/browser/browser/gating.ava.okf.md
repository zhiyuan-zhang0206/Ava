---
type: doc
title: Browser Service Gating
description: Capability gates for the headed browser and browser MCP services on agent-runner hosts.
tags: []
---

# Browser Service Gating

## Browser

`browser` requires `AVA_BROWSER_ENABLED` plus `browser_incapability()` (display,
Chrome, and npx). The converge browser step uses the settings-free twin
`browser_deps_incapability()`: the same prongs, order, and reasons, without the
`AVA_CHROME_BINARY` override because Settings cannot be built on a fresh host.

On macOS, these static prongs are necessary but not sufficient at daemon launch.
`services/browser/macos_readiness.py` additionally waits for the current service
account to be the console GUI user, to have a `launchctl gui/<uid>` namespace,
and for its login Keychain to answer a read-only readiness query. The browser
session stays alive while waiting, and the healthcheck reports **DEGRADED**
instead of respawning it. This runtime gate never unlocks the Keychain or
changes Chrome profile data.

## Browser MCP

`browser-mcp` requires the same browser prongs plus AF_UNIX
(`browser_mcp_incapability()`), because the wrapper-to-daemon leg is a Unix
socket. The `chrome` MCP entry uses the same gate
(`requires: {display, unix_socket}`).
