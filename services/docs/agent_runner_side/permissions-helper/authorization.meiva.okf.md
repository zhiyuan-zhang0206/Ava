---
type: doc
title: Permissions Helper — Authorization Model
description: "The helper's two macOS TCC grants, the two planes each is read on (system record vs the running process), the helper-owned request path, and how a stable signing identity keeps the grants across rebuilds."
tags: []
---

# Authorization Model

The helper has two independent macOS TCC grants:

- **Screen Recording** authorizes `screencapture_region`, `screencapture_window` and window metadata; without it, the helper refuses captures before starting a child or SCK request.
- **Accessibility** authorizes `click`, `drag`, `move`, `focus_app`, `type`, `key`, `scroll`, `ax_window_info`, `ax_tree`, and `ax_act`; without it, macOS silently drops synthetic input and denies accessibility-tree reads.

`ping` reports both facts as `preflight_screen` and `ax_trusted`. The Swift dispatch gate refuses every Accessibility-gated operation with an explicit error when `ax_trusted=false`, triggering the System Settings prompt at most once per 30s. Requests never wait for human response. Converge preflights both grants with `_ensure_screen_capture` and `_ensure_accessibility`, then agent startup reports either unavailable axis (or one combined notice when both fail).

`request_grants` (advertised as `ping.request_grants_v1`) raises the two system dialogs this identity can still get: `CGRequestScreenCaptureAccess()` for Screen Recording and the same rate-limited Accessibility prompt. It runs inside the helper on purpose -- a child process inherits its launch chain's Accessibility trust, so a prompt raised there is answered for an identity that is not the one that will use the grant. macOS shows one dialog per bundle identity and answers later requests for the same grant silently, so a repeat is free and the caller must read the grant back rather than treat the request as consent.

TCC keys grants on the helper's code identity. A stable certificate plus fixed bundle id preserves both grants across rebuilds; ad-hoc signing or a regenerated identity drops them once and the operator must re-grant in System Settings. Accessibility applies to the already running helper immediately. A changed Screen Recording grant may require an externally coordinated stop and helper restart. A descendant must not kickstart its own ancestor.
