---
type: doc
title: Install Registry Write Path
description: The single write path — mutate() under registry_lock, the register/deregister wrappers, the three bulk-edit cycles, and why migrate_skill_identity takes the lock explicitly.
tags:
- shared
- install-registry
---

# Install Registry Write Path

## One way to write it

Every change goes through `shared/install_registry.py:mutate()`, which loads the
registry, hands it over for edits, and saves it back under `registry_lock` — a
bounded advisory lock on the sibling `installed.json.lock`. `register` /
`deregister` are thin wrappers over it, and the three cycles that edit rows in
bulk — the gateway's skills-toggle handler, `ava skill update`, and skills
converge — open it directly. `scripts/migrate_skill_identity.py --apply` cannot
use `mutate` (it rewrites a registry under an arbitrary `--ava-home`), so it
takes `registry_lock` explicitly; that is the only writer outside this module.

The lock is what the file needs that atomic saving does not give it: `save` is a
full replace, so two writers in different processes (an agent running
`ava skill install`, a restart running converge, the panel toggling a skill) each
publish a registry read before the other's rows existed, and one side's packages
stop being tracked while their directories sit on disk. `save` also stages
through ONE fixed temp name, so two overlapping saves corrupt each other's
staging outright rather than merely losing a row.

A `mutate` body must not open a second cycle — the lock is not re-entrant, so a
nested one contends with its own outer take. It raises `LockTimeoutError` rather
than deadlocking, but only after waiting out the full 30s bound: the failure is
bounded and loud, not fast. Catch it in review or a test, not by watching prod.


Parent: [[shared/install_registry/install_registry.ava.okf.md|install registry]].
