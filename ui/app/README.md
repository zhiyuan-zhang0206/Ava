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
It accepts `entryUrl`, optional advanced `gatewayUrl`, `autoLogin`,
`backgroundService`, and `notifications`; it never stores a cluster secret.
Desktop auto-login reads `AVA_CLUSTER_SECRET` from `$AVA_HOME/.env` only when
`autoLogin` is enabled.

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
network-security configuration, JNI/reflection-safe ProGuard rules, and
optional release signing. A CI keystore is enabled only when
`src-tauri/gen/android/keystore.properties` exists; see the release workflow
for the three Android signing secrets.

The shared onboarding page has one primary server field. On Android, a bare
host means `http://host:3000`, and a pasted default gateway URL (`:8000`)
becomes that console URL. Paths, queries, and fragments are stripped; `https`
is preserved; other primary-field ports point the user to the advanced gateway
override, which keeps existing manual `gatewayUrl` settings compatible. Desktop
uses the same page without a secret field but preserves its existing arbitrary
entry and gateway URLs for worktree development. Android's optional cluster
secret is logged in natively, saved to Android Keystore only after that login
succeeds, and exposed to the webview only as the resulting HTTP-only session
cookie.

Saving switches immediately to a 30-second connecting screen, then defers the
post-save window change until its IPC response flushes. Android refreshes the
existing webview's prelude and navigates it rather than rebuilding it; desktop
keeps the native rebuild. If the page is hidden when that screen times out, it
recovers as soon as the page is visible again. The entry watchdog makes an HTTP
GET (four-second per-attempt timeout) rather than a TCP connect, accepting
2xx/3xx/401/403 answers and ending with an unreachable, HTTP, or rollout-window
recovery state within 30 seconds. Plain HTTP is accepted only when the resolved
target is private (loopback, link-local, RFC1918, or `100.64.0.0/10`); public
targets require HTTPS.

## Verification

```bash
cd ui/app/src-tauri
cargo fmt --all --check
cargo clippy --all-targets -- -D warnings
cargo test
cargo check --target aarch64-linux-android
cd ../../..
.venv/bin/pytest tests/ui/test_android_overlay.py \
  tests/scripts/test_build_shell_update_manifest.py -q
```

Tags matching `shell-v<major>.<minor>.<patch>` drive
`.github/workflows/release-shell.yml`. macOS/Windows updater assets are listed
in `latest.json` only when their Tauri signatures exist. Tag builds fail unless
updater, OS, and Android signing credentials are present; manual dispatch keeps
the explicit unsigned validation path without publishing a GitHub Release.
