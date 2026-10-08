# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Anti-patterns

- **Self-certification**: the agent that produced the result declares it done → Instead: independent verification point (`principles/honesty`, `practices/present`).
- **Benchmark-as-truth**: "SOTA on X" with no provenance → Instead: provenance + contamination check + claim scoped to what the benchmark can support.
- **Output-only review**: reading the paper, not the logs → Instead: re-derive the key numbers from logs + code (82% detection).
- **Evals nostalgia**: assuming the old eval still measures the new system → Instead: re-derive what signal substitutes for human judgment in this system.
- **Verification theater**: a checklist with no check stronger than self-assessment → Instead: at least one formal- or process-level check per project.

## Bad → good

- **bad**: "The model's self-assessment says the result is correct, and the benchmark says SOTA — ship it." (two weakest signals, no provenance, producer self-certifies)
- **good**: "Claim: our method improves F1 by 3.2±0.4 (10 seeds). Verification: (1) key numbers re-derived from logs by an independent agent; (2) paired statistical test pre-registered; (3) benchmark provenance checked, contamination check documented; (4) self-assessment reported as such, used for exploration only."
- **bad**: "We evaluated on the standard benchmark everyone uses." (inherited choice, no justification)
- **good**: "We evaluated on a private held-out set from the target distribution (protocol per `practices/design`), plus the standard benchmark for comparability — with its provenance and contamination caveats stated."

## Sources

- Tworek at Auto-Research Summit / AGI House (X @agihouse_org/status/2085133996137259312, 2026-08); the-decoder.com on Core Automation
- burny_tech, *Recursive Self-Improvement in AI* RSI survey (X @burny_tech/status/2085462603610861802, 2026-08) [unverified: arXiv ID]
- Luo et al., CMU evaluation of AI Scientist (arXiv 2509.08713)
- Cursor benchmark-retrieval research (2026, via X @v_shakthi) [unverified: original report]; Gizmodo bogus-benchmarks (2026)
- Zhen Wang on the Nature week and trainable environments (X @zhenwang9102/status/2057207629227667544, 2026-05)
- Jeff Dean, Discovery Loop announcement (X @JeffDean/status/2085034604172603724, 2026-08)
- Gelman & Loken, The Garden of Forking Paths (pre-registration rationale, via `principles/honesty`)
