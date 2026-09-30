---
type: doc
title: Browser macOS Startup Readiness
description: Read-only GUI-session, launch-domain and login-Keychain startup gate for the shared headed browser, with a visible degraded wait under the required macOS root ancestry.
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

## Root ancestry and observation

The macOS permissions helper is the permission-carrying parent of root. Browser
launch occurs under that established ancestry. The readiness marker remains
read-only diagnostic evidence; no probe can kickstart a GUI-domain job, stop a
session, or replace the helper. Unavailable GUI or Keychain prerequisites remain
visible as degraded readiness until an authorized external transition or changed
host state resolves them.
