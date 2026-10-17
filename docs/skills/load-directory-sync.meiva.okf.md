---
type: doc
title: Skill sources — load-directory sync
description: One load directory ~/.meiva/skills/; converge syncs repo built-ins (meiva_builtins/skills/) and plugin-carried skills into it, and user installs land directly (untouched). The repo-local .agents/skills project skills are NOT converged — they reach agents through the project-local mount.
tags:
- extensions
- agent-instruction
---

# Skill sources (load-directory sync)

One load directory: `~/.meiva/skills/` (gated by the install registry's enabled
flag). Converge (`cli/commands/extensions/skills_sync.py`, on `meiva start` /
the fleet update / `meiva converge`) syncs two source types into it:

1. **Repo built-in** (origin=repo): `<repo>/meiva_builtins/skills/` →
   `~/.meiva/skills/<name>/`. Repo-native sources are bootstrap-only:
   converge lands a missing copy and never updates one (the R5 ruling).
   Updating an existing copy belongs to the **content channel**
   (`meiva packages refresh`, registry schema v2): a repo-native row resolves to
   the `core` channel by default and is refreshed from the checkout's remote on
   its own cadence — commit objects fetched without ever touching the
   checkout's working tree. `meiva skill update` and the rollout legs (issue
   #1289) now SKIP channel-managed packages; `meiva packages policy <name>
   --update-mode off` opts one back onto the rollout path. A differing local
   copy is replaced (reported; the old tree stays at `.<name>.prev` for
   rollback): local copies are derived state (user ruling 2026-10-03).
2. **Plugin-carried** (origin=plugin): `<repo>/meiva_builtins/plugins/<p>/skills/`
   and `~/.meiva/plugins/<p>/skills/` → `~/.meiva/skills/<p>/`.

User-installed packages (origin=user): `meiva skill install` drops directly into
`~/.meiva/skills/` (untouched by converge); a hand-placed dir needs
`meiva skill register`.

**The content channel adds two more pieces to this contract** (design §5.5 /
§5.6; [[cli/commands/extensions/docs/update-policy.meiva.okf.md|update policy &
channels]]):

- the **refresh pass** is the load directory's fourth bulk writer (after
  converge, `meiva skill update`, and the gateway's skills toggle). It stages
  swaps the same way converge does (`.<name>.new` → swap; marker-protected
  subtrees carried; the replaced tree kept at `.<name>.prev` for
  `meiva packages rollback`), and writes the registry through one `mutate` at
  the end of the pass;
- the **runtime host filter**: the skill scan mounts `loadable_skill_names()`
  — enabled entries whose manifest host contract passes — so a package whose
  `engines.meiva` / `requires_commit` excludes the running host is dropped from
  the catalog/index, with the reason visible in `meiva packages status`.

**Not converged — the real `.agents/skills/` project skills.** The
repo-development workflow and Meiva-cluster-operations family (review-contribution,
meiva-self-development, …) stopped being
fleet-distributed (issue #146;
`docs/decisions/runtime/processes/shutdown/2026-08-20-stop-fleet-distributing-kernel-contributor-skills.md`,
resolving the open point in
`docs/decisions/extensions/skills/2026-08-19-four-layer-modification-model.md`). A converge pass
treats them as gone sources: untouched copies they used to land are removed
and deregistered, so runtime agents' indexes lose the L4 noise. They reach
agents only through the project-local mount — see
[[docs/skills/project-local.meiva.okf.md]].

The external-agent operator bridge is outside this load-directory contract. A
prod host-global converge step projects the complete `meiva-guide` package into an
already-present Codex or Claude Code home; it neither registers that copy in
Meiva's install registry nor restores `.agents/skills/` as an Meiva runtime source.
See [[docs/skills/external-agent-operator-bridge.meiva.okf.md]].

General methodology and user-service skills — `meiva-serious-engineering`,
`meiva-serious-research`, `meiva-deep-research`, `meiva-corp`, and
`telegram-send-file` — are repo built-ins, so this first source lands them in
the load directory and makes them available to the gateway's command index.
Their `.agents/skills/` entries are open-standard symlink mirrors, not
project-skill sources.
