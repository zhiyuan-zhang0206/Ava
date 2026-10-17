# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Anti-patterns
- **Benchmark padding**: "We beat SOTA on dataset B by 2 points" — nobody loses if it stays unanswered → Replace: state the phenomenon claim and name the audience that loses.
- **Vague curiosity**: "I want to understand transformers" — a topic, not a question → Replace: narrow through interest → topic → question → research question until Y is answerable.
- **Post-hoc hypothesis (HARKing)**: results first, hypothesis second → Replace: pre-register; label any later revision as a revision.
- **Unfalsifiable framing**: "our method is more robust / more general" with no boundary → Replace: define robustness operationally and state the refuting observation (`principles/falsifiability`).
- **Prediction evidence wearing causal clothes**: "these features predict X, therefore raising X improves Y" → Replace: classify as prediction, or design the causal experiment (Domingos L12).
- **Question soup**: five research questions in one project → Replace: one primary question; the rest become explicit secondary/exploratory items.

## Bad → good
- **bad**: "We study LoRA fine-tuning of code LLMs." (a topic — no Y, no Z)
- **good**: "I am studying LoRA fine-tuning on code LLMs (X) to find out whether rank determines where the gain comes from — data fidelity vs optimization ease (Y) — so that practitioners can pick rank by their actual bottleneck (Z)." Then the hypothesis: "at low data budgets, rank gains come from optimization ease; refutation: holding budget fixed, low-rank matches high-rank under the same optimizer settings."
- **bad**: "Our method achieves 92.3% on benchmark B, +2.0 over SOTA." (no one loses if it never existed)
- **good**: "Question: does multi-stage training reduce hallucination on out-of-distribution queries? Hypothesis: the gain comes from stage-2 data mixing, not scale; if we hold compute fixed and remove stage-2, the gap disappears. So-what: OOD hallucination is the reported production failure for this model family; a recipe that fixes it removes a known deployment blocker."

## Sources
- Popper, *Conjectures and Refutations*, 1963 — `../../../references/06-poppers-conjectures-and-refutations.md` (better problems as progress; outcome typology)
- Deutsch, *The Beginning of Infinity*, 2011 — `../../../references/08-deutsch-beginning-of-infinity.md` (problems are inevitable and solvable; better problems as the scoreboard)
- Domingos, "A Few Useful Things to Know About Machine Learning," CACM 2012 — `../../../references/03-domingos.md` (L12 prediction vs causation)
- Gelman & Loken, "The Garden of Forking Paths" (2013) — post-hoc analysis paths
- FirstResearch, "Auditable Question Formation for LLM Scientific Discovery Agents," arXiv 2607.05682 — withdrawn by authors; question-certificate design idea only, not an established result — evidence also in `ai-era/ai-research-landscape`
- The AI Research Assistant, arXiv 2602.22842 (human-led question setting; multi-assistant + human-led pattern) — evidence also in `ai-era/ai-research-landscape`
