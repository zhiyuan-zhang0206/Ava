---
type: doc
title: R2 Env Registry — Convergence Point A
description: EnvRegistry — declared keys, generated metadata, forwarding and keep/drop projections; invariants A1-A3.
tags:
- okf-design
- r2
---

# R2 Env Registry — Convergence Point A

## Env keys — `EnvRegistry` (declaration registry, see [[okf/design/design.ava.okf.md|lexicon]])

Settings fields declare metadata at the class declaration (`json_schema_extra`);
boot-time projections read the generated static index without constructing
Settings. Passthrough keys (PATH/TMPDIR/VIRTUAL_ENV/DISPLAY/HOME, network proxy
configuration and Windows system keys) are registered rows. Enabled provider
plugin bindings declare removable provider keys. Two operation families consume
these declarations:

- **Forwarding** (`child_env(role, platform)`) — the parent→child env view. `role` uses `AVA_PROCESS_PROFILE` (gateway/agent/runner). Native launchers receive an environment dict; secrets never ride argv.
- **Keep/drop** (`env_authority_drop_set(role)` / `env_keep_set(role)`) — dotenv_boot's own-environ surgery, as set-membership queries.

Pure test-fixture sets are deleted outright. The authority for derivation is the **consumption matrix** (which process kind actually reads which keys — #1570's lesson); capability/scope metadata only validates.

Invariants: A1 every key registered exactly once (no duplicates, no orphans); A2 every projection is a pure function of the registry; A3 **one metadata line = every projection updates** (the six-gap class becomes structurally impossible).
