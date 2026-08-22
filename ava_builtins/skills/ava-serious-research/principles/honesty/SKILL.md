---
name: honesty
description: Use when a result could be selected, framed, or omitted, and at every step where the record could drift from what was done — the research record must be a faithful account of what was done and found, including failures, dropped runs, and numbers that did not fit.
---

# Honesty

## One-sentence core
> The research record is a faithful account of what was done and found — failures, dropped runs, and unflattering numbers included; and rendering is separated from deciding, because once the two roles merge, review becomes a formality.

## Core principles
- **No p-hacking**: researcher degrees of freedom (which analysis path to take) must not be used to manufacture significance — **Why**: the Garden of Forking Paths shows the same data offers many post-hoc "legitimate" analysis paths and significance is often an artifact of path choice; the mechanism scales to automated systems whose reward looks at the test set — automated p-hacking (evidence: `ai-era/ai-failure-modes`) — **How**: pre-register the primary analysis; any post-hoc addition is labeled exploratory; zero tolerance for test-set decisions.
- **No cherry-picking**: all runs and seeds are accounted for; drops are recorded with reasons — **Why**: selective reporting (picking seeds, deleting failed runs) distorts statistical inference and is among the most-caught integrity failures in review — **How**: keep the full run log; report the distribution over all seeds, not the best seed.
- **No fabrication**: numbers, citations, and figures must be real and traceable — **Why**: fabrication — deliberate or unwitting — poisons the record for everyone who builds on it; the cumulative enterprise of science depends on each report being a faithful account. The duty predates AI; AI only makes fabrication easier at scale, and the measured 2026 scale (fabricated citations >12× since 2023; 4/7 AI-Scientist papers with hallucinated numbers) is in `ai-era/ai-failure-modes` — **How**: every number comes from a log; every citation is verified; AI-generated content is labeled and human-verified.
- **Failures and negative results are reported faithfully**: they are part of the evidence — **Why**: reporting only successes skews the literature (Ioannidis: publication bias is a root cause of irreproducibility) — **How**: the research log keeps failed experiments; presentations to humans include a "tried but did not hold" list.
- **Rendering ≠ deciding**: whoever renders the frame must not be the one to declare it done — **Why**: whoever both produces and judges an account has no check on motivated framing; every institution of credible review — courts, peer review, auditing — separates advocate from adjudicator. Once the roles merge, review turns into a formality. (The 2026-08 articulation of this for AI research is in `ai-era/ai-failure-modes`.) — **How**: key conclusions pass an independent perspective (human collaborator or independent audit) before being declared final.

## Checklist
- [ ] Primary analysis pre-registered before data was analyzed; post-hoc analyses labeled exploratory and dated
- [ ] All runs/seeds accounted for; drops have recorded reasons
- [ ] Every number traces to a log; citations verified (including retraction status)
- [ ] Failed experiments are recorded, always
- [ ] Negative results are presented in any report that claims a positive result on the same pipeline
- [ ] AI-generated content labeled and human-verified
- [ ] Key conclusions reviewed by an independent perspective (renderer ≠ decider)
- [ ] The renderer and the decider of "done" are named and are distinct roles in the project record

## Anti-patterns
- **p-hacking**: trying analyses until something is significant → instead: pre-registration + exploratory labeling (`practices/measure`)
- **Cherry-picking**: reporting only the best seed/epoch/split → instead: report the full distribution
- **HARKing**: hypothesis after results → instead: hypothesis before (`principles/falsifiability`)
- **Hallucinated citations/numbers**: citing nonexistent literature, completing nonexistent values → instead: verify + trace
- **Selective publication**: hiding failed experiments → instead: keep logs; present negative results faithfully
- **Framing without disclosure**: presenting a result so it reads as more confirmed than it is → instead: state each claim's verification level and qualifiers (`practices/present`)

## Bad → good
- **bad**: trying six preprocessing variants and reporting only the best one as the method (unlabeled analysis path)
- **good**: pre-registering the primary pipeline; reporting the other five as exploratory with their numbers attached
- **bad**: running 10 seeds and reporting the best 3 as "mean±std"
- **good**: reporting all 10 seeds' mean±std, noting the 2 anomalous runs and their causes (e.g., OOM rerun)
- **bad**: AI-generated related-work citations adopted without verification
- **good**: every AI-suggested citation verified for existence and relevance before adoption; AI involvement declared in the methods

## Relationships
- Statistical defense against p-hacking: `practices/measure`; audit re-derivation: `practices/verify`
- The presentation duty for verification levels and qualifiers: `practices/present`; the rendering ≠ deciding boundary is enforced there
- The record includes the path to every number, so a reader re-derives instead of trusting
- Citation verification: `practices/literature`; presentation discipline: `practices/present`
- Boundary with claim-evidence-alignment: deliberate misalignment is an integrity problem; unwitting misalignment is a methods problem

## Sources
- Gelman & Loken, The Garden of Forking Paths
- Ioannidis, Why Most Published Research Findings Are False
- Feynman, "Cargo Cult Science," Caltech 1974 — `../../references/feynman-cargo-cult-science.md` (do not fool yourself; utter honesty in reporting)
- Ritchie, *Science Fictions*, 2020 — `../../references/ritchie-science-fictions.md` (fraud / bias / negligence / hype cases)
- AI-era scale evidence (automated p-hacking, hallucinated citations, rendering ≠ deciding): `ai-era/ai-failure-modes`
