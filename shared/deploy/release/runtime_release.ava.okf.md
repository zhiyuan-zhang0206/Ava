---
type: doc
title: Runtime release store
description: Private generation verification against the host's ABI tag and dormant atomic selection, with explicit input and installed-manifest identities.
tags:
- shared
- runtime
---

# Runtime release store

Active `.pth` files are rejected except the standard setuptools helper whose
bytes and `_distutils_hack` module match a retained copy of the original locked
wheel. The builder must copy that wheel from its verified input set; the full
image manifest protects the evidence wheel too. Installed RECORD alone is not
authority. Inert `.pth` fixtures outside site directories are not startup hooks.
Actual source-absent launch must also verify helper import origin and sys.path.

`runtime_release.py` is a dormant, config-free verification and pointer primitive.
No production service consumes it. Existing checkout activation is unchanged.

An input artifact digest selects a generation directory. Installation happens at
that final inactive path so absolute venv shebangs do not become stale. A separate
installed-manifest SHA256, retained by the installer/caller, verifies the ABI tag,
schema identity and a complete file inventory. The installed manifest is not
self-addressed: hashing shebangs containing their own generation hash would be
circular. A new installation must use a new artifact/generation identity, never
overwrite an existing generation.

Verification rejects symlinks, multiply-linked files, editable/path injection
metadata, missing/extra/corrupt members, unsafe paths, a different schema and an
ABI tag the observed host cannot run. Private-copy installation is required. It
returns absolute interpreter/cwd paths captured once; consumers must not execute
through a moving pointer. `module_argv()` constructs shell-free isolated Python
commands, disables bytecode writes and selects UTF-8 without spawning or
changing lifecycle state.
A bounded cross-platform file lock and expected-predecessor comparison
serialize atomic pointer replacement. Failure leaves the prior pointer intact.

## ABI contract

Manifest version 2 carries `abi_tag`, defined once in the stdlib-only
`runtime_abi.py`: `os` (`linux`/`macos`), `arch`, `libc` + `libc_version` (glibc
floor, Linux), `macos` major + `macos_deployment_target` (macOS), and the Python
ABI as `python` (`sys.implementation.cache_tag`, CPython only) + `abiflags`.
Preparation records it by running `current_abi()` inside the image's own
interpreter, so the floors are the preparing host's glibc or macOS major. The
full `platform.platform()` string stays in the manifest as provenance and is
never compared.

Every verifier passes `host_abi=current_abi()`, read fresh in its own process:
boot, selection (`activate_release`), stage preflight/observation, loaded-image
start and preparation readback. `ReleaseRef.verify` does this itself; no request
captures a platform value. Compatible means equal `os`, `arch`, `libc`, `python`
and `abiflags`, a host glibc at or above the image floor, and a host macOS major
at or above the image major. Kernel, OS patch and minor updates, and glibc or
macOS major upgrades keep retained images (including the rollback target)
bootable. A different architecture or Python ABI, or a host below the floor,
needs a re-prepared image; since the verifier runs on the host's own runtime, a
host keeps one Python ABI. Unknown operating systems, a non-glibc Linux libc,
macOS before 11, other interpreters, unknown or extra tag fields, and a manifest
without `abi_tag` (version 1) refuse; there is no compatibility path.

This is not a supervisor, installer, migration runner, credential store or GC.
The future official caller must hold the rollout lease, verify actual executable
imports and external interpreter/stdlib/native-library dependencies, observe the
schema, and stop legacy writers before enabling generation-aware capabilities.
No claim of fully self-contained/offline rollback is made yet. OS ownership
separation is still needed against an agent deliberately modifying its own image.

CI exercises real temporary-file transitions on Linux, macOS and Windows without
starting a cluster or installing the project environment, so `runtime_release.py`,
`runtime_abi.py` and `runtime_prepare.py` import only the standard library
(`tests/lifecycle/images/test_stdlib_boundary.py` guards the chain and runs a
tool). Preparation tools run through the stdlib-only
[[process-group-closure.ava.okf.md|group-closure core]]: each leads its
own process group, and its leader stays unreaped until a group-wide SIGKILL and a
kernel listing of only that leader prove closure. The builder-embedded
application identity and its verified read live in `release_identity.py`.
Current runtime consumers are not wired until packaging and resource closure,
recovery and old-orchestrator bootstrapping gates are complete.
