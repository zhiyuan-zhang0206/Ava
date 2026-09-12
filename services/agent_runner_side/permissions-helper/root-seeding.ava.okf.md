---
type: doc
title: "Root seeding — the helper as the macOS ava-root seeder"
description: "The macOS adapter side of the lifecycle two-section design (#3195): a seed file makes the permissions helper spawn ava-root as its direct child and keep it alive with bounded backoff; single instance rides on the root's own flock (a held lock rests the keeper in conflict — nothing spawned, nothing killed), and root_seed / root_status / root_stop are the wire methods."
tags:
- services
- permissions-helper
- lifecycle
- root-supervisor
---

# Root seeding

The lifecycle design (#3195) splits macOS into two sections: launchd → helper (the stable TCC identity) and helper → **ava-root** (the platform-neutral supervisor). The helper implements the adapter side (task #3209). With a seed file named by `AVA_PERMISSIONS_HELPER_ROOT_SEED`, it spawns `ava-root` as its **direct child** (posix_spawn, SETSID | CLOEXEC — a session boundary only, ppid unchanged, no reparent) and keeps it alive: an unexpected exit restarts it with bounded backoff, while a stop the helper requested does not restart. Single instance rides on the root's own `ava-root.lock`: before every spawn the keeper probes it non-blockingly, and a held lock means another live root owns the run dir — the keeper then rests in `conflict` (nothing spawned, nothing killed, "lose attribution, not service") until the dir frees or an explicit `root_stop(force: true)` disposes the foreign root; a freed run dir is seeded again. The wire surface is `root_seed` / `root_status` / `root_stop`; without a seed the keeper is dormant. Production converge does not write a seed yet — the path is exercised by `scripts/two_section_chain_smoke.py` (dev hosts and the macOS CI lane).
