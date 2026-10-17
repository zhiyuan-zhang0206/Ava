---
name: evaluation-paradigm-shift
description: "Assesses AI evaluation beyond static benchmark scores. Use when examining contamination, process evidence, or the verification gap in AI-produced results."
---

# Evaluation Paradigm Shift

> **Observation dated 2026-08.** This file captures a live debate, not settled doctrine: the "era of evals" claim (Tworek), the benchmark-contamination crisis, and the emerging answer — evaluate process with an explicit verification hierarchy.

## One-sentence core

> Static benchmarks and output-only review are losing authority as evidence of research quality; the open question "who verifies the output of an automated lab" is now the central evaluation problem, and the working answer is to evaluate process (trace logs + code) with an explicit verification hierarchy and a named, independent verifier.

## The shifts (2026-08 observation)

1. **"The era of evals is done" (Jerry Tworek)** — Tworek (ex-OpenAI; led o1/o3/Codex; founder of Core Automation, "the most automated AI lab") argued at the Auto-Research Summit (2026-08) that traditional evals fail when AI runs research automatically; his AGI House interview is titled "why the era of evals is done." The claim: when the lab itself is automated, nobody knows what signal substitutes for human judgment.
2. **Benchmark crisis** — frontier models retrieve solutions from test sets (Cursor research, 2026 [unverified: original report]); bogus benchmarks inflate capability claims (Gizmodo, 2026); "benchmark picking" is a documented AI-Scientist pitfall (CMU arXiv 2509.08713, pitfall #1); arXiv is tightening AI-slop policies (2026).
3. **Who verifies the output is the research gap** — with fully-automatic labs, the verification question is unsolved; the RSI survey (burny_tech, 2026) gives the operative hierarchy: formal verifiers (strongest) → process reward models → rubrics → intrinsic self-assessment (weakest). Every improvement loop is implicitly a claim that some signal can replace human judgment.
4. **Process beats output for detection** — CMU: paper-only review detects fabrication 55% of the time; adding trace logs + code raises detection to 82%. Evaluation that does not look at the process misses roughly half of fabrication.
5. **Trainable environments as the next wave** — instead of bigger agent swarms, agents trained in environments grounded in raw data (Zhen Wang, 2026-05); Discovery Loop's mission is exactly the automated experiment loop; "who evaluates an agent trained in an environment" will be the next evaluation question.

## Core principles

- **State the verification level of every claim**: use the hierarchy (formal verifier → process reward → rubric → self-assessment) and say where each claim sits — **Why**: the RSI survey makes the hierarchy explicit, and "the model says it is good" is the weakest signal; presenting it as strong is a claim-evidence mismatch — **How**: tag each headline claim in a report with its level; design at least one claim per project to be checked at a level stronger than self-assessment.
- **Evaluate process, not just output**: require trace logs + code for anything load-bearing — **Why**: 55% → 82% detection (CMU arXiv 2509.08713) — **How**: when auditing, re-derive the key numbers from logs; when producing, keep the trace from day one (`practices/reproduce`); when reviewing a paper, ask for the logs before believing the numbers.
- **Treat benchmark numbers as contaminated until proven otherwise**: — **Why**: test-set retrieval and bogus benchmarks are documented (Cursor 2026; Gizmodo 2026); contamination is the same leakage family as Kapoor's taxonomy, now applied to evaluation itself — **How**: check benchmark provenance (who built it, what leakage paths exist, when the model's training data was collected); prefer private held-out data; report the contamination check alongside the number.
- **Decide who verifies before starting**: name the independent verifier (human collaborator, separate audit agent, formal checker) before the work, not after — **Why**: "who verifies the automated lab's output" is unsolved; leaving it implicit means the producer self-certifies, and presenter ≠ judge is the sharpest 2026 consensus — **How**: in the project plan, write one line per deliverable: "X verifies Y against Z."
- **Pre-registration is the anti-gaming contract**: the discipline that guards against p-hacking now guards against automated selection — **Why**: automated p-hacking (CMU) is selection-on-test with better throughput; pre-registration is the documented countermeasure (Garden of Forking Paths) — **How**: hypothesis, metric, and procedure written before runs; test set sealed; post-hoc analyses labeled.
- **Watch for environment-based evaluation**: the next evaluation wave may be agents in trainable environments rather than static tests — **Why**: consensus commentary (Zhen Wang 2026-05; Discovery Loop 2026-08) — **How**: when evaluating an agentic system, treat environment fidelity and raw-data grounding as first-class criteria, and state what substitutes for the benchmark's signal.

## Checklist

- [ ] Each headline claim carries a verification level (formal verifier → self-assessment)
- [ ] At least one key claim per project is checked at a level stronger than self-assessment
- [ ] Trace logs + code exist for every load-bearing number, and were used in review
- [ ] Benchmark provenance is stated; contamination check done or marked [unverified]
- [ ] The verifier is named and is not the producer of the frame
- [ ] Main comparison pre-registered; test set sealed
- [ ] The evaluation choice (benchmark vs environment vs process audit) is justified, not inherited

## Relationships

- Verification levels and presenter ≠ judge: `principles/honesty` + `practices/present`; claim–evidence alignment under the new regime: `principles/claim-evidence-alignment`.
- Pre-registration and statistics: `practices/measure`; sealed test sets and leakage: `practices/design`.
- Trace-log discipline: `principles/reproducibility` + `practices/reproduce`.
- Failure modes that motivate this file: `ai-era/ai-failure-modes`; the tool landscape being evaluated: `ai-era/ai-research-landscape`; the upgrade/invalidation map: `ai-era/guidance.md`.

## Examples and sources

Read [examples and sources](references/examples-and-sources.md) when a concrete
counterexample, worked example, or source context would clarify these decisions.
Use the core guidance above directly for routine work.
