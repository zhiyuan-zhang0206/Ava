# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

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

## Sources

- SCMP paper-mill investigation (2026, via Retraction Watch); Wiley 19-journal closure (2024, The Register); Nature mill-similarity tool (2026); arXiv AI-slop policy (2026, X @lukOlejnik)
- The Lancet, *Fabricated citations: an audit across 2·5 million biomedical papers* (2026); Retraction Watch coverage 2026-05-07; Columbia Nursing study (2026)
- Koppel thread (X, 2024-08, @jimmykoppel/status/1828077203956850756); Beel, Kan & Baumgart (arXiv 2502.14297); Luo et al., CMU (arXiv 2509.08713); *Why LLMs Aren't Scientists Yet* (arXiv 2601.03315)
- ICML desk-reject (2026, X @allenainie); 21% AI reviews (2026, X @erwinloh); Baumann, "paper laundering" (ICML'26 spotlight, X @joabaum); Delip Rao double-blind thread (2026-08, @deliprao/status/2084295476816339276)
- Cursor benchmark-retrieval research (2026, via X @v_shakthi) [unverified: original report]; Gizmodo bogus-benchmarks (2026)
