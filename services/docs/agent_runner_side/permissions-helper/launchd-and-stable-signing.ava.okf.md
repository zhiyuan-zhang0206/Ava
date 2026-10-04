---
type: doc
title: "Permissions Helper — launchd ownership and stable signing constraints"
description: "Why the macOS permissions helper must be launchd-owned and signed with a stable certificate: process identity for TCC grants, the never-ad-hoc policy, headless signing probes, and the one-time manual authorization."
tags:
- services
- permissions-helper
- lifecycle
---

# Permissions Helper — launchd ownership and stable signing constraints

## Why launchd + Stable Signing is Required
- **launchd launch**: the helper must be its own responsible process to independently hold permission grants, rather than borrowing the grant from the terminal that launched it.
- **Stable self-signed certificate**: TCC permissions are tracked by code signing identity; a fixed certificate (`Ava Permissions Helper Code Signing`) + fixed bundle id (`com.ava.permissions-helper`, one grant shared across clusters) avoids re-prompting for permissions on each rebuild.
- **Never ad-hoc**: `codesign --sign -` mints a throwaway identity per build, so every rebuild drops the grants. A locked login keychain (the norm over SSH) therefore fails the build with an unlock instruction instead of downgrading; only a real rebuild consults the keychain, so an up-to-date host converges over SSH unaffected.
- **The signing key must work headlessly**: an unlocked keychain can still block on a SecurityAgent ACL prompt. On real rebuilds only, a short scratch-sign probe diagnoses that prompt and names the ACL remedy; the following hard smoke signs a scratch file with the production designated requirement, reads it back through `codesign`, and rejects any signing, output, parse, or identity failure before compilation.
- **Hardened runtime**: the helper is signed with `--options runtime` and no dyld or library-validation exception, so dyld ignores `DYLD_*` from the launchd session (for example `launchctl setenv`) and maps only platform libraries; without it an inserted library runs before `main` and the running image still satisfies `codesign -R`. Release admission requires the flag on the file and in the running image's kernel status (`csops`). The designated requirement, and the TCC grants keyed on it, are unchanged; the signing options are part of the build input hash, so a policy change is a new artifact.
- **AppleEvents entitlement**: the helper is signed with `com.apple.security.automation.apple-events` (`helper/helper.entitlements`), the entitlement that lets tccd build an attribution chain for helper-spawned `osascript` / AppleEvents children; without it such a request dies as `-1712` (exec) / `-609` (pty) before any dialog can appear. The entitlements file is part of the build input hash, so changing it is a new artifact; the designated requirement, and the TCC grants keyed on it, stay unchanged.
- The first-time authorization in System Settings is a one-time manual step (OS forces human click).


## Artifact and activation boundary

A loaded helper is never upgraded in place: a differing loaded job, or a stale
installed artifact whose home job is still loaded, whose plist is still
registered, or whose executable a live process still runs (resolved-path match),
causes refusal (`launchd_job.require_retired_helper`, checked before the signing
probes and again right before removal). After `ava stop` retired the exact-home
job, the next start rebuilds the stale `$AVA_HOME/helper` artifact in place with
the same stable certificate and designated requirement, so TCC grants carry over
(stderr says so; a regenerated identity warns instead). Keychain, ACL probe,
signing smoke and compile all finish before the old artifact is removed, so a
host that cannot sign keeps it. Descendants cannot replace their permission
ancestor. An artifact in the optional explicit artifact directory is
immutable: a reviewed replacement goes into a fresh directory. That directory is
host-local configuration, including for isolated previews; it may not overlap
the ordinary home or production helper directories. A signing failure never
falls back to ad-hoc identity.
