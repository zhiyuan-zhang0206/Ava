# Codebase Sweep with Adversarial Review

**What this shows**: 7-wave orchestrator-workers scanning real code.
28 agents total. Silent workers, checkpoints on agent lifecycle instead of files.

## Prompt

```
Load dynamic workflow skill: ava.help(ava.skills.ava_dynamic_workflow)

Use reference/codebase_sweep_orchestrator.py to scan Ava repo's legacy code.

Directly execute with script:
- Wave 1: 4 scout agents scan agent/ ava/ shared/ plugins/
- Wave 2: 8 verify agents cross-verify (2 per issue type)
- Wave 3: 1 writer composes initial report
- Wave 4: 3 adversarial agents try to overturn findings
- Wave 5: Feed feedback back to Wave 2 original verify agents for revision
- Wave 6: Final report
- Wave 7: 2 review + 1 publish by rendering to HTML and serving with ava.ui.serve

Use the terminate signal: a worker writes its JSON file and calls ava.self.terminate() —
it never messages the orchestrator. Each wave's checkpoint watches the workers' agent
statuses instead of their files; all terminated = wave complete.
```

## Expected flow

7 waves, ~28 agents:
```
W1 Scout (4) ──→ W2 Verify (8) ──→ W3 Reduce (1)
    ↓                                    ↓
W4 Adversarial (3) ──→ W5 Feedback (8) ──→ W6 Reduce (1)
    ↓
W7 Publish (3) ──→ render HTML → ava.ui.serve()
```

## Checkpoint signal: agent lifecycle

Worker completion protocol (identical to every other dynamic workflow):
write JSON file → `ava.self.terminate()`. Silent — no `send_message`.

Orchestrator: one checkpoint per wave. The checkpoint is the same `gather_files`
file-polling watcher as every other dynamic workflow (see
`ava_builtins/skills/ava-dynamic-workflow/reference/`) — here it watches the
wave's status file: all agents reported terminated = wave complete → read the
result files for the data.

Difference from the gather_files checkpoint: what the watcher looks at (agent
lifecycle vs. the file system). Both carry data in files, and in both the
orchestrator wakes once per wave, never once per worker.

## Scale

28 agents, 7 waves, 1 feedback loop
