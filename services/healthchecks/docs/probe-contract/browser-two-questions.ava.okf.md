---
type: doc
title: Browser protocol and ownership evidence
description: CDP success must be bound to the browser generation captured by root.
tags:
- ops
---

# Browser protocol and ownership evidence

CDP answers whether a browser protocol responds; it does not identify the
supervisor that owns it. The browser protocol probe checks the response and
profile facts, while the roster's `owned_service.probe_endpoint` wrapper verifies
the listener belongs to root's captured browser generation before and after that
probe. A same-profile orphan is not an owned service. Root never adopts or kills
it based on binary or profile similarity.

The browser reachability diagnostic runs its temporary canary inside the same
ownership envelope and contrasts it with a host request. It reports browser-only
failure, while an unusable CDP channel or failed host baseline is unavailable.
The temporary target is closed in finally. Diagnostic failure cannot restart
Chrome or discard user tabs.

macOS permission ancestry is established before root starts the browser; a
healthcheck does not kickstart a GUI-domain job or replace root's helper parent.
