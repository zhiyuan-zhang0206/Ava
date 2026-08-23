# Phase-B updater outcomes

## Decision

- Treat a POSIX `updater-<epoch>.log` as the authoritative updater outcome
  whenever one exists. Read the shared backend session log only when no per-run
  file exists; Windows continues to read that shared log.
- Let the in-process restart guard defer to Windows' session-tree sparing
  predicate. A recorded service session blocks a restart only when its stop tree
  would actually reach the restart process.

## Why

POSIX writes one tee log per updater run, while its backend log appends across
runs. The appended file can be microscopically newer while it still contains a
previous run's exit line, so selecting it incorrectly makes a live updater look
terminal. Windows preserves nested live-session subtrees while killing a service
tree; its updater therefore survives stopping its `ava-ops` parent and must not
decline that safe restart.

## Rejected alternative

Duplicating Windows tree-boundary logic in `shared.proc` would drift from the
kill path. The guard instead calls the kill path's public sparing predicate.
