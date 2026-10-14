---
type: doc
title: Shared user context
description: Workspace document reading, notification interaction and Inspector layout beside agent messages.
tags:
- frontend
---

# Shared user context

The home page displays the selected agent's workspace document beside its message
stream. Desktop gives the narrow agent tree 16% by default, then divides messages
and shared context 4:3. To user / Inspector tabs sit at the top of the entire
right panel. To user contains notifications above the shared document; Inspector
replaces that entire body with its original queries and a category selector.
The To user body stays mounted while hidden so switching tabs preserves reply
drafts and the document reading position. Below the desktop breakpoint the shared
context opens as a full-screen reading view with a return-to-messages control.

`["to-user", agentId]` owns the document snapshot. The home-machine ops server
reads `to_user.html` (default) or `to_user.md` (`to_user_format=markdown`) directly;
there is no publication registry or database content copy. The runner profile
includes the agent config domain because ops resolves `to_user_format`, with
stored agent pins taking precedence over the cluster default. One shallow watchfiles
watcher in each ops process broadcasts `to_user_changed`. The global fold repairs
that agent's query and includes the family in reconnect repair. Atomic replacement,
creation and deletion all invalidate. A missing or empty document does not open
a desktop panel; notices and an explicit Inspector selection still can.

HTML runs in an opaque-origin sandbox. It can render, run inline scripts and
navigate links, but receives no Ava token and cannot POST to the gateway. Actual
replies use the authenticated notification or conversation controls outside the
frame. The HTML response has its own sandbox CSP, including when opened directly.
Normal links navigate inside the reading frame; modifier clicks use the browser's
new-tab behavior. A source-checked postMessage bridge lets the authenticated shell
initiate HTTP(S) frame navigation, preserving SameSite cookies on private links
without giving the isolated document credentials or general API access. Third-party sites may refuse embedding; the browser's open-link
in-new-tab action remains available. Relative workspace assets are not served.
The document read accepts only a regular UTF-8 file up to 256 KiB.

An explicit refresh, focus, or SSE reconnect rechecks availability. A failed read
retains an already-cached snapshot and marks it stale; it never turns a machine
failure into an empty file. This is not an offline document store.
