# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

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

## Sources

- Huang et al., *MLAgentBench* (arXiv 2310.03302, 2023)
- Lu et al., *The AI Scientist: Towards Fully Automated Open-Ended Scientific Discovery* (arXiv 2408.06292, 2024); Nature version 2026: sakana.ai/ai-scientist-nature/; Nature news d41586-026-00969-z
- Gottweis et al., *Towards an AI co-scientist* (arXiv 2502.18864, 2025)
- Nature same-week agent systems, 2026-05: C&EN report (cen.acs.org, 2026-05) + X @zhenwang9102/status/2057207629227667544
- Beel, Kan & Baumgart, *Evaluating Sakana's AI Scientist for Autonomous Research: Wishful Thinking or an Emerging Reality Towards 'ARI'?* (arXiv 2502.14297); Luo et al., CMU (arXiv 2509.08713); *The AI Research Assistant* (arXiv 2602.22842)
- X threads 2026-08: @JeffDean/status/2085034604172603724 (Discovery Loop); @SakanaAILabs/status/2036840833690071450; @advaita_labs/status/2085033352663621689; @ProfBuehlerMIT/status/2062865983459475830; @RaphaelNithin/status/2085447319881670789; @ScottGraffius/status/2085114380799365165 (Flag Game)
- the-decoder.com: Core Automation launch (2026-08)
