---
type: doc
title: Ava App
description: Thin Tauri 2 app that loads the remote Ava console on macOS, Windows, and Android, adding native residency, login, notification, and update behavior.
tags:
  - frontend
  - desktop
  - android
  - tauri
---

# Ava App

## What it is

`ui/app/` is one Tauri 2 application for macOS, Windows, and Android. It bundles
only the small onboarding/retry/settings surface in `app-ui/`; the product UI
is always the remote gate-served [[../web/web.ava.okf.md|Next.js console]]. The
app owns the native window and capabilities that a browser tab cannot supply.

## Entry and trust boundary

`settings.json` in the platform app-config directory is the persisted
non-credential configuration. `entryUrl` names the gate; `gatewayUrl` is
optional and otherwise derives from the same host on port 8000. Desktop defaults
to `localhost:3000`; Android starts on onboarding. Android's primary server
field normalizes a bare host or a default gateway URL into the console origin
on 3000, preserves `https`, and removes paths, queries, and fragments. Other
Android primary-field ports are refused with an advanced-options hint; the
optional `gatewayUrl` override keeps its existing custom-port capability (and
maps a bare host or `:3000` to `:8000`). Desktop shares the one-field page but
preserves its existing arbitrary entry and gateway URLs for worktree clusters.

The bundled Chinese onboarding page immediately changes to a 30-second
connecting state while saving. `app_save_settings` persists Android-normalized
settings, then defers the window change until the asynchronous IPC reply flushes.
Android refreshes the existing webview's prelude and navigates it rather than
destroying it; its navigation allowlist and page-load prelude resolve from live
settings so the new console origin and notification bridge see the saved
configuration. Desktop keeps its rebuild. A hidden Android page retains its
timeout recovery until it becomes visible. `window.rs` probes the console root
with a four-second HTTP GET rather than TCP, treats
2xx/3xx/401/403 as healthy, and sends a final `reason=unreachable|http|update-window`
query into the bundled failure screen within the retry budget.

`src-tauri/src/window.rs` builds one webview and makes bounded HTTP probes of
the gate while it connects. Native navigation accepts only the exact gate
and gateway origins plus bundled Tauri assets. New-window links leave through
`src-tauri/src/external.rs`: local headed cluster Chrome first on supported Unix
desktops, then the system browser. Only HTTP(S), `mailto:`, and `tel:` may cross
that OS-opening boundary.

The bundled origin gets settings commands through `capabilities/local.json`.
The configured remote console gets a narrower runtime capability with no
credential-reading command. Desktop auto-login exchanges the local cluster
secret from `$AVA_HOME/.env` in native Rust. Android accepts an optional secret
only in the bundled form; after the first successful native login it encrypts
the value with Android Keystore and stores ciphertext in SharedPreferences. In
both cases the webview receives only the resulting HTTP-only session cookie;
401/403 falls back to the console login without retrying or clearing Android's
stored credential. Android loads that credential through an asynchronous native
plugin call into a process-local cache; a first window prelude may report
`autoLogin: false` while loading, but startup login uses the same loader and
does not block the Android main looper.

## Desktop

`src-tauri/src/desktop.rs` provides one-instance behavior, tray open/quit,
close-to-tray, launch-at-login, and Tauri updater checks. Auto-login reads the
local `$AVA_HOME/.env` secret, performs the gateway login outside web content,
and reloads after its native cookie store receives the session.

## Android

Android has opt-in background residency and local notifications. A generated
Tauri Gradle project is patched at build time by `android/apply_overlay.py`; the
checked-in Kotlin plugins control a non-exported `specialUse` foreground service,
the Android-Keystore secret bridge, and CookieManager session-cookie injection.
`AvaClickPlugin` captures a notification tap so the bridge can open the fleet
Inbox at `/fleet#inbox`: after its SSE opens, the bridge consumes the per-tap
flag once and navigates the same window.
The overlay also keeps the JNI-named
Tauri plugin classes and reflectively discovered commands through release
minification. The injected SSE bridge listens to
`/api/system` and notifies only on busy-to-idle completion or a newly
awaiting-response notice. Notification IPC also rechecks persisted consent
natively. The bridge starts from a fresh prelude or waits once for its
`ava-app-config` event. All Android plugin calls leave the main looper before waiting for
their JNI response; direct synchronous mobile-plugin calls would deadlock it.

Android's network-security XML cannot express IP prefixes, so it permits
cleartext transport while Rust refuses to persist an HTTP endpoint unless all
resolved addresses are loopback, link-local, RFC1918, or `100.64.0.0/10`.
HTTPS is unrestricted. APK updates are explicit GitHub Release downloads, not
silent installs.

## Build and release

`Cargo.toml` is version `0.4.0`. `.github/workflows/ci-app.yml` path-filters
Rust format/clippy, Android-target checking, and overlay/manifest tests.
`release-app.yml` builds universal macOS DMG and Windows NSIS; `release-android.yml` builds the Android APK (`android-v*` tags)
artifacts for `app-v*` tags. OS signing and updater signing activate only for
complete secret groups; tag releases fail closed when any required signing
group is absent, while manual dispatch may still produce unsigned evidence.
`scripts/build_app_update_manifest.py` emits signed updater entries only, and
the mutable `app-latest` release carries the stable `latest.json` endpoint.

## Limits

The Android foreground service keeps the process resident but notification SSE
still lives in the webview; force-stop/closed-app delivery would require a push
service and is out of scope. Android cleartext CIDR enforcement is an
application-time DNS check because the platform XML supports domain names, not
network prefixes.

## Key paths

- `src-tauri/src/` — Rust lifecycle, settings, ACL, navigation, platform wiring
- `src-tauri/scripts/` — scripts injected into the remote console
- `app-ui/index.html` — bundled onboarding/retry/settings UI
- `android/` — Kotlin/XML/ProGuard foreground-service and Keystore overlay plus deterministic patcher
- [README](README.md) — local build and signing commands
- [Future distribution work](../../future/ui-app.md) — remaining external work
