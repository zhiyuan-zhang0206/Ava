---
type: doc
title: Android updates
description: "Manifest-driven in-app updates on Android: build-number ordering, digest and signer verification, the system installer hand-off, and both entry points."
tags:
  - frontend
  - android
  - tauri
---

# Android updates

The app never polls. One check runs per app session from the injected prompt
(`src-tauri/scripts/update-check.js`) through `app_check_update`: Rust reads the
rolling release's `android-latest.json` manifest (`versionCode`, `versionName`,
`apkUrl`, `sha256`) and compares its build number with the installed one
(`BuildConfig.VERSION_CODE`). A strictly greater `versionCode` is the only
"newer" signal — `versionName` is a human milestone and may repeat across
builds. A newer build shows a bottom bar and, when notifications are enabled,
one local notification; every failure there stays silent.

The bar's **Update** and the settings page's **Check for updates** share one
chain (`app_install_update`): re-read the manifest, download the APK over HTTPS
into the app cache, hash the stream, and refuse the file unless its SHA-256
matches the manifest. `AvaUpdatePlugin` then requires the archive's package name
and signing-certificate set to equal the installed app's, opens the per-app
"install unknown apps" setting when that grant is missing, and otherwise hands a
`FileProvider` content URI to the system installer. The Tauri template already
declares that provider at `${applicationId}.fileprovider` with a cache path; the
overlay adds `REQUEST_INSTALL_PACKAGES`. The user still confirms the install: a
silent swap is not available to side-loaded apps. Every failure returns the
manifest's download page, which both surfaces offer as the fallback.

The settings screen carries the About block: `app_update_info` supplies the
version name and build number without touching the network, and the manual
check renders its own status line. The remote console may run the same check and
install commands, but the manifest and download locations are fixed in Rust and
never come from the page.

Parent node: [[app.ava.okf.md|Ava App]].

```ava-contract
id = "android-update-prompt"
kind = "invariant"
statement = "The injected prompt asks the native check once per session, renders the bar only for an available build, and falls back to the download page when an install fails. Each case runs the checked-in script in a Node VM with canned command results."
code = ["ui/app/src-tauri/scripts/update-check.js:<module>"]
tests = ["tests/ui/test_android_update_check.py::test_offers_a_newer_build_with_an_install_action", "tests/ui/test_android_update_check.py::test_a_failed_install_falls_back_to_the_download_page"]
```
