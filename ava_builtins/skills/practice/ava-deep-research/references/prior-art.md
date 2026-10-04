# Prior Art — what the frontier deep-research systems do

Researched 2026-08-09 (web + official docs). This file records the design's
provenance: what each system does, what we borrow, and what we deliberately do
not copy. It is reference material — read once, then forget the details.

## Gemini Deep Research (Google DeepMind)

What it does:
- A **custom post-trained model variant** (not the stock API model) trained to
  plan, search, read, and synthesize agentically; Google's "Agentic RAG".
- **Collaborative planning**: before researching, it lays out a multi-step
  plan and asks the user to confirm or edit it (the plan is an editable
  "chain of thought").
- **Iterative loop**: plan → search → deep-dive into pages → revise plan as
  findings arrive; breadth-first exploration, then selective depth-first into
  gaps, contradictions, and highly relevant sources. Parallel searches +
  sequential refinement (find an EU regulation, then search for the FDA
  equivalent).
- **Publisher-forward citations**: full source attribution throughout the
  report; users see live which sites are being read and can click sources
  while research runs.
- **Report**: long-form structured report with an executive summary,
  thematic sections matching the plan's sub-questions, tables, and a source
  list; export to Google Docs preserves citations.
- Evaluation: automated behavioral metrics (plan length, websites browsed,
  search-to-browse ratio) as early-warning signals; human evaluation on
  comprehensiveness / completeness / groundedness / accuracy across a
  use-case ontology (broad-shallow, deep-narrow, comparison, compound).
- UX finding: longer research times were perceived as *more* thorough — users
  liked the visible effort.

What we borrow: the confirm-the-plan step (Phase 1, step 5); breadth-then-depth
iteration (Phase 2 → 3); the use-case framing that research "journeys" differ
and the process must adapt.

## Perplexity Deep Research

What it does:
- **Iterative search → read → reason loop**: "iteratively searches, reads
  documents, and reasons about what to do next, refining its research plan as
  it learns more" — dozens of searches, hundreds of sources.
- **Fact-checking**: source materials are "fully evaluated" before report
  writing; claims 93.9% on SimpleQA (factuality benchmark).
- **Report**: clear, comprehensive synthesis; exportable to PDF/doc or a
  shareable Perplexity Page.
- Product shape: no plan-approval gate — it starts researching immediately
  and finishes fastest of the big three (per Ava's web-ai/deep-research
  experience).

What we borrow: evaluation-before-synthesis as a distinct stage (Phase 4);
the insight that a no-gate fast path is a legitimate mode (Phase 1 step 5's
"just research it" default).

## OpenAI deep research

What it does:
- A version of the o3 model **trained with reinforcement learning** on
  real-world tasks requiring browser and Python tool use; plans multi-step
  trajectories, browses, reacts, pivots/backtracks; uses Python for analysis
  and plotting.
- **Sentence-level citations**: it cites specific passages from sources, with
  a summary of its thinking so users can review the process.
- Report: comprehensive, with side-by-side comparisons, tables, structured
  data, and recommended actions.
- **Stated limitations** (from the launch post): can hallucinate facts or
  make incorrect inferences (at a lower rate than earlier models); struggles
  to distinguish authoritative information from rumors; weak confidence
  calibration; minor formatting errors; 5–30 min runtime.

What we borrow: sentence/claim-level citation granularity (Phase 5); the
explicit limitations list — our "Known limitations" section exists because
OpenAI published theirs and it is honest; the confidence-calibration warning
maps to our confidence tags.

## Claude Code deep research (the /deep command and ecosystem skills)

What it does:
- **Parallel subagents**: spawns multiple subagents simultaneously, each
  researching a different angle with its own context and tools.
- **Cross-checking / voting**: claims found consistently across independent
  agents/sources get higher confidence; single-agent claims are flagged
  uncertain.
- **Cited synthesis**: the orchestrator combines findings into one coherent,
  cited report, reducing single-agent error compounding.
- Ecosystem skills (anthropics/skills and third-party) converge on the same
  shape: clarify → plan (sub-questions + queries) → search → extract →
  cross-check → iterate → synthesize, with inline source-linked citations and
  a structured report (executive summary / findings / contradictions / gaps /
  sources).

What we borrow: parallel subagents per sub-question (Phase 2 — realized
through `ava-dynamic-workflow`); the consensus rule (independent confirmation
raises confidence — our `consensus` tag requires ≥ 2 independent sources);
the ecosystem's report skeleton (our Phase 5 structure).

## dzhng/deep-research (open-source reference implementation)

What it does:
- A tight recursive loop: `deepResearch({query, breadth, depth, learnings,
  visitedUrls})` — generate SERP queries → search + scrape concurrently →
  **reflect** (extract learnings + follow-up directions) → recurse with
  `depth - 1`.
- **State is two JSON arrays**: `learnings` (facts) and `visitedUrls`
  (sources), passed through every recursion; used to avoid re-visiting and to
  seed the final report's citations.
- Stopping is a **pre-registered depth counter** (breadth × depth searches),
  not a learned saturation heuristic.
- Final step: `writeFinalReport(query, learnings, visitedUrls)` — inline
  citations + a sources section.
- Optional LLM follow-up questions to clarify intent before starting.

What we borrow: the learnings + visited-urls state shape (our `sources` +
`learnings` sections — upgraded to a single JSON file with metadata and
invariants); the reflection step (Phase 3); the pre-registered budget as the
stopping mechanism (Phase 1); the follow-up-questions clarification.

## What we deliberately do NOT copy

| Their mechanism | Why we skip it |
|---|---|
| Custom model fine-tuning / RL training | Out of reach for a skill; we achieve discipline through process + deterministic audit instead |
| Closed-loop "the model decides when it's done" | We pre-register the stopping rule in the state file and check it after every wave |
| Trusting the model's self-report of groundedness | The audit script mechanically verifies citation→source consistency; self-assessment is the weakest rung of the ladder |
| Vendor-specific report artifacts (Google Docs export, Pages) | Ava delivers through `ava.ui.serve` of a self-contained HTML page — one channel, no vendor lock |
| Browser-driving automation of paid seats (web-ai/deep-research does this deliberately) | Different cost model: native uses `ava.web` API search/fetch; the browser seat is a separate skill |
