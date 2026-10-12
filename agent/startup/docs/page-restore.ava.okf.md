---
type: doc
title: Unavailable Pages
description: Page-server ownership of serving admission and terminal unavailable-link observation.
tags: []
---

# Unavailable Pages

Open `serve()`/`show()` pages are rows in `agent_pages`. The page-server service
is the sole observer of unavailable links. Agent boot and heartbeat do not probe,
re-serve or insert page-recovery messages.

For a fresh `serve()` registration, the service reserves a session name in the
row before creating its persistent shell. A live recorded session can be adopted
after service restart. If a reserved session is absent, the directory is missing,
or the recorded server fails its health check after startup grace, the service
closes that snapshot and logs a warning. It never relaunches the page server.
A concurrent fresh registration is protected by the row snapshot CAS.

The `dead_show_pages` loop probes open `show()` links, closes unreachable rows,
and publishes best-effort `PageClosed` events. Neither observer creates an
inbound message or wakes the agent to rebuild the page.

An explicit new registration can serve again. Link continuity across process or
machine loss is not guaranteed. Identity-checked cleanup, page TTL and normal
explicit close remain in place. Database connectivity failures can skip a dead
show-page scan; unexpected reconcile exceptions end the service after logging.
