---
type: doc
title: macOS Firewall Audit
description: shared/macos_firewall.py — the read-only per-binary Application Firewall allow-list audit behind the converge report and OFF_BOX_UNREACHABLE attribution (issue #949).
tags:
- shared
- macos
- firewall
---

# macOS Firewall Audit

## The audit

`shared/macos_firewall.py` is a **read-only** audit of the macOS Application Firewall's per-binary allow list, and read-only by necessity rather than by taste: mutating it needs root, which Ava does not have (see the module's own docstring for how that was established). It exists because every binary Ava serves an off-box port from lives at a version-stamped path, so a `uv python` / vendored-Postgres bump orphans its allow rule while loopback keeps answering — issue #949. Membership is read from `--listapps`, never `--getappblocked`: the latter reports "permitted" for a path with no rule at all, so it cannot see the defect. Consumed by `cli/commands/_converge_firewall.py` (proactive report) and `_gateway_ready.py`'s `OFF_BOX_UNREACHABLE` verdict (post-hoc attribution) — see [[cli/commands/commands.ava.okf.md]].


Parent: [[shared/session-backend/session-backend.ava.okf.md|session backend]].
