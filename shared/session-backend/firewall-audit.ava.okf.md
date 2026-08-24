---
type: doc
title: macOS Firewall Manifest
description: shared/macos_firewall.py — the declarative per-binary Application Firewall allow-list manifest, renderer, and rootless-first reconciliation behind converge and OFF_BOX_UNREACHABLE attribution.
tags:
- shared
- macos
- firewall
---

# macOS Firewall Manifest

## Manifest and reconciliation

`shared/macos_firewall.py` owns the declarative macOS Application Firewall
allow-list manifest. Each entry carries stable identity, purpose, machine scope,
and globbed paths so versioned Python, Postgres, Homebrew, browser, and
observability binaries are resolved after upgrades. Redis is absent by design:
it is loopback-only and its off-box relay uses Apple's system Python.

The audit reads membership from `--listapps`, never `--getappblocked`: the latter
reports "permitted" for a path with no rule and cannot detect a missing entry.
The status renderer joins the manifest to that audit and reports every pattern,
resolved path, and Allow/Block/Missing state.

Reconciliation adds, unblocks, and prunes managed rules. On verified macOS 26.5
systems the three `socketfilterfw` mutations work without elevation. A failed
direct mutation is retried with bounded, non-interactive `sudo -n`; if that also
fails, converge reports the exact manual command and continues rather than
blocking unattended startup. `cli/commands/_converge_firewall.py` uses the
reconciler proactively, while `_gateway_ready.py` uses the same audit to explain
an `OFF_BOX_UNREACHABLE` verdict — see [[cli/commands/commands.ava.okf.md]].


Parent: [[shared/session-backend/session-backend.ava.okf.md|session backend]].
