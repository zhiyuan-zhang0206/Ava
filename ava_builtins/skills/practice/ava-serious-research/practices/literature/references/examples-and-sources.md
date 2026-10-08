# Examples, anti-patterns, and source notes

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Anti-patterns
- **Citation laundering**: citing a paper because a reference list or an AI tool said it exists → verify at the primary source, including retraction status.
- **Reading everything, understanding nothing**: fifty tabs, zero notes → three-pass reading + one note per source.
- **Abstract worship**: believing the abstract's claims — leak-inflated numbers and weak baselines survive abstracts → read the protocol; check test-set independence (Kapoor).
- **Groundless synthesis**: "related work shows …" with no anchor → every sentence carries a source + location.
- **AI oracle**: treating a Deep Research summary as ground truth → it is a lead; verify before use.
- **Bibliography instead of tension**: the review's output is a list of papers → the output is the live tension; the list is only the input.

## Bad → good
- **bad**: "Recent work shows RLHF reduces sycophancy" — citation copied from an AI tool's summary, never opened.
- **good**: The citation is opened: arXiv ID exists, no retraction notice; protocol read — evaluation on one dataset, one model family; the note records "source says X; I think Y, because their evaluation covers a single distribution and the claim may not transfer" — the tension becomes the research question.
- **bad**: Thirty papers skimmed at the same depth, no notes; the review reports "many papers address X."
- **good**: Three anchor papers read at pass 3 with the key tables re-derived, twelve read at pass 1 with verdicts, notes with a source/own split; the review reports "papers A and B assume P; C's result contradicts P under protocol Q — nobody has tested P directly," which is the live tension.

## Sources
- Keshav, "How to Read a Paper," ACM SIGCOMM Computer Communication Review, 2007 (three-pass method)
- Kapoor & Narayanan, "Leakage and the Reproducibility Crisis in ML-based Science," Patterns 2023 — `../../../references/05-kapoor.md` (leak-suspicion reading)
- Domingos, "A Few Useful Things to Know About Machine Learning," CACM 2012 — `../../../references/03-domingos.md` (L6 theory vs empirical verdict)
- Raschka, "Model Evaluation, Model Selection, and Algorithm Selection in ML," arXiv 1811.12808 — `../../../references/04-raschka.md` (protocol equivalence for comparing numbers)
- Grounded Autonomous Research, arXiv 2607.02329 (distributed grounding) — evidence also in `ai-era/ai-research-landscape`
- Lancet, *Fabricated citations: an audit across 2·5 million biomedical papers* (2026), PIIS0140-6736(26)00603-3 — >12× growth verified against the primary study — evidence also in `ai-era/ai-failure-modes`
