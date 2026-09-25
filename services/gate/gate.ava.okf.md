---
type: doc
title: Fleet UI Gate
description: Root-owned HTTP entry for authentication, maintenance projection, and frontend proxying.
tags:
- services
- gateway
---

# Fleet UI Gate

`services/gate/daemon.py` owns the public `frontend` port slot and proxies the
Next.js app on the separate `app` slot. The canonical `gate` service spec has
gateway capability, needs no database, and runs as an ordinary child of
[[services/ava_root/ava_root.ava.okf.md|root]]. Selection, readiness, restart, and
stop use the same lifecycle as the other application services. Full planned
application downtime includes the entry listener.

`GET /__ava/healthz` returns the serving Gate's name, home, and PID independently
of login state, gateway availability, and app availability. It accepts only GET.
The roster wraps that protocol check with captured root ownership and native
listener ancestry before and after the request. An unrelated listener, an
unknown process birth, or an unobservable root cannot certify readiness.
`ava status` reports Gate in its ordinary root/probe service table.

For product requests, Gate reads one immutable
[[shared/ui_update_state.ava.okf.md|UI update snapshot]]. A valid active generation
renders System updating from its stable start time. Without an active generation,
Gateway/app transport failure renders Service unavailable; malformed state has
the same unavailable projection. Auth forwards the session cookie to
`/api/auth/check`; an authenticated response permits proxying the app. Login,
updating, and unavailable pages are dependency-free static assets loaded at boot.
Next.js navigation request headers and response security headers cross the proxy.

`GET /__ava/deploy-state` exposes `{status,generation}` with `no-store` before
gateway/app probes. An already-open SPA uses it only as a reload hint; Gate owns
the maintenance page and clock. This response exists only while Gate is running:
a durable update marker does not imply an available entry listener during a root
transition. Normal root startup loads the candidate code and static assets.

The loopback product tests exercise auth, maintenance snapshots, proxy behavior,
and health independently of dependencies. The native child test exercises real
Gate startup, root IPC ownership, restart, stop, and rejection of a foreign
listener. It does not prove macOS helper ancestry or a complete cluster rollout.
