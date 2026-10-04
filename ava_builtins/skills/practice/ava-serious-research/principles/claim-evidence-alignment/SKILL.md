---
name: claim-evidence-alignment
description: "Aligns every research claim with evidence that measures exactly what it asserts and traces to a run. Use when writing conclusions, choosing metrics, reviewing results, or checking whether a number actually warrants the stated claim."
---

# Claim–Evidence Alignment

## One-sentence core
> Every claim must be supported by evidence whose object of measurement is isomorphic to what the claim asserts: the metric states explicitly what it measures and why that supports the conclusion, and every number traces to a concrete run.

## Core principles
- **Metric–claim warrant, made explicit**: for every metric, answer "is the object it measures isomorphic to what the claim asserts" — **Why**: metric substitution (claiming robustness while reporting accuracy) is the most insidious evaluation mismatch, and evaluation-method errors are harder to spot than model errors (Raschka); inflated numbers in ML-based science are most often leakage artifacts, which a warrant check catches (Kapoor) — **How**: when writing a conclusion, also write the warrant: "F1 here measures minority-class recall, supporting the 'low miss rate' claim, not an 'overall accuracy' claim".
- **Evidence before assertion**: data, logs, code > descriptive assertion; citations reach primary sources — **Why**: a citation is itself a claim about the literature, and an unverified claim about a claim compounds into a record no one can trust; the duty to trace assertions to primary sources predates AI, and AI makes the failure easier at scale (2026 measured evidence in `ai-era/ai-failure-modes`) — **How**: every key number carries its run record / log path; every citation is checked for existence and status (including retraction).
- **Numbers carry uncertainty**: point estimates come with variance, replication count, and a matched statistical test — **Why**: a number without error bars cannot distinguish a real difference from noise (Raschka: model comparison requires paired tests such as McNemar or 5×2cv F) — **How**: repeat across seeds, report mean±std, and pick the test that matches the comparison type (paired tests such as McNemar for paired designs, on the same test set).
- **Claims scoped to the evidence's distribution**: conclusions must not exceed the data distribution and setup — **Why**: leakage manufactures inflated numbers and out-of-distribution conclusions collapse (Kapoor: 329 papers with wildly overoptimistic conclusions) — **How**: conclusions carry a distribution statement; generalization claims require external validation.

## Checklist
- [ ] Every conclusion carries an explicit warrant: what the metric measures, why it supports the claim, and what it does not support
- [ ] Key numbers trace to a concrete run (log / code / config path)
- [ ] Citations verified to exist (including retraction-status check)
- [ ] Point estimates carry variance / replication count; test matches comparison type
- [ ] Conclusions scoped to the evidence's distribution; generalization claims externally validated
- [ ] Negative results and failed experiments recorded as evidence too
- [ ] Secondary-source quotes and figures are traced back to the primary source before they enter the record

## Anti-patterns
- **Metric substitution**: claiming robustness while reporting accuracy; claiming efficiency with FLOPs that ignore I/O → instead: write the claim first, then pick a metric that measures the claim's object
- **Selective metrics**: reporting only the favorable metric from one run → instead: pre-declare the primary metric and report all
- **Assertion without trace**: "experiments show…" with no logs or code → instead: key numbers carry trace paths
- **Fake confidence**: reporting ±0.0 from n=1 → instead: report replication count honestly; label n=1
- **Evidence laundering**: quoting a secondary source as if it were primary → instead: trace every citation to the primary source before it enters the record

## Bad → good
- **bad**: "The method is more accurate in practice." — no operational definition, no data, no run record
- **good**: "On the sealed test set of dataset X, mean accuracy over 10 seeds is 91.7±0.6 (runs/2026-08-06/exp-17). The claim covers accuracy only, not robustness or latency."
- **bad**: "Our model is more robust (accuracy 92.3%)." — robustness claim, accuracy evidence; the warrant is broken
- **good**: "Our model is more robust to input perturbation: mean performance drop 4.1% (±0.8, 10 seeds) across 5 perturbation types vs 9.3% for the baseline. Accuracy is 92.3%, but this claim concerns only the drop under perturbation, not overall accuracy."
- **bad**: citing a paper that "presumably exists" to support related work
- **good**: before citing, verify DOI/arXiv existence, authors, and retraction status; record the verification in notes.

## Relationships
- Metric choice and statistical tests: `practices/measure`; leakage audit: `practices/design`
- Citation-verification mechanics: `practices/literature`; the audit that re-checks warrants on a cold read: `practices/verify`
- Deliberate misalignment is an integrity problem, not a methods problem: `principles/honesty`
- Citation verification: `practices/literature`; evidence levels when presenting: `practices/present`
- Boundary with honesty: this principle covers unwitting misalignment; deliberate misalignment belongs to `principles/honesty`

## Sources
- Raschka, Model Evaluation, Model Selection, and Algorithm Selection in ML
- Kapoor & Narayanan, Leakage and the Reproducibility Crisis in ML-based Science
- Deutsch, *The Beginning of Infinity*, 2011 — `../../references/08-deutsch-beginning-of-infinity.md` (good explanations are hard to vary; explanation as the unit of knowledge)
- AI-era scale evidence (hallucinated citations): `ai-era/ai-failure-modes`
