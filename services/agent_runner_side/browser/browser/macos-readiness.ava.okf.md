---
type: doc
title: Browser macOS Startup Readiness
description: Read-only GUI-session, launch-domain and login-Keychain startup gate for the shared headed browser, with a healthcheck-visible degraded wait and a GUI-domain relaunch heal for a chain that lost the login session.
tags:
- browser
- macos
- security
---

# Browser macOS Startup Readiness

## What It Is

`services/browser/macos_readiness.py` prevents the headed browser from
launching on macOS until the detached service has the same GUI and login
Keychain prerequisites Chrome needs for its encrypted profile state. Static
browser capability remains a separate prerequisite; this is the runtime gate
that follows the CDP port guard and precedes daemon profile initialization.

## Readiness Contract

The gate succeeds only when all four read-only observations agree:

- `/dev/console` names the service account, proving that account owns the
  active GUI console session.
- `launchctl print gui/<uid>` confirms that account's GUI namespace exists.
- `launchctl managername` answers `Aqua`, proving THIS process chain runs
  inside that GUI session — a daemon launched or respawned from an agent/SSH
  chain inherits launchd's `Background` domain, where the Keychain is
  unreachable however ready and unlocked it is. That state is recorded as
  `context_missing` and the Keychain probe is skipped there, since it could
  only mislabel the symptom as a locked Keychain. An empty or failed
  `managername` answer is not evidence either way and falls through.
- `security show-keychain-info` succeeds for that account's login Keychain.

A missing console user, unavailable GUI namespace, wrong launch domain, locked
Keychain, or an interaction-not-allowed Keychain response leaves the
supervised daemon alive and waiting. Each probe is bounded; the gate retries
after five seconds and periodically logs the explicit **DEGRADED** reason. It
never unlocks a Keychain, changes a login session, or launches Chrome without
its encryption material.

## Healthcheck Contract

While waiting, the daemon atomically records a private marker below
`$AVA_HOME/run` with its pid, process start time, reason, `context_missing`, and
observation time. The marker is trusted only while the owning process still
matches its recorded start time and the observation is fresh; a missing or
malformed `context_missing` reads as false. If the marker cannot be read or
written, `probe.py` and `healthchecks/browser.py` fall back to the same
bounded, read-only readiness check in structured form
(`degraded_wait_state`); a macOS probe failure fails safe to degraded rather
than creating restart churn. The marker is removed just before Chrome can
launch, so an ordinary CDP-down session remains restartable.

A marker with `context_missing` is the one wait the healthcheck does not
preserve: it stops the stuck session (otherwise the GUI `ava start` would skip
the live session) and kickstarts the cluster's GUI-domain autostart job
(`shared/os_autostart.relaunch_via_gui_domain`), whose `ava start` rebuilds the
session in the GUI domain — at most twice per episode, 600 seconds apart, then
one episode-gated ERROR naming the manual recipe. A session-gone round inside
the relaunch window defers its own in-context rebuild so the relaunch is not
undone; the episode clears on the first healthy round or context-healthy wait.

## Profile Safety

Automatic provisioning copies a daily profile only into an absent destination;
every existing profile directory, including an empty or partial first copy,
remains untouched. At launch `Local State` receives only existence,
read-permission, and future-mtime checks. Warnings are non-fatal, and Ava never
parses, rewrites, copies, or deletes that Chrome-owned file.
