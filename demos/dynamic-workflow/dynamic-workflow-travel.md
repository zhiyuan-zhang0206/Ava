# Deep Research with Cross-Check

**What this shows**: 7-wave orchestrator-workers with feedback loop.
40 agents total. Silent workers, one checkpoint per wave.

## Prompt

```
Load dynamic workflow skill: ava.help(ava.skills.ava_dynamic_workflow)

Then use the script from reference/deep_research_orchestrator.py to conduct a deep research.
Topic: "AI coding agent 2026 competitive landscape".

Use the script directly, do not modify it — the script has already written all the parameters and prompts for each wave.
You only need to execute it. The script will:
- Wave 1: spawn 5 explore agents (commercial/open-source/China/academic/enterprise)
- Wave 2: 1 planner + 10 verifiers for cross-validation
- Wave 3: 1 writer to synthesize the initial draft
- Wave 4: 5 adversarial agents to find vulnerabilities
- Wave 5: Feedback fed back to Wave 1+2 original agents for correction
- Wave 6: Final draft
- Wave 7: 2 review + 1 publish via ava.ui.serve_markdown

Workers end silently: each writes its JSON to the handoff directory and terminates —
none of them messages the orchestrator. The orchestrator arms one checkpoint per wave
(a gather_files watcher); the checkpoint's files landing = wave complete.
```

## Expected flow

7 waves, ~40 agents:
```
W1 Explore (5) ──→ W2 Deep-dive (1→10) ──→ W3 Reduce (1)
    ↓                                              ↓
W4 Adversarial (5) ──→ W5 Feedback (15) ──→ W6 Reduce (1)
    ↓
W7 Publish (3) ──→ ava.ui.serve_markdown()
```

## Completion protocol: silent workers, per-wave checkpoints

Worker: write JSON file → `ava.self.terminate()`. No message to the orchestrator —
40 workers reporting individually would cost 40 orchestrator LLM turns.

Orchestrator: 7 checkpoints, one per wave, because each wave consumes the previous
wave's output. A checkpoint is a gather_files watcher over the handoff directory;
its files landing wakes the orchestrator exactly once. Wave 5 counts feedback files
by glob (only critiques that map to a live agent produce one) and Wave 7 waits on the
two reviews but not the publisher — the designated-reporter shape.

## Scale

40 agents, 7 waves, 2 feedback loops
