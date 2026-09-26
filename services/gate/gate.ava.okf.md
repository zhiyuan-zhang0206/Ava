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

For product requests, auth forwards the session cookie to `/api/auth/check`; an
authenticated response permits proxying the app. A gateway or app transport
failure renders Service unavailable; Gate does not guess whether the cause is a
transition, a service, or the host. Gate keeps no update state: a release
transition stops Gate with the rest of root, so no listener exists to describe
it, and a `$AVA_HOME/deploy-state.json` left by the retired updater is ignored.
The login and unavailable pages are dependency-free static assets loaded at
boot. Next.js navigation request headers and response security headers cross
the proxy. Normal root startup loads the candidate code and static assets.

The loopback product tests exercise auth, the unavailable projection, proxy
behavior, and health independently of dependencies. The native child test exercises real
Gate startup, root IPC ownership, restart, stop, and rejection of a foreign
listener. It does not prove macOS helper ancestry or a complete cluster rollout.
