# Project one operator skill to external agent clients

## Context

The 2026-08-20 distribution decision stopped converging Ava's real
`.agents/skills/` family into every Ava runtime agent. Project-local mounting is
the right boundary for contributor and cluster-operation procedures because it
keeps unrelated runtime capability indexes free of kernel-maintenance noise.

Codex and Claude Code sessions operating beside an Ava cluster have a narrower
need: they must locate sibling agents' workspaces and durable memory before
diagnosing or coordinating with them. Those clients may run outside the Ava
checkout, so the project-local mount cannot provide the operator procedure.

## Decision

Add one narrowly scoped exception for `operating-ava-cluster`. Prod
host-global converge projects exactly that skill into the global skills root of
an already-present `~/.codex` and/or `~/.claude` home. It does not create a
missing client home, does not enumerate other clients, and does not add
`.agents/skills/` back to Ava's runtime skill sources.

The projection is a copied directory, not a symlink. A regular marker records
Ava ownership and the digest of the content Ava last wrote. Converge may update
the target only when its current digest still matches that record. An unmanaged
target or a user-modified managed copy is preserved and reported. Updates stage
a complete copy beside the exact target and restore the prior verified copy if
activation fails.

## Alternatives rejected

- **Restore fleet distribution for `.agents/skills/`.** This would reverse the
  noise boundary for every Ava runtime agent to solve one external-client
  context need.
- **Link the repo skill into each client.** Directory symlinks are not a
  portable Windows contract and make client availability depend on the prod
  checkout path remaining mounted.
- **Create client homes proactively.** Presence of a top-level home is the
  evidence that the user installed and uses that client; Ava must not manufacture
  that evidence.
- **Overwrite a reserved target name.** External client directories are
  user-owned. A name collision or local edit is a conflict to surface, not
  permission to erase content.
- **Require a manual copy.** A manual copy drifts across product updates and
  provides no ownership proof for safe refresh.

## Consequences

- The 2026-08-20 project-local-only ruling remains intact for Ava agents and for
  every other repo skill. This decision supersedes only its implication that no
  repo cluster-operation skill may ever have a separate external-client
  projection.
- A host with neither requested client installed gets no filesystem changes.
- A conflict requires the user to choose whether to keep, remove, or reconcile
  their target; converge deliberately has no force-overwrite surface.
- Dev worktree convergence cannot mutate global client context because the
  existing prod/default-home host-global gate excludes it.

Related:
`decisions/2026-08-20-stop-fleet-distributing-kernel-contributor-skills.md`.

Update 2026-09-04: the ownership and update protocol was strengthened after
adversarial review; see
[`2026-09-04-bind-external-skill-ownership-outside-target.md`](2026-09-04-bind-external-skill-ownership-outside-target.md).
