---
name: ai-era-guidance
description: "Use when deciding how much weight to give a principle, a signal, or a new practice — this is the dated upgrade / invalidation map: which enduring principles the AI era strengthens, which traditional signals it weakens, and which new disciplines it adds."
---

# AI-Era Guidance: Principle Upgrade / Invalidation Map

> **Observation dated 2026-08.** This is the control document for the `ai-era/` layer: it maps each part of the skill to its status in the AI era. Dates matter — the fast-moving observations here (tool landscape, failure modes, evaluation debate) turn over in months; the principles themselves do not (see the three-layer split in the root SKILL.md).

## One-sentence core

> The AI era strengthens every enduring principle in this skill — falsifiability, claim–evidence alignment, reproducibility, parsimony, honesty — because fabrication and gaming are now cheap and automated; it weakens traditional *signals* (peer review, static benchmarks, double-blind anonymity, "published = verified"); and it adds new disciplines (verification-level tagging, tool-class awareness, contamination-by-default, named independent verifiers) that did not exist before.

## The map

Legend: **↑** strengthened · **↓** weakened · **→** unchanged · **+** new (added by the era).

| Part of the skill | Status | What changes in practice | Date |
|---|---|---|---|
| `principles/falsifiability` | ↑ | AI generates plausible hypotheses at near-zero cost — the filter must be falsifiability, applied before any compute. Pre-registration is now also a defense against automated selection (CMU: internal reward on test set = automated p-hacking). | 2026-08 |
| `principles/claim-evidence-alignment` | ↑ | Hallucinated citations (+12× since 2023, Lancet) and fabricated numbers (4/7 papers in AI-Scientist evaluation) make "evidence traces to a run" the default requirement; the warrant must name the verification level. | 2026-08 |
| `principles/reproducibility` | ↑ | Trace logs + code are no longer nice-to-have: they raise fabrication detection from 55% to 82% (CMU). Calibration checkpoints (reproduce a known result first) are the documented fault-tolerance mechanism for autonomous pipelines. | 2026-08 |
| `principles/parsimony` | → | Unchanged in substance, wider in scope: scaffolding, agent counts, and tool chains are complexity too — every component must earn its place (Flag Game: agent count has an optimum; weak baselines still decide what "improvement" means). | 2026-08 |
| `principles/honesty` | ↑ | The era industrializes p-hacking and adds machine-scale fabrication; presenter ≠ judge becomes a design principle, not etiquette (2026-08 consensus: review stops being a check and becomes a formality when they merge). | 2026-08 |
| `practices/question` | ↑ | Question quality is the human bottleneck: AI can generate and elaborate, but selecting what is worth knowing remains human (AI Research Assistant PoC: AI lacks question/strategy judgment). | 2026-08 |
| `practices/literature` | ↑ | Citation verification now includes retraction-status checks and mill/laundering awareness; the FirstResearch retraction is the canonical lesson — verify the source before citing it as a source. | 2026-08 |
| `practices/design` | ↑ | Leakage discipline extends to evaluation itself: benchmarks can be contaminated (evidence: test-set retrieval, bogus benchmarks — `ai-era/ai-failure-modes`); sealed test sets stay in design, provenance checks in `practices/verify`. | 2026-08 |
| `practices/measure` | ↑ | Pre-registration moves from best practice to the anti-gaming contract; "who scored it" joins "what does it measure" as a required metric question. | 2026-08 |
| `practices/reproduce` | ↑ | The trace is the evidence: calibration checkpoints and full-trace archives are the practice's core; fresh-context isolation (`practices/verify`) and distributed grounding (`practices/literature`) harden agent pipelines (Grounded Autonomous Research). | 2026-08 |
| `practices/present` | ↑ | Verification levels (formal verifier → self-assessment) must be tagged on every claim; the human checkpoint list (hypothesis selection, interpretation, done-call) is part of the presentation, not an afterthought. | 2026-08 |
| `practices/verify` | ↑ | Audit = re-derive from logs + code (82% vs 55%); adversarial self-review is the documented countermeasure to single-session omissions; demand trace when reviewing others. | 2026-08 |
| Peer review as a quality gate | ↓ | AI-written reviews (21% at one conference), LLM-authored reviews (ICML desk-rejected 500+), paper laundering, and mill saturation make "accepted/published" weak evidence. | 2026-08 |
| Static benchmark SOTA as capability evidence | ↓ | Test-set retrieval (Cursor) and bogus benchmarks mean a number without provenance is a claim, not evidence. | 2026-08 |
| Double-blind anonymity | ↓ | Review agents can match anonymized submissions to arXiv preprints; blind is practically single-blind. | 2026-08 |
| "Published = verified" | ↓ | Mills produce published fake papers at scale (250K+ flagged by a Nature tool); publication status must be checked, not assumed. | 2026-08 |
| + Verification-level tagging | + | Every claim carries its level on the hierarchy (formal verifier → process reward → rubric → self-assessment); at least one claim per project checked above self-assessment. See `ai-era/evaluation-paradigm-shift`. | 2026-08 |
| + Tool-class awareness | + | Name the class (fully-automatic / semi-automatic / assistive) and its verification obligation before using a tool. See `ai-era/ai-research-landscape`. | 2026-08 |
| + Contamination-by-default | + | Treat AI-produced text, citations, numbers, and benchmark scores as unverified until traced. See `ai-era/ai-failure-modes`. | 2026-08 |
| + Named independent verifier | + | "X verifies Y against Z" written into the plan before the work; the producer never certifies the done-call alone. | 2026-08 |
| + Environment-based evaluation | + (emerging) | The next evaluation wave may be agents trained in environments grounded in raw data; treat environment fidelity as a first-class criterion. | 2026-08 |

## Core principles

- **When a principle and a signal disagree** (e.g. pre-registration says the comparison is underpowered, but a benchmark says SOTA): the principles win — signals are the weakened party.
- **When the era adds a discipline**: add it to your checklist alongside the inherited one; do not trade inherited discipline for new (contamination-by-default does not replace sealed test sets, it extends them).
- **When a dated observation expires**: re-check before relying on it; the map is a snapshot, the principles are not.

## Checklist

- [ ] For the current project: for each ↑ row in the map, the corresponding module's checklist contains a check implementing the strengthened bar; spot-check two rows against the project record
- [ ] No weakened signal is used as primary evidence (no "published", "SOTA", "peer-reviewed" as the load-bearing justification)
- [ ] The new disciplines are present: verification levels tagged, tool class named, contamination checks done, independent verifier named
- [ ] Dated observations in this layer are marked with their date and treated as re-checkable
- [ ] The map is applied per project: for every weakened signal used, a strengthened practice substitutes for it

## Anti-patterns

- **Signal substitution**: treating "published in Nature" as proof of correctness in 2026 → Instead: verify the process yourself (`practices/verify`).
- **New-discipline theater**: adding contamination checks to the checklist while dropping pre-registration → Instead: strengthen, never swap.
- **Snapshot freeze**: treating this 2026-08 map as timeless → Instead: re-check the ai-era files before relying on them; the principles stay.

## Bad → good

- **bad**: "It was accepted at a top venue and scored SOTA — that settles it." (two weakened signals, no process evidence)
- **good**: "It was accepted at a top venue — a weak signal. We verified the key numbers from the submitted logs, checked the benchmark's provenance, and confirmed the comparison was pre-registered. That is what settles it."
- **bad**: "We added an AI co-scientist; the era's new disciplines don't apply because it's just a helper."
- **good**: "We named the tool class (semi-automatic), set the human checkpoints before running it, and every hypothesis it generated gets primary-literature verification before entering our pipeline."

## Relationships

- The three files this map controls: `ai-era/ai-research-landscape`, `ai-era/ai-failure-modes`, `ai-era/evaluation-paradigm-shift`.
- The stable layer it upgrades: `principles/` (falsifiability, claim-evidence-alignment, reproducibility, parsimony, honesty) and `practices/` (question, literature, design, measure, reproduce, present, verify).

## Sources

- Same evidence base as the three ai-era files: Lancet fabricated-citations audit (2026); CMU evaluation of AI Scientist (arXiv 2509.08713); Beel, Kan & Baumgart (arXiv 2502.14297); Koppel (2024); ICML desk-reject + 21% AI reviews + paper laundering (2026); Delip Rao on double-blind (2026-08); Tworek "era of evals" (2026-08); Zhen Wang Nature-week analysis (2026-05); burny_tech RSI verification hierarchy (2026-08); Grounded Autonomous Research (arXiv 2607.02329); The AI Research Assistant (arXiv 2602.22842); Flag Game (2026-08) [待核实: paper details]
- Principles layer sources (books/papers) remain authoritative: see each `principles/*/SKILL.md` Sources section
