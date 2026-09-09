---
type: doc
title: Browser healthcheck's two questions
description: A CDP 200 answers neither identity nor supervision — the probe asks argv ownership + socket holder, and browser.py maps each verdict combination, including an intentional macOS readiness wait, to the safe action.
tags:
- ops
---

# Browser healthcheck's two questions

A CDP 200 answers neither of the questions that matter. It cannot tell the supervised Chrome from an orphan holding the same port — and `services/browser/daemon.py` deliberately refuses to launch while that port is served, so a CDP-only check stayed green forever with no browser under supervision — and it cannot tell OUR Chrome from another unit's, because CDP carries no field we control (measured: `/json/version` returns browser/protocol/UA/V8/WebKit strings and a per-launch websocket uuid; `DevToolsActivePort` is written only for an auto-assigned port).

So `services/browser/probe.py` asks identity a different way — a Chrome whose argv carries this cluster's `--user-data-dir` (the positive token `services/browser/orphan.py` established) **and** which holds the LISTEN socket on the CDP port — and `browser.py` asks supervision separately. Before identity, the probe also validates the `/json/version` BODY: a 200 whose body is not valid JSON carrying `Browser` reads as `DOWN`, never `ALIVE` — that is the wedged-DevTools shape of the 2026-09-09 macmini swap-pressure outage (orphaned Chrome answered 200 with an empty body for ~8 minutes while a status-only check stayed green).

- verdict `PORT_TAKEN` (someone else's Chrome, or ownership unconfirmable) → report at ERROR, exit `EXIT_PORT_TAKEN`, **never respawn**. Asked first: our own session being alive does not make a respawn able to bind a port another netns won.
- session-dead, whatever the probe says (ours-alive OR CDP-dead — a `DOWN` can hide a wedged orphan still holding the port) → sweep the identity-verified Chrome (a no-op when none is left) and rebuild the session. A plain respawn cannot win while an orphan holds the port: the daemon refuses to launch a second Chrome on it.
- session-alive + CDP-dead + current macOS readiness marker → report **DEGRADED** and preserve the waiting session; the daemon is deliberately waiting for a GUI session and usable login Keychain, not crashed.
- session-alive + CDP-dead without that marker → respawn (`respawn_service` kills the stale session first).
