---
type: doc
title: Browser protocol and ownership evidence
description: Browser protocol availability and destructive lifecycle ownership are separate.
tags:
- ops
---

# Browser protocol and ownership evidence

CDP answers whether a browser protocol responds. The browser protocol probe also
checks configured profile facts; read-only health does not bind the listener to
root ancestry. An available endpoint is not permission to adopt or kill it.

The browser reachability diagnostic uses CDP to create a temporary hidden
`about:blank` target in the shared browser context, runs a page-level fetch, and
contrasts a failed fetch with a host request. `hidden: true` keeps the target out
of the tab strip; `background: true` alone does not. It reports browser-only
failure, while an unusable CDP channel or failed host baseline is unavailable.
The temporary target is closed in finally. Chrome versions that reject hidden
targets report unavailable; the diagnostic never falls back to a visible tab.
Diagnostic failure cannot restart Chrome or discard user tabs.

macOS permission ancestry is established before root starts the browser; a
healthcheck does not kickstart a GUI-domain job or replace root's helper parent.
