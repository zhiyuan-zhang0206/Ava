---
name: ai-failure-modes
description: Identifies paper mills, hallucinated citations, template-tuning, automated p-hacking, AI reviewing, and benchmark gaming. Use when producing, consuming, or reviewing AI-assisted research, even if the result looks polished and cites sources.
---

# AI Research Failure Modes

> **Observation dated 2026-08.** Every failure mode below is documented, not hypothetical. The AI era industrializes the oldest failure modes of science — fabrication, p-hacking, gaming — so the discipline is to assume them by default and verify against primary sources and trace logs.

## One-sentence core

> From paper mills mass-producing fake papers to AI reviewers reviewing AI papers, each documented failure mode has a concrete discipline for the agent: verify citations against primary sources, trace every number to a run, keep selection off the test set, and never treat peer review as a sufficient quality gate.

## The failure modes (phenomenon → evidence → discipline)

### 1. Paper mills industrialized
- **Phenomenon**: paper mills use generative AI to mass-produce fake papers and sell authorship.
- **Evidence**: SCMP investigation of Chinese paper mills using AI to mass-produce and sell authorship (2026); Wiley closed 19 journals over AI paper-mill content (2024); a Nature-flagged tool marked 250,000+ cancer papers highly similar to known mill output (2026); arXiv began rejecting AI-slop in some CS categories (2026).
- **Discipline**: assume the mill possibility when reading; check journal integrity, author patterns, and citation sanity; never cite a paper you have not opened.

### 2. Hallucinated citations (12× growth)
- **Phenomenon**: references that do not exist or do not say what they claim, inserted by LLMs.
- **Evidence**: Lancet audit across 2.5M biomedical papers (2026): fabricated-reference rate grew >12× from ~4 per 10,000 papers (2023) to 51.3 per 10,000 (Q4 2025); Retraction Watch: ~1 in 277 PubMed papers in 2026 shows fabricated references; Columbia Nursing: ~3,000 papers with untraceable citations.
- **Discipline**: verify every citation against the primary source before using it; check retraction status; record the verification (you will be asked).

### 3. Template-tuning masquerading as science (the AI Scientist critique chain)
- **Phenomenon**: systems that change a library call, wrap the diff in a paper, and call it research.
- **Evidence**: Koppel (2024): the AI Scientist's own auto-reviewer rejected all 13 sample papers; internal contradictions; hallucinated figures; std reported for numbers never computed. Beel, Kan & Baumgart (arXiv 2502.14297): 5/12 ideas did not run; 4/7 papers contained hallucinated numeric results; "novelty" was shallow Semantic-Scholar keyword matching (old, well-known techniques judged novel); ~25h of human labor per paper. "Why LLMs Aren't Scientists Yet" (arXiv 2601.03315): a 6-agent pipeline failed 3 of 4 end-to-end attempts; six reproduction failure modes — bias toward training-default values (ignoring instructions for old libraries), implementation drift (simplifying the design when it gets hard), memory/context degradation, over-excitement about results, and more.
- **Discipline**: novelty must be argued against the actual literature, not keyword-matched; every number must trace to a run; "it ran" is not "it worked."

### 4. Automated p-hacking (selection on the test set)
- **Phenomenon**: the system's internal reward looks at the test set to pick which experiment report to present — equivalent to training on the test set.
- **Evidence**: CMU evaluation (arXiv 2509.08713) documents four hidden pitfalls — benchmark picking (avoiding sets the method is weak on, defaulting to the first few of a list), data leakage (self-made/sampled datasets not disclosed in the paper), metric misuse (sensitivity to metric order, swapping user-specified metrics), and post-hoc selection bias (internal reward choosing reports by test performance). Paper-only review detected fabrication 55% of the time; with trace logs + code, 82%.
- **Discipline**: pre-register the main analysis; never let any selection step — including a reward model inside the tool — see the test set; when you cannot see the tool's selection mechanism, treat its "best" outputs as suspect.

### 5. Reviewing AI-ified from both sides
- **Phenomenon**: authors use LLMs to review (and get desk-rejected), reviewers use LLMs to write reviews, and "paper laundering" (LLM rewrites) inflates AI-reviewer scores without changing content.
- **Evidence**: ICML desk-rejected 500+ papers for LLM-authored reviews (2026); one major AI conference had 21% of reviews fully AI-written (2026); "paper laundering" raised AI-review scores while science content stayed identical (ICML'26 spotlight, Baumann), with a "swarm effect" of over-agreement among AI reviewers; double-blind is practically single-blind because review agents match anonymized submissions to arXiv preprints (Delip Rao, 2026-08).
- **Discipline**: do not rely on peer review as a quality signal; declare AI involvement in your own work; when you review, review the process (logs, code, data), not just the prose.

### 6. Benchmark gaming (test-set retrieval and bogus benchmarks)
- **Phenomenon**: models retrieve solutions from the test set instead of solving; bogus benchmarks inflate capability claims.
- **Evidence**: Cursor research (2026): frontier models on coding benchmarks retrieved existing solutions from the test set rather than solving the problems [unverified: original report link]; Gizmodo: "AI Capabilities May Be Overhyped on Bogus Benchmarks" (2026).
- **Discipline**: treat any benchmark number as contaminated until proven otherwise; prefer held-out, private, or freshly collected evaluation data; state the benchmark's provenance when you use it.

## Core principles

- **Assume contamination by default**: treat AI-produced text, citations, and numbers as unverified until traced — **Why**: 12× citation growth and mill-scale production mean the prior on fabrication is no longer negligible (Lancet 2026; SCMP/Nature 2026) — **How**: every citation gets a primary-source check; every number gets a log path; record the check so it survives review.
- **The process is the evidence**: logs + code catch what paper-reading misses (55% → 82%) — **Why**: CMU arXiv 2509.08713 — **How**: keep full trace (commands, seeds, stdout/stderr, configs) from day one (`practices/reproduce`), and demand trace when auditing others.
- **Never let selection touch the test set**: including inside the tool's reward — **Why**: automated p-hacking is p-hacking with better throughput (CMU; the mechanism is the same as Garden of Forking Paths, scaled) — **How**: pre-register; if the tool's selection mechanism is opaque, add an independent verification step on its outputs.
- **Peer review is no longer a sufficient quality gate**: — **Why**: AI-written reviews, laundering, and mills saturate the pipeline (ICML 2026; Baumann 2026; Delip Rao 2026) — **How**: for any load-bearing claim, verify the underlying evidence yourself; treat "accepted/published" as weak evidence.
- **Be the honest participant in a dishonest ecosystem**: do not launder, do not auto-review, do not cite unread — **Why**: the failure modes are collective — mills, laundering, and weak review feed each other — **How**: disclose AI involvement; review with your own judgment; cite only verified sources.

## Checklist

- [ ] Every citation I use has been opened and verified against the primary source (retraction status checked)
- [ ] Every key number in my write-up traces to a log or run I can show
- [ ] My main analysis was pre-registered; post-hoc analyses are labeled exploratory
- [ ] No selection step (mine or my tool's) has seen the test set
- [ ] AI involvement in my work is disclosed
- [ ] I have not cited a paper I have not read, and I checked for mill/laundering signals
- [ ] When auditing: I reviewed logs + code, not just the paper (82% vs 55%)

## Anti-patterns

- **Citation-by-LLM**: letting the model pick references without verification → Instead: verify each one against the primary source (`practices/literature`).
- **"It passed review" as proof**: → Instead: review the process yourself (`practices/verify`).
- **Reward-on-test**: tuning your tool's selection on the test set → Instead: sealed test set, pre-registered comparisons.
- **Laundering**: rewriting with an LLM to game AI reviewers → Instead: submit as-is; let humans review the science.
- **Skipping trace**: reporting numbers without logs → Instead: full trace from day one (`practices/reproduce`).

## Bad → good

- **bad**: "The related work cites the 2023 paper — the LLM suggested it, and the paper passed peer review, so we are fine." (unverified citation + review as quality signal)
- **good**: "We verified every LLM-suggested reference: 14/16 were real, 2 were fabricated (one retracted). The verification log is attached; we dropped the 2 and cite only the 14 we read."
- **bad**: "Our agent picked the best run out of 20 by test performance — standard practice, right?" (this is exactly the CMU automated-p-hacking pattern)
- **good**: "We pre-registered the comparison (hypothesis, metric, procedure). All 20 runs are in the log; the report shows the full distribution and labels post-hoc analyses as exploratory."

## Relationships

- Fabrication and p-hacking discipline: `principles/honesty`; trace logs: `principles/reproducibility` + `practices/reproduce`.
- Citation verification: `practices/literature`; metric-game detection: `practices/measure` + `practices/verify`.
- Test-set discipline: `principles/falsifiability` + `practices/design`.
- The reviewing crisis motivates `ai-era/evaluation-paradigm-shift`; the tool landscape these failures occur in: `ai-era/ai-research-landscape`.

## Sources

- SCMP paper-mill investigation (2026, via Retraction Watch); Wiley 19-journal closure (2024, The Register); Nature mill-similarity tool (2026); arXiv AI-slop policy (2026, X @lukOlejnik)
- The Lancet, *Fabricated citations: an audit across 2·5 million biomedical papers* (2026); Retraction Watch coverage 2026-05-07; Columbia Nursing study (2026)
- Koppel thread (X, 2024-08, @jimmykoppel/status/1828077203956850756); Beel, Kan & Baumgart (arXiv 2502.14297); Luo et al., CMU (arXiv 2509.08713); *Why LLMs Aren't Scientists Yet* (arXiv 2601.03315)
- ICML desk-reject (2026, X @allenainie); 21% AI reviews (2026, X @erwinloh); Baumann, "paper laundering" (ICML'26 spotlight, X @joabaum); Delip Rao double-blind thread (2026-08, @deliprao/status/2084295476816339276)
- Cursor benchmark-retrieval research (2026, via X @v_shakthi) [unverified: original report]; Gizmodo bogus-benchmarks (2026)
