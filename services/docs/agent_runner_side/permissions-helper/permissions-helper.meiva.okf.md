---
type: doc
title: Permissions Helper — macOS Desktop Automation Daemon
description: A signed Swift daemon on agent-runner that holds Screen Recording / Accessibility permissions — receives JSON requests via Unix socket and performs privileged desktop operations such as screenshot, click, drag, type, window geometry, and whitelisted file reads on behalf of all skills. macOS only.
tags: []
---

# Permissions Helper — macOS Desktop Automation Daemon

## What is it
A signed Swift `.app` owned by launchd holds macOS Screen Recording and
Accessibility grants and is the required ancestor of `meiva-root` on every macOS
role. Desktop and protected-file skills call its Unix socket. Root diagnostics
observe its protocol and native job state; they never reload their ancestor.

**Role affiliation**: the macOS ancestry owner is required for gateway and runner
roles. Desktop capability probes remain distinct from service ownership. The
helper is outside the application manifest because it owns root's lifetime.

## Why launchd + Stable Signing is Required
The launchd-ownership and stable-signing constraint set moved to its own node: [[launchd-and-stable-signing.meiva.okf.md]].

## Authorization Model

The two grants, the planes they are read on, the helper-owned request
path and the signing rule moved to their own node: [[authorization.meiva.okf.md]].

## Three Components
- `helper/main.swift` — the Swift daemon body (+ `helper/Info.plist`). A socket-less launch (no `MEIVA_PERMISSIONS_HELPER_SOCKET`, no argv[1]) becomes the user-facing panel instance rather than exiting: [[panel.meiva.okf.md|panel mode]]. Wire method names: `ping` (with `preflight_screen`, `ax_trusted`, and the `*_v1` capability flags), `request_grants`, `screencapture_region`, `file_list`, `file_read`, `click`, `drag`, `type` (the Python client function is named `type_text`, but the wire method sent is `type`), `key`, `scroll`, `ax_window_info`, `ax_tree` (bounded read-only accessibility walk of one window, advertised as `ping.ax_tree_v1`), `ax_act` (press / show_menu / focus / set_value on an element of the latest walk, advertised as `ping.ax_act_v1`; `ping.ax_act_v2` adds reported platform actions and literal text selection), `window_info`, `session_info`. Accessibility-gated methods are explicitly refused when the helper lacks that grant. File access is limited to `~/Downloads`, `~/Desktop`, and `~/.meiva/incoming`; both the requested path and roots are symlink-resolved, then checked as the exact root or the root plus a `/` boundary. `file_list` returns sorted entry metadata; `file_read` returns base64 content for regular files up to 32 MiB.
- `wire.py` — typed helper results, explicitly re-exported by `client.py` for existing consumers.
- `client.py` — Python client. Connects to the local cluster helper via Unix socket, each call one line JSON request/response; `PermissionsHelperError` represents unreachable/timeout/remote error. Its `list_dir()` and `read_file()` wrappers expose the whitelisted file operations. `check_screen_capture()` turns `ping().preflight_screen` into a `base.host.converge.screen_capture.ScreenCaptureStatus`; `check_accessibility()` turns `ping().ax_trusted` into a `base.host.converge.accessibility.AccessibilityStatus`; `request_grants()` is the wire call above. Each result keeps grant denial distinct from helper unreachability.
- `lifecycle.py` — bounded certificate checks, compilation, stable signing, and
  initial LaunchAgent registration. A current signed artifact is reused
  (`build_state.py` records its content hash and designated requirement). A
  stale installed artifact is rebuilt in place only once `meiva stop` retired this
  home's helper (`launchd_job.require_retired_helper`); an explicit
  `MEIVA_PERMISSIONS_HELPER_ARTIFACT_DIR` artifact is immutable. Boundary:
  [[launchd-and-stable-signing.meiva.okf.md]]. A loaded changed job is refused
  before any plist write. Isolated preview artifacts never overwrite the normal
  home or production bundle. IPC paths must fit Darwin's 103-byte usable Unix
  socket name limit before signing or native registration.


## Root seeding
The helper also seeds `meiva-root` (`root_seed` / `root_status` / `root_stop`): [[root-seeding.meiva.okf.md]].

## Key Dependencies
- [[tool-calls.meiva.okf.md]] — skills that drive the desktop call this helper via `base.host.permissions_helper.client`
- [[cli/docs/cli.meiva.okf.md|CLI/converge]] — the converge phase (`cli/commands/lifecycle/converge_host.py:_ensure_permissions_helper`) builds+signs+loads during `meiva start`; the following `_ensure_screen_capture` and `_ensure_accessibility` steps probe both helper grants and record unavailable statuses for the next agent startup to report

## Entry Points
- `services/desktop/permissions_helper/lifecycle.py` — bring-up called by converge
- `services/desktop/permissions_helper/launchd_job.py` — the launchd job surface (label/plist/`launchctl print` read + parse, retirement, replacement guard) shared by lifecycle and the helper healthcheck
- `base/host/permissions_helper/client.py` — Python-side call entry
- `services/desktop/permissions_helper/helper/main.swift` — Swift daemon
- `scripts/host_ops/tcc/preauth.sh` — read-only helper/TCC diagnostics and manual grant list

## Notes
- macOS only; configuration gate `MEIVA_PERMISSIONS_HELPER_ENABLED`, capability probe `base.host.system.probes.permissions_helper_incapability` (swift/codesign/display).
- Outside `ServiceSpec`: launchd owns helper keepalive. Root records read-only protocol/job diagnostics; no diagnostic may repair, re-sign, bootout, or force-restart its ancestor.

The `drag` wire method takes finite numeric `start_x`, `start_y`, `end_x`, and
`end_y` in logical points. It posts a move, left-button down, twelve dragged
movements at 10 ms intervals, and left-button up. All events are allocated before
any is posted; a local `defer` posts the release after the drag. Boolean and
nonfinite values are rejected. The result carries `start` and `end` point objects.

Mouse buttons/counts/modifier chords and bounded holds are advertised as
`ping.native_input_v1`: [[../computer-mcp/native-input.meiva.okf.md]]. Desktop-changing
methods stop with `error_code: "user_abort"` while the pointer is parked at the main
display's top-left corner (`ping.corner_failsafe_v1`). The helper marks the window each input
lands on with the system capture badge (`ping.capture_indicator_v1`;
`indicator_stop` ends it: [[../computer-mcp/observation-frames.meiva.okf.md]]). Clipboard
(`ping.clipboard_v1`): [[../computer-mcp/clipboard.meiva.okf.md]]; held mouse buttons
(`ping.mouse_hold_v1`): [[../computer-mcp/mouse-hold.meiva.okf.md]]. The
`move`, `cursor_position`, `list_apps`, `list_windows`, `focus_app` and
`screencapture_window` methods use the same single-call JSON transport.

Tests: [[native-test-evidence]].
