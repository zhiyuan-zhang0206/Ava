---
type: doc
title: Browser healthcheck's two questions
description: A CDP 200 answers neither identity nor supervision — the probe asks argv ownership + socket holder, and browser.py maps each verdict combination to respawn / report / sweep actions.
tags:
- ops
---

# Browser healthcheck's two questions

A CDP 200 answers neither of the questions that matter. It cannot tell the supervised Chrome from an orphan holding the same port — and `services/browser/daemon.py` deliberately refuses to launch while that port is served, so a CDP-only check stayed green forever with no browser under supervision — and it cannot tell OUR Chrome from another unit's, because CDP carries no field we control (measured: `/json/version` returns browser/protocol/UA/V8/WebKit strings and a per-launch websocket uuid; `DevToolsActivePort` is written only for an auto-assigned port).

So `services/browser/probe.py` asks identity a different way — a Chrome whose argv carries this cluster's `--user-data-dir` (the positive token `services/browser/orphan.py` established) **and** which holds the LISTEN socket on the CDP port — and `browser.py` asks supervision separately:

- verdict `PORT_TAKEN` (someone else's Chrome, or ownership unconfirmable) → report at ERROR, exit `EXIT_PORT_TAKEN`, **never respawn**. Asked first: our own session being alive does not make a respawn able to bind a port another netns won.
- session-dead + ours-alive → report at ERROR, do not respawn. An unsupervised Chrome of our own; `ava stop --stop-browser` sweeps it by profile.
- session-dead + CDP-dead → respawn.
- session-alive + CDP-dead → respawn (`respawn_service` kills the stale session first).
