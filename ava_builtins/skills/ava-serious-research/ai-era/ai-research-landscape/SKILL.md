---
name: ai-research-landscape
description: Maps AI research tools by automation level, limits, and verification obligations. Use when choosing an AI research tool, reviewing tool-produced work, deciding how many agents to use, or comparing automated, semi-automated, and assistive research systems.
---

# AI Research Tools: Landscape and Boundaries

> **Observation dated 2026-08.** This file tracks a fast-moving landscape: the timeline, tool classes, and consensus below were current as of August 2026. Re-check any product-specific claim before relying on it.

## One-sentence core

> AI research tools went from benchmarks (MLAgentBench 2023) through full-pipeline demos (AI Scientist 2024) to industry bets (Discovery Loop, Core Automation, 2026) — and the working consensus moved from "automatically write papers" to "close the experiment loop," which reframes what you should ask any AI research tool to do.

## Timeline (2023–2026, verified)

- **2023 — MLAgentBench** (arXiv 2310.03302): the first benchmark for language agents doing ML experimentation; agents plan and run experiments on real codebases.
- **2024 — Sakana AI Scientist v1** (arXiv 2408.06292): end-to-end pipeline idea → code → experiment → paper; immediately criticized for template-tuning, unrunnable experiments, and hallucinated numbers (Koppel; Beel, Kan & Baumgart arXiv 2502.14297; CMU arXiv 2509.08713 — see `ai-era/ai-failure-modes`).
- **2025-02 — Google AI Co-Scientist** (arXiv 2502.18864): multi-agent hypothesis generation with scientist-in-the-loop (generation / reflection / ranking tournament / evolution).
- **2026-05 — Nature, same week, three agent systems** (2026-05-20): FutureHouse Robin, Google AI Co-Scientist, and a DeepMind empirical-software agent — the first serious wave of "AI did real discovery" claims; commentators noted all were roughly year-old snapshots.
- **2025-03/04 — AI Scientist v2 passes human peer review** (ICLR workshop; the v2 report was reviewed by humans, per the authors).
- **2026-06→08 — The AI Scientist main paper (Lu et al., arXiv 2408.06292) published in Nature.** Core finding: a "scientific scaling law" — AI-generated research quality scales with base-model strength and inference compute (scored by an automated reviewer).
- **2026-08-05 — Jeff Dean co-founds Discovery Loop** (with Ghemawat, Vinyals, Quoc Le): the automated experiment loop (hypothesis → experiment → analysis → iterate) as a company; same week Schrödinger's Bunsen deployed at Bristol Myers Squibb and Owkin's K Pro shipped; Jerry Tworek's Core Automation targets "the most automated AI lab in the world."

## Tool classes and their boundaries

- **Fully-automatic** (AI Scientist v1/v2; the Discovery Loop / Core Automation vision): proposes, runs, and writes up without step-by-step human sign-off. **Boundary**: v1-class systems failed verification (5/12 experiments did not run; 4/7 papers contained hallucinated numbers — arXiv 2502.14297); output quality is only as good as the verification layer around it.
- **Semi-automatic / co-scientist** (Google AI Co-Scientist; Bunsen): generates hypotheses and research strategies; a human selects and steers. **Boundary**: hypothesis generation is cheap; hypothesis *selection* still needs human judgment of value (advaita_labs: the value is choosing the next assay, not writing a better hypothesis).
- **Assistive** (Deep Research; AI Research Assistant PoC, arXiv 2602.22842): AI executes derivation, exhaustive search, literature, and formatting; the human sets questions, filters ideas, verifies key steps. **Boundary**: the human remains the bottleneck on question quality and final verification.

## Core principles

- **Classify the tool before trusting it**: name which class you are using, because the verification obligation differs — fully-automatic = audit everything; assistive = audit only what you delegated — **Why**: the CMU evaluation (arXiv 2509.08713) showed output-only review misses roughly half of fabrication (55% detection without trace logs), and the obligation scales with autonomy — **How**: for any tool, write one line: "this tool proposes / decides / executes X; I must verify Y."
- **Value lives in the closed experiment loop, not in paper text**: judge an AI scientist by whether it improves your next experiment decision, not by how many pages it emits — **Why**: the 2026 consensus (advaita_labs; Buehler; Hassoon & Dredze) is that value sits in choosing the next assay, evolving the search space, and planning for capability; write-up is the easiest part to fake (Koppel) — **How**: after each tool-assisted cycle, ask "did my next experiment get better informed or better chosen?" — if not, the tool is decoration.
- **Keep an explicit human judgment point**: scientist-in-the-loop is the dominant design; whoever renders the frame must not be the one deciding it is done — **Why**: 2026-08 community consensus (dushyantk) and the RSI survey's verification hierarchy (formal verifiers strongest → self-assessment weakest) — **How**: before starting, name the human checkpoints: hypothesis selection, result interpretation, and the done-call.
- **Agent count has an optimum**: more agents is not monotonically better — **Why**: NTT Research × Harvard "Flag Game" found human-AI team performance degrades beyond a threshold number of AI agents [待核实: paper details]; scaling a swarm past the number of independent subtasks adds coordination cost — **How**: run one agent per independent subtask, and measure the marginal contribution of each additional agent instead of adding them because you can.
- **Scaling claims need scaling evidence**: "scientific scaling law" (better model → better AI research) is a real claim with real counter-evidence about what "research" means — **Why**: the scaling metric is an automated reviewer's score, which may measure reviewer taste rather than scientific value (Lauffenburger: narrow definition of science; Koppel) — **How**: when a tool claims scaling, ask what the metric measured, who scored it, and whether the evaluation was output-only.
- **The next wave is trainable environments, not bigger swarms**: single models' native search + reasoning + coding eroded the marginal value of multi-agent scaffolding within a year — **Why**: Zhen Wang's reading of the Nature week (2026-05) and Discovery Loop's stated approach both point to environments where agents learn on raw data — **How**: prefer designs that ground the agent in raw data and a closed loop over designs that bolt more agents onto the same loop.

## Checklist

- [ ] I can name which tool class I am using and what verification that class requires
- [ ] A named human checkpoint exists for hypothesis selection / result interpretation / done-call
- [ ] The tool is judged by its effect on my next experiment decision, not by text volume
- [ ] I know how many agents/assistants I run and why that number (not "more is better")
- [ ] Any "scaling law" claim I rely on states what metric was measured and who scored it
- [ ] My agent's outputs are grounded in raw data or primary literature, not only generated context
- [ ] The snapshot date is noted; product-specific claims are not treated as timeless

## Anti-patterns

- **Demo-chasing**: adopting the newest tool because it made news (Nature week, Discovery Loop launch) → Instead: adopt by matching tool class to your verification budget.
- **Paper-counting**: measuring an AI scientist by papers produced → Instead: measure by experiments correctly chosen and run.
- **Swarm instinct**: spawning 10 agents where 2 would do (Flag Game pattern) → Instead: marginal-contribution check per added agent.
- **Scaling-law worship**: "better model ⇒ better science, so wait for the model" → Instead: the loop and the verification layer are yours to design regardless of the model.
- **Judging the class by one instance**: treating v2's Nature acceptance as proof the class works, or v1's failures as proof it never will → Instead: evaluate the specific system with logs and code.

## Bad → good

- **bad**: "We will use an AI Scientist to write our paper; it passed human peer review, so the output is publishable." (class confusion: fully-automatic output assumed human-grade without audit; ignores the documented 4/7 hallucination rate in v1-class evaluation)
- **good**: "We use a co-scientist for hypothesis generation (semi-automatic). We select hypotheses ourselves, verify the top three against primary literature, and the done-call on any experiment is human. We measure it by whether our next experiment is better informed."
- **bad**: "Add six more agents to the loop so we cover more ground."
- **good**: "We have three independent subtasks; we run one agent per subtask plus one verifier who re-derives the key numbers from logs. A fifth agent was tried and its marginal contribution was negative — removed."

## Relationships

- Tool-class verification obligations connect to `practices/verify` (audit with trace logs) and `principles/honesty` (presenter ≠ judge).
- The value-repositioning (loop > text) reinforces `principles/parsimony`: scaffolding is complexity and must earn its place.
- Known failure modes of these tools: `ai-era/ai-failure-modes`; how to evaluate tools and their outputs: `ai-era/evaluation-paradigm-shift`.
- The calibration-checkpoint pattern from the timeline lives in `practices/reproduce`.

## Sources

- Huang et al., *MLAgentBench* (arXiv 2310.03302, 2023)
- Lu et al., *The AI Scientist: Towards Fully Automated Open-Ended Scientific Discovery* (arXiv 2408.06292, 2024); Nature version 2026: sakana.ai/ai-scientist-nature/; Nature news d41586-026-00969-z
- Gottweis et al., *Towards an AI co-scientist* (arXiv 2502.18864, 2025)
- Nature same-week agent systems, 2026-05: C&EN report (cen.acs.org, 2026-05) + X @zhenwang9102/status/2057207629227667544
- Beel, Kan & Baumgart, *Evaluating Sakana's AI Scientist for Autonomous Research: Wishful Thinking or an Emerging Reality Towards 'ARI'?* (arXiv 2502.14297); Luo et al., CMU (arXiv 2509.08713); *The AI Research Assistant* (arXiv 2602.22842)
- X threads 2026-08: @JeffDean/status/2085034604172603724 (Discovery Loop); @SakanaAILabs/status/2036840833690071450; @advaita_labs/status/2085033352663621689; @ProfBuehlerMIT/status/2062865983459475830; @RaphaelNithin/status/2085447319881670789; @ScottGraffius/status/2085114380799365165 (Flag Game)
- the-decoder.com: Core Automation launch (2026-08)
