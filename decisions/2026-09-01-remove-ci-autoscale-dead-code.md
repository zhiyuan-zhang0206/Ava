# Remove the dead CI autoscaler instead of preserving its references

Date: 2026-09-01

## Context

The expert review found `ops/ci_autoscale/` described by the repository as an
active subsystem. The directory is not present in the tracked tree: it has no
tracked implementation, callers, or tests apart from references to its former
logger name and controller path. Those references made the first-party logger
allowlist, its regression test, and the clock-lattice lint configuration assert
that a non-existent CI autoscaler was still part of the runtime.

An untracked local residue in a separate shared checkout does not establish a
repository feature. It is outside this worktree and cannot safely be removed by
this change.

## Decision

Remove every tracked reference to the absent CI autoscaler. The first-party
logger allowlist now names only tracked application namespaces; the logger test
uses a descendant of every such namespace; and the clock-lattice linter keeps
only exemptions for paths in the tracked source tree.

No replacement subsystem is introduced. Version control preserves the former
implementation if a future CI scaling requirement is established.

## Alternatives rejected

- **Keep the logger name and lint exemption for possible local use.** Rejected:
  configuration and tests must describe the tracked product, not an
  unversioned machine-local artifact.
- **Restore the missing directory to match its references.** Rejected: no
  current caller or requirement justifies restoring an obsolete subsystem.
- **Delete the untracked shared-checkout residue here.** Rejected: it is outside
  this worktree and outside the scope of a tracked repository change.

## Consequences

- First-party DEBUG passthrough cannot be configured for a removed CI
  autoscaler.
- The clock-lattice linter no longer carries an exemption for an absent
  controller.
- A future CI autoscaler must arrive as a complete, tracked subsystem with its
  own logging and timing contracts rather than relying on these historic
  references.
