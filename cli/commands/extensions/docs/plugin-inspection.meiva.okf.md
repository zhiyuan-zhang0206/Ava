---
type: doc
title: Plugin inspection commands
description: Read-only inspect and verify output and load boundaries.
tags: [contracts]
---

# Plugin inspection commands

## Inspection output and verification

inspect reports surfaces or one installed plugin and returns nonzero for an unknown name. verify loads the enabled agent-boot faces, exits 1 for contained plugin failures and 2 for a loader crash; it does not start an agent or invoke hooks.

Inspect output is line-oriented for both people and agents: stable tokens, one fact per line, and signatures kept unwrapped for grep. It links registration facts to activation telemetry without querying the event store. Verify reports `RESULT enabled=N failed=M rc=R`, then `RED plugin=...` for each contained failure or `TOOL-ERROR` for a loader failure. The reporter contains diagnostics without emitting telemetry; plugin import-time code still runs. Only the SDK, config and agent-runtime faces are verified: services, providers and data faces have separate load paths.
