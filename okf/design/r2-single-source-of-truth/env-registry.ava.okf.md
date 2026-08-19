---
type: doc
title: R2 Env Registry — Convergence Point A
description: EnvRegistry — every env key declared exactly once; forwarding/keep-drop/seed-allowlist as pure projections of class metadata; invariants A1-A3.
tags:
- okf-design
- r2
---

# R2 Env Registry — Convergence Point A

## Env keys — `EnvRegistry` (declaration registry, see [[okf/design/design.ava.okf.md|lexicon]])

Every env key is declared exactly once: Settings fields declare metadata at the class declaration (`json_schema_extra`); passthrough keys (PATH/TMPDIR/VIRTUAL_ENV/DISPLAY/HOME/Windows system keys) are registered rows. The registry builds from pydantic **class metadata only** (no Settings instantiation — decoupled from `dotenv_boot` timing). Three operation families become **projections** of the registry:

- **Forwarding** (`child_env(role, platform)`) — the parent→child env view. `role` reuses the existing `AVA_PROCESS_PROFILE` (gateway/agent/runner) — no new enum. POSIX delivery stays the 0600 env-file prefix; Windows delivers a dict. argv env-splices stay forbidden (secrets never ride argv).
- **Keep/drop** (`env_authority_drop_set(role)` / `env_keep_set(role)`) — dotenv_boot's own-environ surgery, as set-membership queries.
- **Seed allowlist** (`seed_allowlist()`) — the install-time file-copy whitelist.

Pure test-fixture sets are deleted outright. The authority for derivation is the **consumption matrix** (which process kind actually reads which keys — #1570's lesson); capability/scope metadata only validates.

Invariants: A1 every key registered exactly once (no duplicates, no orphans); A2 every projection is a pure function of the registry; A3 **one metadata line = every projection updates** (the six-gap class becomes structurally impossible).

