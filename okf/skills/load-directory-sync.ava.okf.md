---
type: doc
title: Skill sources — load-directory sync
description: One load directory ~/.ava/skills/; converge syncs repo built-ins (ava_builtins/skills/) and plugin-carried skills into it, and user installs land directly (untouched). The 11 real .agents/skills project skills are NOT converged — they reach agents through the project-local mount.
tags:
- extensions
- agent-instruction
---

# Skill sources (load-directory sync)

One load directory: `~/.ava/skills/` (gated by the install registry's enabled
flag). Converge (`cli/commands/_converge_skills.py`, on `ava start` /
`ava cluster update` / `ava converge`) syncs two source types into it:

1. **Repo built-in** (origin=repo): `<repo>/ava_builtins/skills/` →
   `~/.ava/skills/<name>/`. Repo-native sources are bootstrap-only:
   converge lands a missing copy and never updates one (the R5 ruling).
   Updating an existing copy belongs to the **content channel**
   (`ava packages refresh`, registry schema v2): a repo-native row resolves to
   the `core` channel by default and is refreshed from the checkout's remote on
   its own cadence — commit objects fetched without ever touching the
   checkout's working tree. `ava skill update` and the rollout legs (issue
   #1289) now SKIP channel-managed packages; `ava packages policy <name>
   --update-mode off` opts one back onto the rollout path. Local edits are
   never clobbered either way: refresh records a conflict and leaves the copy
   untouched, the same contract the rollout always had.
2. **Plugin-carried** (origin=plugin): `<repo>/ava_builtins/plugins/<p>/skills/`
   and `~/.ava/plugins/<p>/skills/` → `~/.ava/skills/<p>/`.

User-installed packages (origin=user): `ava skill install` drops directly into
`~/.ava/skills/` (untouched by converge); a hand-placed dir needs
`ava skill register`.

**The content channel adds two more pieces to this contract** (design §5.5 /
§5.6; [[cli/commands/packages/update-policy.ava.okf.md|update policy &
channels]]):

- the **refresh pass** is the load directory's fourth bulk writer (after
  converge, `ava skill update`, and the gateway's skills toggle). It stages
  swaps the same way converge does (`.<name>.new` → swap; marker-protected
  subtrees carried; the replaced tree kept at `.<name>.prev` for
  `ava packages rollback`), and writes the registry through one `mutate` at
  the end of the pass;
- the **runtime host filter**: the skill scan mounts `loadable_skill_names()`
  — enabled entries whose manifest host contract passes — so a package whose
  `engines.ava` / `requires_commit` excludes the running host is dropped from
  the catalog/index, with the reason visible in `ava packages status`.

**Not converged — the 11 real `.agents/skills/` project skills.** The
repo-development workflow and Ava-cluster-operations family (ship-a-change,
write-a-pr-description, ava-self-development, …) stopped being
fleet-distributed (issue #146;
`decisions/2026-08-20-stop-fleet-distributing-kernel-contributor-skills.md`,
resolving the open point in
`decisions/2026-08-19-four-layer-modification-model.md`). A converge pass
treats them as gone sources: untouched copies they used to land are removed
and deregistered, so runtime agents' indexes lose the L4 noise. They reach
agents only through the project-local mount — see
[[okf/skills/project-local.ava.okf.md]].

The external-agent operator bridge is outside this load-directory contract. A
prod host-global converge step projects exactly `operating-ava-cluster` into an
already-present Codex or Claude Code home; it neither registers that copy in
Ava's install registry nor restores `.agents/skills/` as an Ava runtime source.
See [[okf/skills/external-agent-operator-bridge.ava.okf.md]].

General methodology and user-service skills — `ava-serious-engineering`,
`ava-serious-research`, `ava-deep-research`, `ava-corp`, and
`telegram-send-file` — are repo built-ins, so this first source lands them in
the load directory and makes them available to the gateway's command index.
Their `.agents/skills/` entries are open-standard symlink mirrors, not
project-skill sources.
