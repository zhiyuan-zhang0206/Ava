---
type: doc
title: Loaded-runtime interpreter binding
description: Absolute interpreter binding to the loaded wheel generation without release activation.
tags:
- shared
- runtime
---

# Loaded-runtime interpreter binding

`runtime_interpreter.py` distinguishes imported wheel code inside `sys.prefix`
from editable checkout code. Wheel consumers retain that absolute prefix;
development keeps its existing checkout venv. An explicitly targeted checkout
remains separate for the updater's preparation path. Nothing reads or changes
the active-generation pointer, installs packages, or grants release admission.

The consumers include retained frontend and collector executable paths, service Python commands, process-agent interpreters,
platform console-script paths, and shell/session activation. Their lifecycle,
enabled-state, credentials, and supervision owners do not change. Wheel bootstrap
requires explicit absolute `AVA_HOME` before loading configuration; it cannot
silently choose the production default. The production-source launch guard is
deliberately unchanged: this slice does not authorize production activation.

CI prepares a real inactive generation, removes the checkout from its original
path, binds consumer paths to generation A, changes a test selector to B, and
launches a delayed subprocess and exec-child entry-point guard from A. It also
checks missing-home rejection. No agent turn, service, or cluster is started.
The builder's host-closure gates must pass first; platform/build failures are not
consumer proof. Full CLI update cutover, old-orchestrator bootstrapping, all-host
writer fencing, and explicit activation remain separate release gates.


## Loaded code and local birth evidence

The settings-free verifier records an explicit source or image identity. Source
identity binds the canonical loaded module directory, native interpreter,
virtual-environment prefix, expected cwd, and Git-visible source digest (dirty
and untracked nonignored bytes included). It does not seal ignored build outputs
or editable dependencies. Image identity binds the full verified inventory and
the builder's source/schema identity to the actual isolated interpreter and
loaded modules. The builder identity model and inventory reader live in
`runtime_release.py`; construction remains in `cli/release_build.py`.

Deployment root wiring captures this identity before any application child is
started. Its status reports the runtime, canonical home and launch digest with
the native root birth. `shared/start_serving.py` records those facts only after
ordinary startup readiness succeeds. Every serving read compares the receipt
with the live root. POSIX root status authenticates the response against the
kernel Unix-socket peer and exact native birth (Linux start ticks, otherwise
exact stable birth). Missing Linux ticks refuse even when both timestamps match.
A JSON PID, stale marker, missing identity or unreadable peer cannot grant serving. `require_born_runtime()` also checks this caller's
loaded code against that local birth. It does not consult a moving selector.
Full loaded-image verification belongs at process admission, not heartbeat or
inbox-claim frequency; ordinary serving/recovery checks only read the marker
and authenticate current native root status.

This is local evidence, not DB publication, a fleet barrier, resource-predecessor
closure or a protocol-version grant. RuntimeAdmission keeps all existing DB and
resource fences. Direct-process E2E fixtures inject their own test-only serving
gate through `tests.e2e._proc`; those business tests exclude root custody proof.
