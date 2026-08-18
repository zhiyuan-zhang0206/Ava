---
type: doc
title: Permissions Helper — macOS Desktop Automation Daemon
description: A signed Swift daemon on agent-runner that holds Screen Recording / Accessibility permissions — receives JSON requests via Unix socket and performs privileged desktop operations such as screenshot, click, type, and window geometry on behalf of all skills. macOS only.
tags: []
---

# Permissions Helper — macOS Desktop Automation Daemon

## What is it
A macOS permissions helper on agent-runner — a signed Swift `.app` launched by **launchd** (not session/watchdog), holding the machine's Screen Recording / Accessibility permissions. All skills that need to drive the desktop (screenshot, click, type, window geometry) call it via Unix socket, rather than each shelling out `screencapture` / sending CGEvent — privileged, authorized actions are centralized in this single process.

**Role affiliation**: agent-runner side (macOS only) — launched by launchd, not in the session service roster (`build_services`), with capability probe `permissions_helper_incapability` gating.

## Why launchd + Stable Signing is Required
- **launchd launch**: the helper must be its own responsible process to independently hold permission grants, rather than borrowing the grant from the terminal that launched it.
- **Stable self-signed certificate**: TCC permissions are tracked by code signing identity; a fixed certificate (`Ava Permissions Helper Code Signing`) + fixed bundle id (`com.ava.permissions-helper`, one grant shared across clusters) avoids re-prompting for permissions on each rebuild.
- **Never ad-hoc**: `codesign --sign -` mints a throwaway identity per build, so every rebuild drops the grants. A locked login keychain (the norm over SSH) therefore fails the build with an unlock instruction instead of downgrading; only a real rebuild consults the keychain, so an up-to-date host converges over SSH unaffected.
- **The signing key must be usable without a human**: an unlocked keychain is not enough — a key whose access control is not satisfied makes macOS raise a SecurityAgent dialog, which a headless converge (SSH / detached session) can never answer. A pre-sign probe signs a throwaway scratch file under a short bound and refuses the build with the ACL remedy (Keychain Access → allow all applications, or `security set-key-partition-list`) rather than waiting. Like the keychain check, it runs only on a real rebuild.
- The first-time authorization in System Settings is a one-time manual step (OS forces human click).

## Three Components
- `helper/main.swift` — the Swift daemon body (+ `helper/Info.plist`). Wire method names: `ping` (with `CGPreflightScreenCaptureAccess` preflight), `screencapture_region`, `click`, `type` (the Python client function is named `type_text`, but the wire method sent is `type`, `client.py:104`/`main.swift:238`), `key`, `scroll`, `ax_window_info`, `window_info`, `session_info`.
- `client.py` — Python client. Connects to the local cluster helper via Unix socket, each call one line JSON request/response; `PermissionsHelperError` represents unreachable/timeout/remote error. Also holds `check_screen_capture()`, the one interpreted call: it turns `ping().preflight_screen` into a `shared.screen_capture.ScreenCaptureStatus`. Because the helper is the process that runs `screencapture_region`, its grant is the only one that decides the answer — a preflight inside the caller would report whatever grant the caller inherited (none, for a detached session started over SSH). Three states, never collapsed: available / no_grant / helper_unreachable (grant unread, so a launchd problem, not a permissions one).
- `lifecycle.py` — build + codesign + launchd management, all idempotent: ensure certificate → compile main.swift + sign .app with that certificate → write per-home LaunchAgent plist and (re)load. Skips compile+sign when the bundle is current AND already signed by the stable certificate, so a no-op converge never churns the cdhash; an ad-hoc-signed leftover fails that check and is re-signed onto the stable identity. The launchd job label is `<bundle-id>.<home_slug>` (`home_slug()` = basename + 8-hex hash; under path-only cluster identity, no longer uses cluster name). Every shell-out (swiftc / codesign / security / launchctl / openssl) is bounded via `shared.proc.run_bounded` with a per-tool ceiling in `_TIMEOUTS_S` — these are local tools with no network leg, so a long-running one is waiting on a GUI prompt, and the bound turns that into a failed converge step instead of a stalled rollout.

## Key Dependencies
- [[tool-calls.ava.okf.md]] — skills that drive the desktop call this helper via `services.permissions_helper.client`
- [[../../cli/cli.ava.okf.md|CLI/converge]] — the converge phase (`cli/commands/_converge.py:_ensure_permissions_helper`) builds+signs+loads during `ava start`/`ava update`; the following `_ensure_screen_capture` step probes the helper's grant and records it for the next agent startup to report

## Entry Points
- `services/permissions_helper/lifecycle.py` — bring-up called by converge
- `services/permissions_helper/client.py` — Python-side call entry
- `services/permissions_helper/helper/main.swift` — Swift daemon

## Notes
- macOS + Windows; configuration gate `AVA_PERMISSIONS_HELPER_ENABLED`, capability probe `shared.platform_probes.permissions_helper_incapability` (macOS: swift/codesign/display; Windows: csc.exe — the helper's session capability is checked at runtime, converge runs in Session 0).
- Windows: C# helper (`services/permissions_helper/windows/helper.cs`, built with the .NET Framework csc.exe every Windows install ships; DPI-aware via SetProcessDPIAware so click coordinates are physical pixels), served over the named pipe `\\.\pipe\ava-permissions-helper`, registered as the logon scheduled task `AvaPermissionsHelper` (`/IT` so it starts in the user's interactive session). Client dials the pipe automatically (`_IS_WINDOWS` transport switch in `client.py`).
- Not in the session/watchdog service roster — launchd handles keepalive, a different layer from other background services.
