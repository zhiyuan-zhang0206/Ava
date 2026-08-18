---
type: doc
title: Browser — Teardown Reaches Chrome by Profile
description: How the explicit browser-teardown paths kill a Chrome that left the supervised process tree on a SingletonLock handoff — identification by the cluster's own --user-data-dir, positive only, never by exclusion.
tags: []
---

# Browser — Teardown Reaches Chrome by Profile

## What is it
The identification and kill that `ava stop --stop-browser` / `ava cluster destroy` use to make sure this cluster's headed Chrome is actually gone, and not merely detached from the session that was killed. Implementation: `services/browser/orphan.py`, called from `cli/commands/stop.py:_do_stop`.

## The problem a tree walk cannot solve
After a `SingletonLock` handoff Chrome is no longer a descendant of the supervised `ava-browser` session, so killing that session (`shared/winproc.py:_terminate_tree` on Windows, `shared/posixproc.kill_session` on POSIX) left the Chrome running and holding the cluster's CDP port — which the next launch's port guard then refused, permanently, until an operator killed the pid by hand (done on the `win` runner 2026-07-28, after which `ava-browser` came back healthy). No better tree walk fixes this: `shared/proc.py:kill_process_tree` resolves descendants by ppid, and the ppid link to the session is exactly what the handoff destroyed. What was missing is a way to *name* Ava's Chrome without walking to it.

## The identification
A process is this cluster's Chrome iff all three hold (`orphan.is_cluster_chrome`):
1. its argv carries the exact token `--user-data-dir=<$AVA_HOME/chrome-profile>` (compared as a `Path`, so a trailing separator or Windows case folding cannot hide it);
2. its argv carries no `--type=` — Chrome tags every process it spawns for itself, so this narrows the match to the browser process; helpers die as its descendants;
3. its executable name looks like Chrome/Chromium.

(1) authorizes; it is the per-cluster profile path, minted under a `$AVA_HOME` this install alone owns. (3) only narrows — it keeps a `grep` that merely mentions the path out of the kill.

## Why it cannot take the operator's browser
**Positive identification only, never by exclusion.** The operator's daily Chrome runs on the platform default and passes no `--user-data-dir` at all (measured on the dev Mac: a flagless 60-character argv); any other explicitly-profiled Chrome passes a different path (measured beside it: a `chrome-devtools-mcp` Chrome on its own cache profile). Neither can come to claim ours. The one non-daemon process that *can* satisfy (1) is a Chrome started by hand against Ava's profile — which is holding Ava's profile lock and Ava's CDP port, is the squatter the port guard complains about, and is a thing a full teardown is right to take down.

Unreadable argv (another user's process, `psutil.AccessDenied`) is a **skip, not an error**: the token is the only thing that authorizes a kill, so a process whose argv cannot be read has not been identified and the teardown carries on. The asymmetry is deliberate — a missed orphan costs one manual kill, a wrong kill costs the operator their logged-in browser.

Rejected alternative: the profile's own `SingletonLock` pid (`profile.py:_running_chrome_pid`). It is positive, but a *stale* lock plus pid recycling could point at an unrelated process, which is the one mistake that must be impossible; and it is a POSIX-only symlink.

## Scope
Only the paths that explicitly ask for the browser down: `ava stop --stop-browser`, and `ava cluster destroy` (whose child stop passes `--stop-browser`) — both funnelling through `_do_stop(keep_browser=False)`. Plain `ava stop`, `ava restart` and `ava update` preserve the login Chrome and never sweep, so a normal stop pays nothing.

## Ordering, idempotence, platform
Runs after every session kill: the watchdog is already dead by then, so nothing relaunches Chrome onto the just-cleared port, and no launch is in flight for the port guard to fight. A silent no-op with nothing to reap (one process-table pass, no output), idempotent (a second sweep finds nothing; `kill_process_tree` is a no-op on a gone pid), and it never fails the teardown around it. **Platform-neutral and unbranched** — a `SingletonLock` handoff is Chrome's behaviour, not Windows'. POSIX makes it rare (the daemon `os.execvp`s, so the pane's process *is* Chrome and no launcher is left to hand off from), but a Chrome that detaches from that exec'd process leaves the identical orphan on the identical CDP port, and `psutil` supplies argv on both.

## Key Dependencies
- [[services/agent_runner_side/browser/browser.ava.okf.md]] — the parent service
- `shared/proc.py` — `kill_process_tree` does the killing once the root is correctly named
