# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Anti-patterns

- **Result-only delivery**: "it works, +8 points" with no trail — → present the decision trail: question, hypotheses, experiments, failures, decisions.
- **Self-verdict**: presenting and declaring "done and verified" in the same act — → name the human checkpoint; get an independent pass.
- **Verification laundering**: reporting numbers without their verification level, letting the reader assume they were checked — → tag every claim; default to "not verified".
- **Hidden decision points**: making judgment calls silently and reporting only the outcome — → surface the fork, the options, and the choice.
- **Reader-blind reporting**: writing what you did without considering what the reader knows, doubts, and must decide — → reader analysis first, structure second.
- **Failures in the appendix**: failed experiments buried or omitted — → failures in the main trail, one line each, with the lesson.

## Bad → good

- **bad**: "Experiment done. Our method achieves +8% F1. Verified." (one act: render + decide + self-assess)
- **good**: "+8% F1 over baseline on the sealed test set (3 seeds; runs/2026-08-06/exp-17). Verification: statistical test — McNemar p = 0.02. Not verified: generalization beyond this distribution — no OOD data. Decision needed from you: proceed to follow-up experiment B? My recommendation: yes, because the error analysis shows ... (options considered: B, C, stop; evidence for each)."

- **bad**: "We tried our method and it is better." (result only)
- **good**: "Question: does component A carry the gain? Hypothesis: yes, by reducing X. Experiments: (1) full model +8.0; (2) −A: +1.2 → A is the main contributor; (3) −B: +7.1 → B not significant (p > 0.05), dropped; (4) failed: variant C did not converge (log: runs/exp-19). Decision point: keep B in the final model? I recommend dropping it."

- **bad**: "The system is verified correct." (no level, no trace)
- **good**: "Claim 1: the arithmetic is property-tested [formal verifier]. Claim 2: improves over baseline [process-level: statistical test, McNemar on sealed test]. Claim 3: ready for production — NOT verified; needs an OOD evaluation [self-assessment only]. Trace: all numbers reproduce from runs/exp-17; rerun: `bash runs/exp-17/run.sh`."

## Sources

- Feynman, "Cargo Cult Science," Caltech 1974 — `../../../references/feynman-cargo-cult-science.md` (report what could invalidate the result; leaning over backwards)
- Deutsch, *The Beginning of Infinity*, 2011 — `../../../references/08-deutsch-beginning-of-infinity.md` (explanation as first-class output; hard-to-vary claims)
- *The AI Research Assistant: Promise, Peril, and a Proof of Concept*, arXiv:2602.22842 (human-led, multi-AI-assistant model) — evidence also in `ai-era/ai-research-landscape`
- Burny (@burny_tech), *Recursive Self-Improvement in AI: From Bounded Self-Refinement to Autonomous Research Loops*, 2026 (verification-level ladder: formal verifiers → process reward models → rubrics → intrinsic self-assessment)
- CMU evaluation of the AI Scientist, arXiv:2509.08713 (fabrication detection 55% → 82% with trace logs + code) — evidence also in `ai-era/ai-failure-modes`
- AI-era articulation of role separation (2026-08): `ai-era/ai-failure-modes`
- Ng, *Machine Learning Yearning*, 2018 (eyeball/blackbox split: human inspection belongs on a set you may look at, never on the evaluation set)
