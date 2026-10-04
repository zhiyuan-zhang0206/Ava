# 07 · The Structure of Scientific Revolutions — Reference

**Author**: Thomas S. Kuhn (1962; 2nd ed. 1970 adds the postscript, 3rd ed. 1996)
**Source**: University of Chicago Press; Chinese translation: The Structure of Scientific Revolutions.
**Role in this skill**: the structural model behind `practices/question` (anomaly-driven research), `practices/measure` (framework-relative metrics), `practices/verify` (anomaly vs noise), and `practices/present` (honest reporting of crisis/confusion as a legitimate outcome).
**Review status (Chinese edition)**: ⏳ pending user review (2026-08-07, epistemology supplement group).

## 0. One-line core

> Science is not linear accumulation of facts but an alternation of **puzzle-solving within a paradigm** and **revolution between paradigms**: normal science solves puzzles inside an accepted framework; anomalies accumulate until crisis breaks out; a new paradigm replaces the old in the sense that "the world changes" — **discovery commences with the awareness of anomaly**, and a surge of confusion is the normal prelude to revolution.

## 1. Normal science and puzzle-solving

- Most research is **normal science**: solving puzzles inside a paradigm (the accepted framework: theory + methods + exemplars).
- Puzzle-solvers do not question the paradigm — like chess players not questioning the rules; that is exactly what makes normal science efficient.
- **Operationalization for ML research**: most ML work (new datasets, architecture tweaks, benchmark improvements) is paradigm-internal puzzle-solving. That is not wrong — but know that you are solving puzzles: you are improving numbers inside an accepted evaluation framework, not questioning the framework.

## 2. Anomaly, crisis, revolution

- **"Discovery commences with the awareness of anomaly"**: when observation clashes with paradigm expectation and cannot be dissolved by existing rules, an anomaly appears.
- A single anomaly is ignored or filed as noise; anomalies accumulate → crisis: the paradigm loses its puzzle-solving power, the community splits, new theories compete.
- Revolution = paradigm shift: the new paradigm redefines problems, methods, even "what counts as an explanation"; old and new paradigms are **incommensurable** — same terms, different meanings.
- **Operationalization**:
  - Anomaly ≠ noise — tracking anomalies is a source of discovery (the historical basis of `practices/question`'s "tension in the literature").
  - When your result conflicts with the mainstream framework, first ask: is this an anomaly dissolvable inside the framework, or a phenomenon pointing outside it? Report honestly as "possible anomaly" instead of hurriedly "fixing the bug."
  - Cross-framework comparison is treacherous: metrics from different schools (frequentist vs Bayesian, benchmark-driven vs mechanism-driven) are not directly comparable.

## 3. Growing confusion = crisis signal, not failure

- The direct answer to the critique "research sometimes increases confusion": in Kuhn's model, **growing confusion is precisely the marker of moving from normal science toward revolution** — old answers fail, new questions surge, boundaries blur.
- For the researcher: confusion in crisis is information, not shame. Honestly reporting "the current framework cannot explain this result" is worth more than pretending everything is fine.
- For the skill: outcome type (b) (better-posed confusion) has a special status in Kuhn's framework — a sharpened list of anomalies is the fuel of paradigm change.
- **Operationalization**: when benchmarks broadly fail and new capabilities escape old metrics (the 2024–2026 evals crisis discussion is an instance), that is a discipline-level crisis signal — "maintaining the old metrics" and "searching for a new framework" are two different research behaviors, and one should be conscious of which one is being performed.

## 4. The paradigm-relativity warning for the skill

- **Metrics are paradigm-relative**: the meaning of an indicator is conferred by the framework. The same "accuracy" can denote different things in two paradigms.
- Therefore `practices/measure`'s warrant requirement ("what exactly does this metric measure?") must go one level deeper: **under what framework does this metric hold?**
- Incommensurability is not "cannot compare" but "state each framework before comparing" — when a literature review meets a paradigm-level disagreement, describe the framework difference first, then weigh merits.

## 5. Contribution mapping to ava-serious-research

| Module | Contribution |
|--------|--------------|
| question | Anomaly awareness as a research trigger; historical basis of "tension"; legitimacy of question surges in crisis |
| literature | Identifying and presenting paradigm-level disagreements (incommensurability → state frameworks before comparing) |
| design | Designing experiments that distinguish framework-internal anomalies from framework-external phenomena |
| measure | Metric meaning is framework-relative — warrant must declare its framework |
| verify | Discipline of distinguishing anomaly from noise; not "fixing away" anomalies |
| present | Honest reporting of crisis/confusion as legitimate output; presenting framework boundaries instead of pretending universality |
| ai-era | The evals crisis = an instance of discipline-level paradigm crisis (framing the 2024–2026 discussion) |

## Checklist candidates

- [ ] MUST anomaly is distinguished from noise: anomalies enter a tracking list, noise is recorded and set aside
- [ ] MUST metrics/indicators declare their framework and scope of validity
- [ ] MUST conflicts with the mainstream framework are reported as "possible anomaly," not silently "corrected"
- [ ] SHOULD the literature review marks paradigm-level disagreements (state frameworks where incommensurable)
- [ ] SHOULD project docs record: is this work puzzle-solving inside a paradigm, or challenging the paradigm?
- [ ] SHOULD presentation states the framework-dependence of conclusions (do they survive a framework change?)
