# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Anti-patterns

- **Celebrating first, auditing later**: numbers that look too good are published, not examined — → the leakage audit is the first reaction to a surprising result.
- **Self-verdict**: declaring work verified because you produced it — → at minimum the four-judge adversarial pass; better, an independent check (the human or a separate reviewer).
- **Paper-only verification**: checking the write-up instead of the runs — → re-derive from logs and code (55% → 82%).
- **Verdict without attribution**: "our method failed" with no cause — → run the attribution chain before concluding.
- **Selective memory**: dropped runs and failed experiments vanish from the record — → account for every run; record why each was dropped.
- **Unlabeled claims**: numbers presented without a verification level — → tag each claim; default to "not verified".

## Bad → good

- **bad**: "Our model reaches 99.2% accuracy on disease prediction — excellent result."
- **good**: "99.2% is suspiciously high, so the leakage audit ran first: (1) no independent test set — 'test' is a random subset of the same cohort; (2) patient IDs overlap across splits (category 5). The claim degrades to in-distribution only; re-splitting by patient gives 71.4%."

- **bad**: "We tried several configurations and the best one gives p = 0.03 — our method is significantly better." (no record of which decisions saw the test set)
- **good**: "Pre-registered main analysis: McNemar on the sealed test set. Decision audit: no threshold, early stop, or run-dropping after seeing test numbers — all tuning on validation. One post-hoc threshold sweep is labeled exploratory and excluded from the headline claim."

- **bad**: "The method doesn't work." (no attribution)
- **good**: "Attribution chain: representation — it fits the training set perfectly, so the hypothesis space can express the target; optimization — training loss plateaus high and a learning-rate sweep did not move it; data — 200 samples, high variance across seeds; evaluation — the metric sits near chance. Conclusion: data-limited, not a representation failure; learning curves say more data is the next step."

## Sources

- Kapoor & Narayanan, *Leakage and the Reproducibility Crisis in ML-based Science*, Patterns 4(9), 2023; arXiv:2207.07048
- Raschka, *Model Evaluation, Model Selection, and Algorithm Selection in Machine Learning*, arXiv:1811.12808
- Gelman & Loken, *The Garden of Forking Paths*
- CMU evaluation of the AI Scientist, arXiv:2509.08713 (trace logs: 55% → 82%; automated p-hacking) — evidence also in `ai-era/ai-failure-modes`
- *Grounded Autonomous Research: Fault-Tolerant LLM Pipeline from Corpus to Manuscript*, arXiv:2607.02329 (adversarial review as a fault-tolerance layer) — evidence also in `ai-era/ai-research-landscape`
- Domingos, *A Few Useful Things to Know About Machine Learning*, CACM 55(10), 2012 (L1, L11 — attribution)
- Ng, *Machine Learning Yearning*, 2018 (bias/variance/data-mismatch decomposition; optimization verification test)
- Burny (@burny_tech), *Recursive Self-Improvement in AI: From Bounded Self-Refinement to Autonomous Research Loops*, 2026 (verification-level ladder)
