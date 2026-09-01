---
type: doc
title: Browser Service Gating
description: Capability gates for the headed browser and browser MCP services on agent-runner hosts.
tags: []
---

# Browser Service Gating

## Browser

`browser` requires `AVA_BROWSER_ENABLED` plus `browser_incapability()` (display,
Chrome, and npx). Fresh-host enroll/install uses the settings-free twin
`browser_deps_incapability()`: the same prongs, order, and reasons, without the
`AVA_CHROME_BINARY` override because Settings cannot be built on a fresh host.
The headed browser runs on Windows.

## Browser MCP

`browser-mcp` requires the same browser prongs plus AF_UNIX
(`browser_mcp_incapability()`), because the wrapper-to-daemon leg is a Unix
socket. It is POSIX-only: a Windows agent-runner gets headed Chrome over CDP and
no MCP front end, and the `chrome` MCP entry is gated off with it
(`requires: {display, unix_socket}`). Porting the transport is planned in
`future/infra/windows-browser-mcp.md`.
