# Reclassify general skills as builtins

## Context

Issue #146 stopped converge from treating `.agents/skills/` as a source, which
correctly kept Ava's repo-development workflow and Ava-cluster-operations
skills project-local. Its implementation and surrounding wording treated the
whole directory as L4-only, however. Five general skills therefore disappeared
from fleet machines and from the gateway command autocomplete, which scans the
converged `~/.ava/skills/` load directory: `ava-serious-engineering`,
`ava-serious-research`, `ava-deep-research`, `ava-corp`, and
`telegram-send-file`.

## Decision

The user's 2026-08-22 five-point ruling adopts option B: move those five skills
to `ava_builtins/skills/` and retain relative `.agents/skills/` symlink mirrors.
They are repo built-ins, converge into the independent materialized
`~/.ava/skills/` load directory, and appear in the gateway command index. The
mirrors keep the open Agent Skills standard path without creating second
sources.

`.ava/skills` stays empty for now. The three Ava-cluster-operations skills
remain real `.agents/skills/` project skills, alongside the other
repo-development workflow skills.

## Alternatives rejected

- **Keep the five project-local.** General methodology and user-service
  capabilities would remain unavailable on runtime-only machines and absent
  from the web composer autocomplete.
- **Restore `.agents/skills/` as a converge source.** That reverses the
  intentional issue #146 boundary and fleet-distributes the remaining L4
  workflow and operations skills.
- **Create a third duplicate source in `.ava/skills`.** A source copy would
  split ownership from the built-ins, while `~/.ava/skills/` must remain the
  converge-owned materialized directory rather than a symlink.

## Consequences

The correction narrows, rather than overturns, issue #146: L4-only does not
mean every skill formerly located in `.agents/skills/`. Existing user-origin
registry rows for `ava-corp` or `telegram-send-file` can shadow the repo source
on some machines until operators remove those rows; this PR deliberately adds
no migration or cleanup code for them.

Two follow-ups remain outside this change: make `/api/commands` autocomplete
per-agent, and remove the shadowing user-origin registry rows where present.
