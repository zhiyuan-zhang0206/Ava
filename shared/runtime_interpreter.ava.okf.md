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
