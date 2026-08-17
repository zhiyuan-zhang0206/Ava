# Ava shell

`ui/app` is the thin Tauri 2 shell around Ava's remotely served web console. It
does not bundle `ui/web`; the main webview loads the configured gate URL. One
Rust application targets macOS, Windows, and Android.

## Local desktop development

Prerequisites are Rust stable plus the platform libraries required by Tauri 2.
The default desktop entry is `http://localhost:3000`.

```bash
cd ui/app
cargo tauri dev
```

The persisted `settings.json` lives in Tauri's platform app-config directory.
It accepts `entryUrl`, optional `gatewayUrl`, `autoLogin`, `backgroundService`,
and `notifications`. Desktop auto-login reads `AVA_CLUSTER_SECRET` from
`$AVA_HOME/.env` only when `autoLogin` is enabled.

For a production desktop bundle:

```bash
cd ui/app
cargo tauri build
```

The updater public key is checked into `tauri.conf.json`; its private key is
never stored in this repository. Use Tauri's `TAURI_SIGNING_PRIVATE_KEY` and
`TAURI_SIGNING_PRIVATE_KEY_PASSWORD` environment variables to produce updater
signatures. `cargo tauri build --no-sign` is the explicit unsigned validation
path.

## Android

The generated Gradle project is intentionally ignored. Generate it from the
installed Tauri version, then apply the checked-in overlay:

```bash
cd ui/app
cargo tauri android init --skip-targets-install
cd ../..
python3 ui/app/android/apply_overlay.py
cd ui/app
cargo tauri android build --apk
```

The overlay adds the foreground service, notification/network permissions,
network-security configuration, and optional release signing. A CI keystore is
enabled only when `src-tauri/gen/android/keystore.properties` exists; see the
release workflow for the three Android signing secrets.

Android first run asks for the gate URL and opt-in background/notification
settings. Plain HTTP is accepted only when the resolved target is private
(loopback, link-local, RFC1918, or `100.64.0.0/10`); public targets require
HTTPS.

## Verification

```bash
cd ui/app/src-tauri
cargo fmt --all --check
cargo clippy --all-targets -- -D warnings
cargo check --target aarch64-linux-android
cd ../../..
.venv/bin/pytest tests/ui/test_android_overlay.py \
  tests/scripts/test_build_shell_update_manifest.py -q
```

Tags matching `shell-v<major>.<minor>.<patch>` drive
`.github/workflows/release-shell.yml`. macOS/Windows updater assets are listed
in `latest.json` only when their Tauri signatures exist; unsigned release runs
publish an empty updater platform map rather than an unverifiable update.
