---
name: literature
description: Use when reviewing related work, or whenever a citation or an impressive number is about to be trusted — search and synthesize the literature with discipline (three-pass reading, primary-source verification including retraction status, citation tracing, grounding every claim).
---

# Literature Search & Synthesis

## One-sentence core
> The literature review has one product — a live tension you can build a question on — and every claim you take from it is only as good as the primary source it traces to, which you verify yourself, including its retraction status.

## Core principles
- **Three-pass reading**: Pass 1 (5–10 min): title, abstract, introduction, section headings, conclusion, references — enough to classify the paper. Pass 2: careful read of content, figures, and the protocol. Pass 3: re-derive or re-implement the core result. — **Why**: Keshav's "How to Read a Paper": the passes give a decision point at each level of investment, so you read deeply only what matters and skim the rest without guilt. — **How**: Record a pass level and a one-line verdict per paper ("pass 1 — baseline candidate"; "pass 3 — we re-derive Table 2"). Reading everything at pass 2 is the most common waste.
- **Primary-source discipline**: Classify sources relative to your question (primary/secondary/tertiary) and trace every claim to the original. — **Why**: the same material is a primary source for one question and tertiary for another; authority must trace to the original because secondary accounts compress away the details that decide validity. — **How**: For each claim that will support your research, record its primary source (paper/dataset/code) and a location (section, table, line). If you cannot reach the primary source, mark the claim unverified.
- **Verify existence and status of every citation — including retraction status**: — **Why**: a citation is a claim about the literature, and its status can change: a paper that looked citable was withdrawn by its authors (FirstResearch, 2026 — the discipline's cautionary tale); the same habit catches hallucinated references, whose measured 2026 scale is in `ai-era/ai-failure-modes` (Lancet audit: >12× growth, ~4 → 51.3 per 10,000 papers, 2023→Q4 2025). — **How**: Before citing, open the source: DOI/arXiv/venue, authors, date; check publisher retraction and expression-of-concern notices. Verify AI-suggested references the same way — existence first, relevance second.
- **Backward and forward citation tracing**: — **Why**: backward citations find the foundations, forward citations find the live debate — who builds on the claim, who challenges it. A review built from one search is a snapshot, not a map. — **How**: From 2–3 anchor papers, walk the reference chain backward; use a citation index (Google Scholar / Semantic Scholar) forward; stop when the same ~5–10 papers recur — that is the conversation you are joining.
- **Note discipline**: Bibliographic info first; separate source-words from your-words. — **Why**: systematic notes begin with full bibliographic information, and you must distinguish quotation, paraphrase, and your own commentary — otherwise a later you cannot tell what the literature says from what you inferred. — **How**: One note per source: full citation block, then a "The source says" section (quotes and section-anchored paraphrase) and a separate "I think" section (connections, doubts, tensions). Never mix the two.
- **Ground every step in the literature (distributed grounding)**: — **Why**: a claim anchored to a specific location in the literature can be checked, and an unanchored claim cannot; anchoring every step to sources is what keeps a long pipeline from drifting into plausible invention (the 2026 pipeline evidence, from Grounded Autonomous Research, is in `ai-era/ai-research-landscape`). — **How**: Every claim you carry forward carries a citation plus a location; claims without anchors are labeled speculation, not findings.
- **Read numbers with suspicion — protocol first, leakage check second, theory last**: — **Why**: Kapoor & Narayanan found leakage in 329 papers across 17 fields — some "wildly overoptimistic" — none of it detectable by reading the paper alone; Domingos's Lesson 6: theoretical guarantees are usually too loose to carry decisions. — **How**: For every impressive number ask: what protocol produced it, was the test set independent, was the baseline strong and fairly tuned? Compare numbers across papers only when protocols are equivalent (Raschka); treat theory claims as motivation until experiments confirm.
- **Every suggested source is verified, whatever its origin**: — **Why**: a citation is a claim about the literature, and the duty to verify it does not depend on who suggested it; generative AI may fabricate facts and citations and must be verified, and Deep Research–class tools produce fluent, plausible summaries with no reliability guarantee (the 2026 tool landscape is in `ai-era/ai-research-landscape`). — **How**: Treat every AI-returned citation as a lead: open it, verify existence and status, then read. Never copy an AI's reference list into your notes as verified.

## Checklist
- [ ] Each key paper has a recorded pass level and a one-line verdict
- [ ] Every claim that will support the research traces to a primary source with a location (section/table)
- [ ] Citations verified to exist (DOI/arXiv/venue) and checked for retraction / expression of concern
- [ ] Backward and forward tracing done from 2–3 anchor papers; the recurring core set identified
- [ ] Notes separate "the source says" from "I think"; bibliographic info complete before content
- [ ] The live tension is written down: what existing claims assume, what contradicts them, what gap would change belief
- [ ] SOTA/baseline numbers traced to their evaluation protocol; protocol mismatches flagged as non-comparable
- [ ] AI-generated summaries/citations marked unverified until checked against the original

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

## Relationships
- The output of this skill feeds the question: `practices/question` (tension → three-part question)
- Baselines and protocol comparisons for experiments: `practices/design`; citation-as-evidence discipline: `principles/claim-evidence-alignment`
- Verification obligations when citing in reports: `principles/honesty`, `practices/present`
- AI tool boundaries and the hallucinated-citation crisis: `ai-era/ai-failure-modes`; leak-suspicion reading feeds `practices/verify`
- Re-reading with reproducibility in mind (can you re-derive Table 2?): `principles/reproducibility`

## Sources
- Keshav, "How to Read a Paper," ACM SIGCOMM Computer Communication Review, 2007 (three-pass method)
- Kapoor & Narayanan, "Leakage and the Reproducibility Crisis in ML-based Science," Patterns 2023 — `../../references/05-kapoor.md` (leak-suspicion reading)
- Domingos, "A Few Useful Things to Know About Machine Learning," CACM 2012 — `../../references/03-domingos.md` (L6 theory vs empirical verdict)
- Raschka, "Model Evaluation, Model Selection, and Algorithm Selection in ML," arXiv 1811.12808 — `../../references/04-raschka.md` (protocol equivalence for comparing numbers)
- Grounded Autonomous Research, arXiv 2607.02329 (distributed grounding) — evidence also in `ai-era/ai-research-landscape`
- Lancet, *Fabricated citations: an audit across 2·5 million biomedical papers* (2026), PIIS0140-6736(26)00603-3 — >12× growth verified against the primary study — evidence also in `ai-era/ai-failure-modes`
