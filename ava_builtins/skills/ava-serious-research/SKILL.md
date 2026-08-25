---
name: ava-serious-research
description: Guides trustworthy ML research from question through evidence, verification, and presentation. Use when starting, running, reviewing, or presenting research on LLMs, algorithms, architectures, agents, or interpretability, even if the request begins as a coding task.
---

# Serious Research

## Mission

Serious research is the discipline of producing knowledge that survives
scrutiny. Its purpose chain is explicit: research exists to understand
reality, and understanding serves human flourishing — truth → understanding →
benefit. Every technique in this skill family (questioning, measuring,
presenting) serves that end; "looking like a contribution" is never the
success criterion, and any practice that optimizes for the appearance of
knowledge over actual understanding (overfitting, cherry-picking, hype) is a
failure of the discipline, not a clever shortcut. Most research fails not from lack of cleverness but from broken
discipline: questions nobody would miss if unanswered, evidence that does not
measure what it claims, leakage dressed as discovery, and results presented as
finished when no human could have checked the process. This skill family covers
the full research lifecycle — **question → literature → design → measure →
reproduce → present → verify** — with one explicit boundary: it is about doing
the project well and making the process legible to a human collaborator, **not
about producing papers**. Publishing is a separate craft, and so is experiment
tracking at scale (the Ava task/fleet system covers that).

## Quick Start

Pick the scenario that matches what you are doing; do the 5 must-dos. Each item
links to the skill that spells it out. The full checklists live in the
individual skills — this is the minimum bar, not the whole bar.

**A. Starting a research project**
1. **Write the research question in three-part form** (studying X to find out Y, so that the audience understands Z) and pass the So-what test — `practices/question`
2. **Derive falsifiable hypotheses with a minimal decisive experiment** for at least the first one — `practices/question` + `principles/falsifiability`
3. **Find the live tension in the literature** — what do existing claims assume, what contradicts them, what gap would change what people believe — `practices/literature`
4. **State the target distribution and the evaluation protocol before writing any code** — `practices/design`
5. **Decide what a human will need to see at each checkpoint** — `practices/present`

**B. Designing an experiment**
1. **Split train/val/test before any preprocessing; seal the test set** — `practices/design`
2. **Pick baselines and ablations that isolate one axis at a time** — `practices/design`
3. **Name the metric and its warrant**: what exactly does it measure, and why does that support the claim — `practices/measure`
4. **Pre-register the comparison** (hypothesis, metric, procedure) to cut researcher degrees of freedom — `practices/measure`
5. **Start the reproducibility record on day one** (seeds, configs, environment) — `practices/reproduce`

**C. Interpreting results**
1. **Run the leakage audit before believing any number** (8-category taxonomy) — `practices/design` + `practices/verify`
2. **Report variance across runs and run the right statistical test** for the comparison — `practices/measure`
3. **Attribute negative results**: representation vs optimization vs data vs evaluation — `practices/verify`
4. **Check for p-hacking paths**: was any decision made on the test set, any run dropped after the fact? — `practices/verify`
5. **Write the claim with its qualifiers** — under what conditions does it hold — `principles/claim-evidence-alignment`

**D. Presenting to a human collaborator**
1. **Separate rendering from deciding** — whoever presents the frame must not be the one to declare it done — `practices/present`
2. **Show the process, not just the result**: question → hypotheses → experiments run (including failures) → decisions and why — `practices/present`
3. **State the verification level of each claim** (formal verifier strongest → self-assessment weakest) — `practices/present` + `practices/verify`
4. **Give the human the trace**: logs, code, configs, seeds — `practices/reproduce`
5. **Solicit the strongest objection you can imagine, and answer it** — `practices/verify`

**E. Auditing a research result (yours or someone else's)**
1. **Check leakage first**: is there a truly independent test set? — `practices/design`
2. **Re-derive the key numbers from logs + code** — detection of fabrication jumps from 55% (paper only) to 82% with trace logs — `practices/verify`
3. **Check metric–warrant alignment** — `practices/measure`
4. **Look for selective reporting** (cherry-picked seeds, dropped runs) — `principles/honesty`
5. **Verify citations against primary sources**, including retraction status — `practices/literature`

## The Three Layers

| Layer | What it is | Stability |
|---|---|---|
| `principles/` | Enduring principles — falsifiability, claim–evidence alignment, reproducibility, parsimony, honesty | Stable across eras |
| `practices/` | Research practices — question, literature, design, measure, reproduce, present, verify | Methodology, slowly evolving |
| `ai-era/` | How the AI era changes research — tool landscape & boundaries, known failure modes, evaluation paradigm shift | Fast-moving; each file carries a dated observation |

The three-layer split exists because **principles are stable while their
application is not** (observation, 2026-08: the practical consensus of how AI
agents participate in research turns over on the order of months). When in
doubt about whether a rule is timeless or current, it belongs in the layer that
matches.

## When to Use

- **Starting a research project** → Quick Start A + `practices/question`
- **Designing an experiment** → Quick Start B + `practices/design` + `practices/measure`
- **Results that look too good** → `practices/design` (leakage audit) + `practices/verify`
- **Reporting to a human collaborator** → `practices/present` + `practices/reproduce`
- **Reviewing someone else's results or claims** → Quick Start E + `practices/verify`
- **A claim without a falsifiable test** → `principles/falsifiability`
- **The metric and the claim disagree** → `principles/claim-evidence-alignment` + `practices/measure`

## Working with a Human

This skill family assumes a human collaborator: the output of research here is a
project done well **and a process the human can verify and steer**. That means:

- **Present decisions, not just results.** The human should be able to see the
  question, the hypotheses, the experiments that were run (including the ones
  that failed), and the reasoning at each decision point.
- **Verify before you claim.** Every claim you hand over should carry its
  verification level and its qualifiers. When you have not verified, say so.
- **The human is the final judge.** Your job is to render the frame; the human
  decides when it is done. Never collapse those two roles.
