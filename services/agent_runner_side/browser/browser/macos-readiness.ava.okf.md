---
type: doc
title: Browser macOS Startup Readiness
description: Read-only GUI-session and login-Keychain startup gate for the shared headed browser, with a healthcheck-visible degraded wait.
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

The gate succeeds only when all three read-only observations agree:

- `/dev/console` names the service account, proving that account owns the
  active GUI console session.
- `launchctl print gui/<uid>` confirms that account's GUI namespace exists.
- `security show-keychain-info` succeeds for that account's login Keychain.

A missing console user, unavailable GUI namespace, locked Keychain, or an
interaction-not-allowed Keychain response leaves the supervised daemon alive
and waiting. Each probe is bounded; the gate retries after five seconds and
periodically logs the explicit **DEGRADED** reason. It never unlocks a
Keychain, changes a login session, or launches Chrome without its encryption
material.

## Healthcheck Contract

While waiting, the daemon atomically records a private marker below
`$AVA_HOME/run` with its pid, process start time, reason, and observation time.
The marker is trusted only while the owning process still matches its recorded
start time and the observation is fresh. If the marker cannot be read or
written, `probe.py` and `healthchecks/browser.py` fall back to the same bounded,
read-only readiness check; a macOS probe failure fails safe to degraded rather
than creating restart churn. The marker is removed just before Chrome can
launch, so an ordinary CDP-down session remains restartable.

## Profile Safety

Automatic provisioning copies a daily profile only into an absent destination;
every existing profile directory, including an empty or partial first copy,
remains untouched. At launch `Local State` receives only existence,
read-permission, and future-mtime checks. Warnings are non-fatal, and Ava never
parses, rewrites, copies, or deletes that Chrome-owned file.
