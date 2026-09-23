---
type: doc
title: Permissions Helper — macOS Desktop Automation Daemon
description: A signed Swift daemon on agent-runner that holds Screen Recording / Accessibility permissions — receives JSON requests via Unix socket and performs privileged desktop operations such as screenshot, click, type, window geometry, and whitelisted file reads on behalf of all skills. macOS only.
tags: []
---

# Permissions Helper — macOS Desktop Automation Daemon

## What is it
A macOS permissions helper on agent-runner — a signed Swift `.app` owned by **launchd**, outside the session roster, holding Screen Recording / Accessibility permissions. The agent-runner watchdog pings its real protocol, classifies the launchd job state on failure (an LWCR/EX_CONFIG spawn-failed loop is named; a failed repair escalates and retries under backoff), and can reload its launchd job. The root era watches the same job through a total probe (task #3393). Desktop-driving and protected-file skills call it via Unix socket, centralizing privileged actions in this process.

**Role affiliation**: agent-runner side (macOS only) — launched by launchd, not in the session service roster (`build_services`), with capability probe `permissions_helper_incapability` gating.

## Why launchd + Stable Signing is Required
The launchd-ownership and stable-signing constraint set moved to its own node: [[launchd-and-stable-signing.ava.okf.md]].

## Authorization Model

The helper has two independent macOS TCC grants:

- **Screen Recording** authorizes `screencapture_region`; without it, captures show wallpaper or black pixels.
- **Accessibility** authorizes `click`, `type`, `key`, `scroll`, and `ax_window_info`; without it, macOS silently drops synthetic input and denies accessibility-tree reads.

`ping` reports both facts as `preflight_screen` and `ax_trusted`. The Swift dispatch gate refuses every Accessibility-gated operation with an explicit error when `ax_trusted=false`, and triggers the System Settings authorization prompt at most once per 30 seconds. The request never waits for a human response. Converge preflights both grants with `_ensure_screen_capture` and `_ensure_accessibility`, then agent startup reports either unavailable axis (or one combined notice when both fail).

TCC keys grants on the helper's code identity. A stable certificate plus fixed bundle id preserves both grants across rebuilds; ad-hoc signing or a regenerated identity drops them once and the operator must re-grant in System Settings. Accessibility applies to the already running helper immediately. Screen Recording needs `launchctl kickstart -k` for the helper's launchd job after it is granted.

## Three Components
- `helper/main.swift` — the Swift daemon body (+ `helper/Info.plist`). A socket-less launch (no `AVA_PERMISSIONS_HELPER_SOCKET`, no argv[1]) becomes the user-facing panel instance rather than exiting: [[panel.ava.okf.md|panel mode]]. Wire method names: `ping` (with `preflight_screen` and `ax_trusted`), `screencapture_region`, `file_list`, `file_read`, `click`, `type` (the Python client function is named `type_text`, but the wire method sent is `type`), `key`, `scroll`, `ax_window_info`, `window_info`, `session_info`. Accessibility-gated methods are explicitly refused when the helper lacks that grant. File access is limited to `~/Downloads`, `~/Desktop`, and `~/.ava/incoming`; both the requested path and roots are symlink-resolved, then checked as the exact root or the root plus a `/` boundary. `file_list` returns sorted entry metadata; `file_read` returns base64 content for regular files up to 32 MiB.
- `client.py` — Python client. Connects to the local cluster helper via Unix socket, each call one line JSON request/response; `PermissionsHelperError` represents unreachable/timeout/remote error. Its `list_dir()` and `read_file()` wrappers expose the whitelisted file operations. `check_screen_capture()` turns `ping().preflight_screen` into a `shared.host.converge.screen_capture.ScreenCaptureStatus`; `check_accessibility()` turns `ping().ax_trusted` into a `shared.host.converge.accessibility.AccessibilityStatus`. Each result keeps grant denial distinct from helper unreachability. On Windows, the absent `ax_trusted` wire key means Accessibility is granted because `SendInput` is not TCC-gated.
- `lifecycle.py` — build + codesign + launchd management, all idempotent: ensure certificate → preflight real signing and designated-requirement recovery when a rebuild is needed → compile main.swift, copy `helper/locales/*.lproj` into `Contents/Resources`, and sign .app with that certificate → write per-home LaunchAgent plist and (re)load. Skips compile+sign and the signing smoke when the bundle is current AND already signed by the stable certificate, so a no-op converge never churns the cdhash or requires keychain access; an ad-hoc-signed leftover fails that check and is re-signed onto the stable identity. The launchd job label is `<bundle-id>.<home_slug>` (`home_slug()` = basename + 8-hex hash; under path-only cluster identity, no longer uses cluster name). Every shell-out (swiftc / codesign / security / launchctl / openssl) is bounded via `shared.proc.run_bounded` with a per-tool ceiling in `_TIMEOUTS_S` — these are local tools with no network leg, so a long-running one is waiting on a GUI prompt, and the bound turns that into a failed converge step instead of a stalled rollout.

## Root seeding
The helper also seeds `ava-root` (`root_seed` / `root_status` / `root_stop`): [[root-seeding.ava.okf.md]].

## Key Dependencies
- [[tool-calls.ava.okf.md]] — skills that drive the desktop call this helper via `services.permissions_helper.client`
- [[../../cli/cli.ava.okf.md|CLI/converge]] — the converge phase (`cli/commands/_converge.py:_ensure_permissions_helper`) builds+signs+loads during `ava start`/`ava update`; the following `_ensure_screen_capture` and `_ensure_accessibility` steps probe both helper grants and record unavailable statuses for the next agent startup to report

## Entry Points
- `services/permissions_helper/lifecycle.py` — bring-up called by converge
- `services/permissions_helper/launchd_job.py` — the launchd job surface (label/plist/`launchctl print` read + parse) shared by lifecycle and the helper healthcheck
- `services/permissions_helper/client.py` — Python-side call entry
- `services/permissions_helper/helper/main.swift` — Swift daemon
- `scripts/tcc-preauth.sh` — read-only helper/TCC diagnostics and manual grant list

## Notes
- macOS + Windows; configuration gate `AVA_PERMISSIONS_HELPER_ENABLED`, capability probe `shared.platform_probes.permissions_helper_incapability` (macOS: swift/codesign/display; Windows: csc.exe — the helper's session capability is checked at runtime, converge runs in Session 0).
- Windows: C# helper (`services/permissions_helper/windows/helper.cs`, built with the .NET Framework csc.exe every Windows install ships; DPI-aware via SetProcessDPIAware so click coordinates are physical pixels), served over the named pipe `\\.\pipe\ava-permissions-helper`, registered as the logon scheduled task `AvaPermissionsHelper` (`/IT` so it starts in the user's interactive session). Client dials the pipe automatically (`_IS_WINDOWS` transport switch in `client.py`).
- Outside `ServiceSpec`: launchd owns keepalive; the agent-runner watchdog adds a DB-free protocol check that classifies launchd failures (LWCR-stuck named), repairs at failure three, and escalates a failed repair with backoff retries (verified again next round).
