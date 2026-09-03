---
type: doc
title: Runtime release store
description: Private generation verification and dormant atomic selection, with explicit input and installed-manifest identities.
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
installed-manifest SHA256, retained by the installer/caller, verifies platform,
schema identity and a complete file inventory. The installed manifest is not
self-addressed: hashing shebangs containing their own generation hash would be
circular. A new installation must use a new artifact/generation identity, never
overwrite an existing generation.

Verification rejects symlinks, multiply-linked files, editable/path injection
metadata, missing/extra/corrupt members, unsafe paths and incompatible observed
platform/schema. Private-copy installation is required. It returns absolute
interpreter/cwd paths captured once; consumers must not execute through a moving
pointer. `module_argv()` constructs shell-free isolated Python commands, disables
bytecode writes and selects UTF-8 without spawning or changing lifecycle state.
A bounded cross-platform file lock and expected-predecessor comparison
serialize atomic pointer replacement. Failure leaves the prior pointer intact.

This is not a supervisor, installer, migration runner, credential store or GC.
The future official caller must hold the rollout lease, verify actual executable
imports and external interpreter/stdlib/native-library dependencies, observe the
schema, and stop legacy writers before enabling generation-aware capabilities.
No claim of fully self-contained/offline rollback is made yet. OS ownership
separation is still needed against an agent deliberately modifying its own image.

CI exercises real temporary-file transitions on Linux, macOS and Windows without
starting a cluster. Current runtime consumers are not wired until packaging and
resource closure, recovery and old-orchestrator bootstrapping gates are complete.
