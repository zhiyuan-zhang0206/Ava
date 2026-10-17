---
type: doc
title: Android shell chrome
description: How the Android shell keeps the webview out of the system bars and what it may draw over the console.
tags:
  - android
  - tauri
---

# Android shell chrome

## System bars

Tauri's generated `MainActivity` calls `enableEdgeToEdge()`, so the webview's
top edge is y=0 under the status bar and its bottom edge under the navigation
bar. A plain Android WebView reports no CSS safe-area insets to the page, so
the console cannot pad itself out of the system UI: the fix has to be native.
The build-time overlay pads the generated activity's content root by the system
bars plus the display cutout and re-requests the insets after installing the
listener, so every page sits inside the usable area on edge-to-edge devices and
the padding is zero where the window already fits. The patch is idempotent and
refuses an unrecognised activity template rather than shipping an APK whose
header hides under the status bar (`tests/ui/test_android_overlay.py`).

## What the shell may draw over the console

The console page carries no app-injected floating controls: the Android
settings shortcut that used to float at the bottom-right corner sat exactly on
top of the composer's send/stop button (measured 390x844: 34x36 of a 36x36
button, z-index 2147483646) and is gone. The route back to the bundled
settings page now lives in the console's own Control page: its App section
renders only when `window.__TAURI_INTERNALS__` exists (that is, inside the app)
and calls `app_open_settings`, which the console origin is granted at runtime.
The failure screen still offers "Change address", and the update check runs on
every console load. Anything the injection layer adds to the console must stay
out of the composer band.

Android's network-security XML cannot express IP prefixes, so it permits
cleartext transport while Rust refuses to persist an HTTP endpoint unless all
resolved addresses are loopback, link-local, RFC1918, or `100.64.0.0/10`.
HTTPS is unrestricted.

## System bars

Tauri's generated `MainActivity` calls `enableEdgeToEdge()`, so the webview's
top edge is y=0 under the status bar and its bottom edge under the navigation
bar. A plain Android WebView reports no CSS safe-area insets to the page, so
the console cannot pad itself out of the system UI: the fix has to be native.

The overlay pads the generated activity's content root by the system-bar plus display-cutout insets, after Tauri's own enableEdgeToEdge() and before the page renders, so every page sits inside the usable area on edge-to-edge devices and the padding is zero where the window already fits; the patch is idempotent and refuses an unrecognised activity template instead of shipping an APK whose header hides under the status bar.
