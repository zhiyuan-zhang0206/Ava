---
type: doc
title: Permissions Helper — macOS Desktop Automation Daemon
description: A signed Swift daemon on agent-runner that holds Screen Recording / Accessibility permissions — receives JSON requests via Unix socket and performs privileged desktop operations such as screenshot, click, type, window geometry, and whitelisted file reads on behalf of all skills. macOS only.
tags: []
---

# Permissions Helper — macOS Desktop Automation Daemon

## What is it
A signed Swift `.app` owned by launchd holds macOS Screen Recording and
Accessibility grants and is the required ancestor of `ava-root` on every macOS
role. Desktop and protected-file skills call its Unix socket. Root diagnostics
observe its protocol and native job state; they never reload their ancestor.

**Role affiliation**: the macOS ancestry owner is required for gateway and runner
roles. Desktop capability probes remain distinct from service ownership. The
helper is outside the application manifest because it owns root's lifetime.

## Why launchd + Stable Signing is Required
The launchd-ownership and stable-signing constraint set moved to its own node: [[launchd-and-stable-signing.ava.okf.md]].

## Authorization Model

The helper has two independent macOS TCC grants:

- **Screen Recording** authorizes `screencapture_region`; without it, captures show wallpaper or black pixels.
- **Accessibility** authorizes `click`, `type`, `key`, `scroll`, and `ax_window_info`; without it, macOS silently drops synthetic input and denies accessibility-tree reads.

`ping` reports both facts as `preflight_screen` and `ax_trusted`. The Swift dispatch gate refuses every Accessibility-gated operation with an explicit error when `ax_trusted=false`, and triggers the System Settings authorization prompt at most once per 30 seconds. The request never waits for a human response. Converge preflights both grants with `_ensure_screen_capture` and `_ensure_accessibility`, then agent startup reports either unavailable axis (or one combined notice when both fail).

TCC keys grants on the helper's code identity. A stable certificate plus fixed bundle id preserves both grants across rebuilds; ad-hoc signing or a regenerated identity drops them once and the operator must re-grant in System Settings. Accessibility applies to the already running helper immediately. A changed Screen Recording grant may require an externally coordinated stop and helper restart. A descendant must not kickstart its own ancestor.

## Three Components
- `helper/main.swift` — the Swift daemon body (+ `helper/Info.plist`). A socket-less launch (no `AVA_PERMISSIONS_HELPER_SOCKET`, no argv[1]) becomes the user-facing panel instance rather than exiting: [[panel.ava.okf.md|panel mode]]. Wire method names: `ping` (with `preflight_screen` and `ax_trusted`), `screencapture_region`, `file_list`, `file_read`, `click`, `type` (the Python client function is named `type_text`, but the wire method sent is `type`), `key`, `scroll`, `ax_window_info`, `window_info`, `session_info`. Accessibility-gated methods are explicitly refused when the helper lacks that grant. File access is limited to `~/Downloads`, `~/Desktop`, and `~/.ava/incoming`; both the requested path and roots are symlink-resolved, then checked as the exact root or the root plus a `/` boundary. `file_list` returns sorted entry metadata; `file_read` returns base64 content for regular files up to 32 MiB.
- `client.py` — Python client. Connects to the local cluster helper via Unix socket, each call one line JSON request/response; `PermissionsHelperError` represents unreachable/timeout/remote error. Its `list_dir()` and `read_file()` wrappers expose the whitelisted file operations. `check_screen_capture()` turns `ping().preflight_screen` into a `shared.host.converge.screen_capture.ScreenCaptureStatus`; `check_accessibility()` turns `ping().ax_trusted` into a `shared.host.converge.accessibility.AccessibilityStatus`. Each result keeps grant denial distinct from helper unreachability. On Windows, the absent `ax_trusted` wire key means Accessibility is granted because `SendInput` is not TCC-gated.
- `lifecycle.py` — bounded certificate checks, compilation, stable signing, and
  initial LaunchAgent registration. A current signed artifact is reused. An
  existing artifact with different source is immutable: prepare a new explicit
  `AVA_PERMISSIONS_HELPER_ARTIFACT_DIR`, then externally stop and unregister the
  old exact-home job before activation. A loaded changed job is refused before
  any plist write. Isolated preview artifacts never overwrite the normal
  home or production bundle. IPC paths must fit Darwin's 103-byte usable Unix
  socket name limit before signing or native registration.


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
- Outside `ServiceSpec`: launchd owns helper keepalive. Root records read-only protocol/job diagnostics; no diagnostic may repair, re-sign, bootout, or force-restart its ancestor.
