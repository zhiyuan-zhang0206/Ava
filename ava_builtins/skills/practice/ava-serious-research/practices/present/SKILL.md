---
name: present
description: "Presents research with traceable claims and verification levels. Use when reporting experimental progress, results, limitations, or a research handoff."
---

# Presenting Research to a Human

## One-sentence core

> Presenting research means handing over a process a human can verify and steer — the question, the hypotheses, the experiments (failures included), and every decision point — while keeping two roles apart: whoever renders the frame must not be the one who declares it done.

## Core principles

- **Rendering ≠ deciding**: the person or system that presents a result must never be the one who declares it done — **Why**: whoever both produces and judges an account has no check on motivated framing; every institution of credible review — courts, peer review, auditing — separates advocate from adjudicator, and once the roles merge review becomes a formality (the 2026-08 articulation for AI research: `ai-era/ai-failure-modes`) link — **How**: at every milestone, name the human checkpoint explicitly ("I present the frame; you decide when it is done") and never mark your own work complete without an independent pass.

- **Present the process, not just the result**: the deliverable is question → hypotheses → experiments run (failures included) → decision points with reasoning — **Why**: an argument persuades only when the audience can follow the chain from claim to evidence to warrant, and the process is the only reliable carrier of what actually happened — the 2026 measurement that verification scales with the trace (55% → 82% with logs + code) is in `ai-era/ai-failure-modes` — **How**: structure every report as a decision trail: the research question, each hypothesis, each experiment with a one-line result and why it was run, each decision with the options considered; attach every headline number to its run (command, log path, seed, commit).

- **State each claim's verification level**: tag every headline claim with how it was verified, using the canonical ladder — formal verifier (strongest) → process-level check → rubric review → self-assessment (weakest) — **Why**: a reader cannot weigh a claim without knowing how it was checked; evidence grading is the backbone of evidence-based practice, and presenting an unverified claim as verified is deception even when unintentional. The ladder is the 2026 operationalization of this duty (RSI survey; `ai-era/evaluation-paradigm-shift`) — **How**: label claims [formal verifier] / [process-level: reproduced from logs] / [process-level: statistical test] / [rubric review] / [self-assessment]; when a claim is not verified, write "not verified" and state what verification would take.

- **Analyze the reader before presenting**: know what the human already knows, what they doubt, and what you need them to do — **Why**: research is a conversation with a community: you can only move a reader once you know their prior beliefs, whether your question is a live question for them, and which side they stand on (reader analysis) — **How**: before writing, answer three questions in a note: what does the human know? what will they suspect first? what decision do I need from them? Then structure the report to answer the suspicion explicitly and end with the decision.

- **Human-led, AI-executed division of labor**: the human sets the questions, filters the ideas, and verifies the key steps; AI executes derivation, exhaustive search, literature, and formatting, and renders the reasoning — **Why**: accountability cannot be delegated: whoever answers to the research community must control the question and the verdict; execution and rendering can be delegated, judgment cannot. In practice AI can err at every step while appearing plausible and lacks the ability to pose good questions and set strategic direction, so the human must keep the lead and a posture of systematic skepticism (AI Research Assistant PoC, 2026; `ai-era/ai-research-landscape`) — **How**: for each phase of the work, write down who owns what: human owns problem-setting, idea filtering, key-step verification, and value judgment; AI owns execution and the presentation of the derivation — and keep the human's ownership visible in the report.

- **Make decision points transparent**: stop and present options at every node that needs human judgment — **Why**: a system that hides its decision points removes the human's ability to steer; the human-led model only works if the forks are surfaced with their evidence (AI Research Assistant PoC, 2026; `ai-era/ai-research-landscape`) — **How**: at each fork (which hypothesis next, which baseline, whether to drop a failed direction) present: the options considered, the evidence for each, your recommendation, and an explicit request for the human's call.

## Checklist

- [ ] The report opens with the research question in three-part form (studying X to find out Y, so that the audience understands Z) and the target distribution
- [ ] Every experiment is listed, including failed ones, each with a one-line result and why it was run
- [ ] Every decision point shows the options considered, the evidence, and which option was chosen and why
- [ ] Each headline claim carries its verification level (formal verifier / process-level: reproduced from logs or statistical test / rubric review / self-assessment); unverified claims say so
- [ ] The reader analysis is written down: what the human knows, what they will doubt, what decision I need from them
- [ ] The human checkpoint is explicit: exactly what the human must decide at this milestone (renderer ≠ decider)
- [ ] Every headline number links to a run (command, log path, seed)
- [ ] The report states the strongest objection the presenter can imagine — and answers it, or asks the human for it
- [ ] AI-generated content in the report is labeled, and anything AI-generated that feeds a decision has been human-verified

## Relationships

- Root SKILL.md Quick Start D (Presenting to a human collaborator) — `../../SKILL.md`
- What to render: `practices/design` (decision points), `practices/measure` (statistical claims and their strength), `practices/reproduce` (the trace you hand over)
- What a claim may say: `principles/claim-evidence-alignment` (qualifiers, warrants), `principles/honesty` (renderer ≠ decider is also an honesty obligation)
- The verification levels presented here are produced by `practices/verify` — boundary: **present owns transparency and collaboration; verify owns trustworthiness**
- AI-era caveats on self-assessment and review: `ai-era/ai-failure-modes`

## Examples and sources

Read [examples and sources](references/examples-and-sources.md) when a concrete
counterexample, worked example, or source context would clarify these decisions.
Use the core guidance above directly for routine work.
