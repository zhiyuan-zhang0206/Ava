# Ritchie, Science Fictions — Reference Material

**Source**: Stuart Ritchie, *Science Fictions: How Fraud, Bias, Negligence, and Hype Undermine the Search for Truth* (Metropolitan Books / Penguin, 2020).
**Role in this skill**: the evidence base for `principles/honesty` and `practices/verify` — concrete, documented cases of how research goes wrong when truth-seeking is displaced by career incentives; the empirical companion to Feynman's attitude argument.

## Core content

- **Four failure families** (the book's structure):
  1. **Fraud** — outright fabrication and falsification. Cases: Diederik Stapel (social psychology, dozens of fabricated studies), Brian Wansink (the Cornell food-lab "p-hacking" scandal where data were re-analyzed until significant), Marc Hauser (Harvard, fabrication in primate cognition).
  2. **Bias** — researcher degrees of freedom: analyzing data every which way and reporting only what comes out significant; the "garden of forking paths." The *Many Analysts, One Data Set* project (Silberzahn et al. 2015): 29 teams analyzed the same data and reached 29 materially different conclusions.
  3. **Negligence** — sloppy methods, unreported flexibility, statistical misunderstandings; results that are not reproducible because the record is incomplete (which is exactly why this skill's reproduce practice demands seeds, configs, environment, and logs from day one).
  4. **Hype** — overclaiming: press releases and abstracts that outrun the evidence (e.g., the "power pose" effect that failed replication after massive media coverage).
- **Why it happens**: incentives — publish-or-perish, novelty bias, no reward for negative results; the replication crisis in psychology (only ~36% of a sample of 100 studies replicated, Open Science Collaboration 2015) is the aggregate symptom.
- **What helps** (the constructive half): pre-registration, open data/code, reporting negative results, adversarial review — all of which this skill already encodes.

## Why it matters for ML research

- ML has the same incentive structure plus higher stakes (benchmark leaderboards, startup claims, AGI timelines) — every failure family in the book has a direct ML analogue:
  - fraud → fabricated benchmark numbers and "AI Scientist" papers with hallucinated results (see `ai-era/ai-failure-modes`)
  - bias → benchmark cherry-picking, seed selection, metric shopping
  - negligence → unreported hyperparameter search, missing seeds/configs
  - hype → capability claims that outrun evaluation evidence
- The book's cases are citable, concrete evidence for the skill's audit checks: when a result "looks too good," the failure families give the auditor a checklist of where to look (fabrication / flexibility / incompleteness / overclaiming).

## Key numbers (verified against the book)

- Many Analysts, One Data Set: 29 teams, 29 materially different conclusions from the same data (Silberzahn et al., 2015, *Analyzing the analyzeers*).
- Open Science Collaboration (2015): 97 original studies replicated; ~36% produced statistically significant results in the same direction as the original.
- Wansink's papers were retracted after an internal review found p-hacking and unverifiable data (Cornell investigation concluded 2018).

## Checklist candidates

- [ ] MUST the audit considers the four failure families (fraud / bias / negligence / hype) when a result looks too good
- [ ] MUST researcher degrees of freedom are cut by pre-registration (hypothesis, metric, procedure before results)
- [ ] SHOULD claims in abstracts/presentations do not outrun the evidence (anti-hype)
