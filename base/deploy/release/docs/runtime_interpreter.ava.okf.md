---
type: doc
title: Loaded-runtime interpreter binding
description: Interpreter paths and the source identity of the checkout that loaded the running code.
tags:
- shared
- runtime
---

# Loaded-runtime interpreter binding

`runtime_interpreter.py` names the interpreter and environment of the checkout
that loaded the running code: its `.venv`, its plugin discovery root, and the
Git-visible source digest of its files. An explicitly targeted checkout stays
separate (`runtime_venv(checkout=...)`). Nothing here installs packages, reads a
release selector or grants admission.

The consumers include service Python commands, process-agent interpreters,
platform console-script paths, and shell/session activation. Their lifecycle,
enabled-state, credentials, and supervision owners do not change.

The image half of loaded-runtime identity (`WHEEL_RUNTIME`, the retained
frontend/plugin/collector paths and `verify_loaded_image`) lives in
`loaded_image.py`, which no production module imports.

## Loaded code and local birth evidence

The settings-free verifier records an explicit source identity: the canonical
loaded module directory, native interpreter, virtual-environment prefix, expected
cwd, and Git-visible source digest (dirty and untracked nonignored bytes included).
It does not seal ignored build outputs or editable dependencies. `kind="release"`
identities are produced only by the image half.

Deployment root wiring captures this identity before any application child is
started. Its status reports the runtime, canonical home and launch digest with
the native root birth. `base/deploy/lifecycle/start_serving.py` records those facts only after
ordinary startup readiness succeeds. Every serving read compares the receipt
with the live root. POSIX root status authenticates the response against the
kernel Unix-socket peer and exact native birth (Linux start ticks, otherwise
exact stable birth). Missing Linux ticks refuse even when both timestamps match.
A JSON PID, stale marker, missing identity or unreadable peer cannot grant serving.
Full loaded-runtime verification belongs at process admission, not heartbeat or
inbox-claim frequency; ordinary serving/recovery checks only read the marker
and authenticate current native root status.

This is local evidence, not DB publication, a fleet barrier, resource-predecessor
closure or a protocol-version grant. RuntimeAdmission keeps all existing DB and
resource fences. Direct-process E2E fixtures inject their own test-only serving
gate through `tests.e2e._proc`; those business tests exclude root custody proof.
