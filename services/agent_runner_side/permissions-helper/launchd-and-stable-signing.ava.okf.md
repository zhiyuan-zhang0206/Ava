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
- The first-time authorization in System Settings is a one-time manual step (OS forces human click).


## Artifact and activation boundary

A loaded helper is never upgraded in place. A differing existing artifact or
loaded job causes refusal. A reviewed replacement is built into a fresh explicit
artifact directory with the same stable certificate and designated requirement.
Activation requires an external exact-home stop/unregister; descendants cannot
replace their permission ancestor. The optional artifact directory is host-local
configuration, including for isolated previews; it may not overlap the ordinary
home or production helper directories. A signing failure never falls back to
ad-hoc identity.
