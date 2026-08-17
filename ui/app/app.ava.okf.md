---
type: doc
title: Ava app shell
description: Thin Tauri 2 shell that loads the remote Ava console on macOS, Windows, and Android, adding native residency, login, notification, and update behavior.
tags:
  - frontend
  - desktop
  - android
  - tauri
---

# Ava app shell

## What it is

`ui/app/` is one Tauri 2 application for macOS, Windows, and Android. It bundles
only the small onboarding/retry/settings surface in `shell-ui/`; the product UI
is always the remote gate-served [[../web/web.ava.okf.md|Next.js console]]. The
shell owns the native window and capabilities that a browser tab cannot supply.

## Entry and trust boundary

`settings.json` in the platform app-config directory is the only persisted
state. `entryUrl` names the gate; `gatewayUrl` is optional and otherwise derives
from the same host on port 8000. Desktop defaults to `localhost:3000`; Android
starts on onboarding.

`src-tauri/src/window.rs` builds one webview and retries gate reachability ten
times at three-second intervals. Native navigation accepts only the exact gate
and gateway origins plus bundled Tauri assets. New-window links leave through
`src-tauri/src/external.rs`: local headed cluster Chrome first on supported Unix
desktops, then the system browser. Only HTTP(S), `mailto:`, and `tel:` may cross
that OS-opening boundary.

The bundled origin gets settings commands through `capabilities/local.json`.
The configured remote console gets a narrower runtime capability. The cluster
secret permission is added only on desktop when auto-login is enabled; Android
and disabled desktop sessions cannot invoke it.

## Desktop

`src-tauri/src/desktop.rs` provides one-instance behavior, tray open/quit,
close-to-tray, launch-at-login, and Tauri updater checks. Auto-login reads the
local `$AVA_HOME/.env` secret and lets the webview perform the normal gateway
login request so its own cookie store receives the session.

## Android

Android has opt-in background residency and local notifications. A generated
Tauri Gradle project is patched at build time by `android/apply_overlay.py`; the
checked-in Kotlin plugin controls a non-exported `specialUse` foreground
service. The injected SSE bridge listens to `/api/system` and notifies only on
busy-to-idle completion or a newly awaiting-response notice. Notification IPC
also rechecks persisted consent natively.

Android's network-security XML cannot express IP prefixes, so it permits
cleartext transport while Rust refuses to persist an HTTP endpoint unless all
resolved addresses are loopback, link-local, RFC1918, or `100.64.0.0/10`.
HTTPS is unrestricted. APK updates are explicit GitHub Release downloads, not
silent installs.

## Build and release

`Cargo.toml` is version `0.4.0`. `.github/workflows/ci-shell.yml` path-filters
Rust format/clippy, Android-target checking, and overlay/manifest tests.
`release-shell.yml` builds universal macOS DMG, Windows NSIS, and Android APK
artifacts for `shell-v*` tags. OS signing and updater signing activate only for
complete secret groups; missing groups still produce unsigned evidence.
`scripts/build_shell_update_manifest.py` emits signed updater entries only, and
the mutable `shell-latest` release carries the stable `latest.json` endpoint.

## Limits

The Android foreground service keeps the process resident but notification SSE
still lives in the webview; force-stop/closed-app delivery would require a push
service and is out of scope. Android cleartext CIDR enforcement is an
application-time DNS check because the platform XML supports domain names, not
network prefixes.

## Key paths

- `src-tauri/src/` — Rust lifecycle, settings, ACL, navigation, platform wiring
- `src-tauri/scripts/` — scripts injected into the remote console
- `shell-ui/index.html` — bundled onboarding/retry/settings UI
- `android/` — Kotlin/XML overlay and deterministic patcher
- [README](README.md) — local build and signing commands
- [Future distribution work](../../future/ui-shell.md) — remaining external work
