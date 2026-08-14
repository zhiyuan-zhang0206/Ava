# Citation & Verification Discipline

Read this before Phase 4 (Verify & audit). It defines what a citation is,
what the verification ladder means in this skill, and how confidence is
tagged. The epistemology behind it is `ava-serious-research`
`practices/literature` and `practices/present`; this file is the web-research
operationalization.

## What a citation is

A citation is a **claim about the literature**: "this source exists, says
this, and supports this statement." All three parts must be true, and the
audit script mechanically checks the first and third (source exists in the
state; the statement's citation resolves). The middle part — the source really
says what you attribute — is checked by you, at Phase 4, by opening the source.
No exception for LLM-suggested references: existence first, relevance second.

## Inline citation format

- Every significant claim carries an inline numbered citation: `[12]`, or
  `[4, 17]` when multiple sources support it.
- Numbers are the integer source ids from `research_state.json` — no separate
  numbering scheme to drift.
- Cite after the claim, not at the paragraph end for a whole paragraph of
  mixed provenance. One claim, one citation.
- The report ends with a numbered source list rendered from the state's
  `sources` (url, title, publisher, date, accessed_at).
- **Never wrap bare years in square brackets** (`[2026]` reads as citation
  number 2026 and the audit will flag it). Write `2026` or `(2026)`.
- Code blocks and inline code spans are ignored by the audit's citation scan —
  but a citation inside a code block is still not a citation; cite in prose.

## The verification ladder (how a claim was checked)

| Level | Meaning | In this skill |
|---|---|---|
| formal verifier | machine-checked proof | (rarely available) e.g. a calculation re-run |
| process-level | a deterministic procedure checked it | the audit script — citation-sourcing consistency, consensus rule |
| rubric review | a human/LLM judged it against criteria | the contradictions section review, primary-source reading |
| self-assessment | the producer asserts it | "I believe this is right" — never sufficient alone for a headline claim |

Every claim in the report's key findings carries one of these labels. When a
claim is not verified, write "not verified" and say what verification would
take. Never present an unverified claim as verified — that is deception even
when unintentional (`ava-serious-research` `principles/honesty`).

## Confidence tags (per learning, in the state)

- `consensus` — ≥ 2 independent sources agree. Independence means different
  publishers/authors, not two pages quoting each other.
- `single` — exactly one source found. Usable in the report, tagged as such.
- `contradicted` — sources disagree. Do not resolve by majority vote; report
  the disagreement and what each side rests on.
- `unverified` — the fact matters but no reachable source supports it. Keep
  it in the state, flag it in the report's uncertainty section. Dropping it
  silently is cherry-picking.

## Primary-source discipline

- Classify each source primary / secondary / tertiary *relative to the
  question* (the same paper is primary for "what did the study find" and
  tertiary for "how is the field seen by outsiders").
- For every claim that will support the report, prefer the primary source.
  If only a secondary summary is reachable, say so in the claim's phrasing
  ("according to X's summary of the study") or in the source's note.
- Check the source's existence and status before citing: date, venue, and —
  for papers — retraction / expression-of-concern notices.
- A source whose URL is dead or whose content changed after `accessed_at` is
  stale: mark it and re-fetch if the claim depends on it.

## Established fact vs inference vs speculation

The report must keep these three apart in language:

- **Fact**: the source states it and you verified the source. "The paper
  reports 51.3 per 10,000 papers (Lancet audit, 2026)."
- **Inference**: you reasoned from facts. Mark it. "If the trend holds,
  ..." — and state the inference's premises.
- **Speculation**: no evidence either way. Either drop it or put it in the
  uncertainty section explicitly labeled as speculation.

Mixing them is the single most common way a research report overclaims — the
Phase 0 evidence bar decides how much each is allowed to matter.

## Citation hygiene checklist (before running the audit)

- [ ] Every headline claim has an inline citation.
- [ ] Every cited source is in the state with full metadata + `accessed_at`.
- [ ] Every source was actually opened (snippet trust is not citation).
- [ ] Primary sources preferred; secondary-sourced claims say so.
- [ ] Contradictions kept, not resolved by vote.
- [ ] Verification level labeled per claim; unverified claims say so.
- [ ] No causal language without causal evidence (Phase 0 classification).
