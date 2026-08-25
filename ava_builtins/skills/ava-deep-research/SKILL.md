---
name: ava-deep-research
description: Produces auditable, cited, multi-source research with Ava's own search and verification tools. Use when the user asks for deep research, a literature-backed report, competitive landscape, evidence synthesis, or any question too consequential for a quick answer.
---

# Ava Deep Research

## Mission

Research that deserves trust. The purpose chain is explicit: research exists to
understand reality, and understanding serves human flourishing —
**truth → understanding → benefit**. Legal outputs are three: (a) an answer,
(b) a sharper confusion — a better question than the one asked, (c) a reframed
question. "Looking like a report" is never the success criterion; any practice
optimizing the appearance of research over actual understanding (padding with
unverified claims, cherry-picking agreeing sources, hype) is failure. This skill
operationalizes `ava-serious-research` for web research — the epistemology
layer (falsifiability, claim–evidence alignment, honesty) lives there; here it
is applied to search, evidence, and reporting.

## What this skill is

Ava-native Deep Research: the agent itself plans, searches, iterates, verifies,
and delivers a cited report using `ava.web.search` / `ava.web.fetch`, the
parallel worker fleet (`ava-dynamic-workflow`), and `ava.ui` for delivery.
Everything that matters lives in **one research state file**; the report is a
derived view of it; a deterministic audit script verifies the derivation — the
R2 single-source-of-truth discipline, applied to research.

## When to use

- A question wants a long, multi-source, cited report: competitive landscape,
  due diligence, technology evaluation, policy question, "what changed in X
  since Y".
- A decision will be made from the answer (buy/build, strategy, health,
  investment) — decision-grade evidence is required.
- The user asks for deep research and wants the process auditable: every claim
  traces to a source a human can open.
- API budget for search/fetch is acceptable.

**When NOT to use:**

- Quick lookup → answer directly with `ava.web.search`, no skill.
- Zero API spend, or a frontier seat is preferable → `web-ai/deep-research`
  (drives Gemini/ChatGPT/Perplexity seats, fetches their finished report —
  their citations, no process control).
- An ML/experimental project → `ava-serious-research`.

The two compose: run an external seat for an extra perspective and cite its
report as a source like any other.

## The pipeline

**Frame → Plan → Collect → Reflect & iterate → Verify & audit → Synthesize &
deliver.** Phases 3–4 loop until the pre-registered stopping rule fires.

### Phase 0 — Frame (question discipline)

1. Write the three-part question: *"studying X to find out Y, so that the
   audience understands Z"* — `ava-serious-research` `practices/question`.
2. So-what test: if the answer were missing, who loses what? No loss → not
   worth deep research.
3. Classify: practical (decision-grade evidence) vs conceptual (understanding);
   prediction vs intervention — causal claims require causal evidence.
4. Name the audience and what they will decide with the report.
5. Record all of it in the state file (schema: `references/research-state.md`).

Why: the question makes "done" decidable — you stop when it is answered or the
budget is spent, never when the report looks long.

### Phase 1 — Plan (sub-questions + queries + budget)

1. Decompose into 3–6 independent sub-questions that partition the question.
2. Per sub-question: 2–3 search queries in different phrasings (synonyms,
   entity names, dates) and the expected source types (primary: official docs,
   papers, data; secondary: reputable reporting; tertiary: summaries).
3. Pre-register the budget: breadth (queries per wave), depth (max iteration
   waves), fetch cap, wall-clock cap. Defaults: breadth 6–10, depth 3,
   ~200 fetches — search and fetch are cheap, so the cap bounds worker drift,
   not API spend (user ruling 2026-08-09: "fetch/search 不值钱", raised from
   ~60 after the #1108 overrun). **When parallelizing with workers, the fetch
   cap is a GLOBAL budget: distribute it so the per-worker caps sum exactly to
   max_fetches** (cap_i = max_fetches // n, +1 to the first max_fetches % n
   workers) and put each worker's cap in its prompt. Without per-worker caps,
   N workers each counting independently blow the budget ~N× — the #1108
   failure mode (60 pre-registered, 100 actual). The audit enforces the global
   cap afterwards (unique sources ≤ max_fetches, a lower bound on fetch
   calls).
4. Pre-register the stopping rule — stop when (a) every sub-question has
   evidence, (b) repeated searches yield nothing new (saturation), or
   (c) the budget is spent; whichever comes first.
5. Show the plan before executing when the question is ambiguous or expensive
   (Gemini Deep Research's collaborative planning is the prior art). If the
   user said "just research it", proceed with reasonable defaults.

Why: pre-registration defends against researcher degrees of freedom — without
a written stopping rule, iteration runs until the report *looks* complete,
exactly the appearance-over-understanding failure mode.

### Phase 2 — Collect (parallel waves, breadth-first)

1. Wave 1 (breadth): run all planned queries with `ava.web.search` in parallel,
   fetch the promising pages with `ava.web.fetch`, extract learnings.
2. Write everything into the state file as you go: each learning = fact +
   source_ids + confidence; each source = url, title, publisher, date, kind,
   accessed_at. **Never hold findings in conversation context** — the state
   file is the memory; compaction and restart must lose nothing.
3. Parallelize by sub-question when the task is large: spawn ONE worker per
   sub-question (pattern: `ava-dynamic-workflow`). Each worker gets a
   self-contained prompt — the sub-question, its queries, the state schema,
   its share of the fetch budget (Phase 1), the handoff path — searches and
   fetches with its own context, writes `learnings-<n>.json` +
   `sources-<n>.json` into the handoff dir, and stops. The orchestrator arms
   ONE checkpoint watcher (`gather_files.py`) and idles.
4. For small tasks (≲ 6 queries total) do it yourself — spawning has real
   overhead (anti-pattern: over-spawning).

Worker prompt template (make it self-contained):

```
You are a Deep Research worker for sub-question: <sub-question>.
Full question: <three-part question>. Audience: <Z>. Evidence bar: <bar>.
Run these queries: <queries>. Prefer primary sources; open pages before
trusting snippets. One reformulation per query if the first returns nothing.
Your fetch cap: <cap> ava.web.fetch calls — COUNT them and stop at the cap;
sources beyond it go unread, not uncited.
Never fabricate a learning — if the sub-question is unanswerable, write
learnings: [] plus a "note" explaining what is missing.
When done: write {handoff}/learnings-{n}.json and {handoff}/sources-{n}.json
conforming to the schema in references/research-state.md (source ids are
integers; every learning's source_ids must exist in your sources file).
Do NOT message anyone — writing the files IS the handoff. Then terminate.
```

Why parallel: independent sub-questions share no state, so wall-clock scales
~1/N and each worker's context stays clean.

### Phase 3 — Reflect & iterate (depth-first)

When a wave's results land:

1. **Gap analysis**: mark each sub-question covered / partial / empty; for
   partial or empty, derive follow-up queries from what was learned (the
   dzhng/deep-research reflection pattern: learnings → follow-up directions).
2. **Contradiction hunt**: find claims that disagree across sources; search
   specifically for the disagreement — who challenges the consensus, what does
   the primary source say?
3. **Depth-first**: follow citations backward (foundations) and forward (live
   debate) from 2–3 anchor sources — `ava-serious-research`
   `practices/literature`.
4. Re-run Collect with the follow-up queries (wave 2, 3…); each wave is
   cheaper than the last as coverage grows.
5. After every wave, evaluate the stopping rule. Never start a wave "just to
   be sure" without a concrete gap it targets.

Why: breadth-then-depth mirrors how a human researcher works — map the
territory first, then dig where the map is blank — and the reflection step is
what turns search into research.

### Phase 4 — Verify & audit (citation discipline)

1. **Primary-source rule**: every claim that will support the report traces to
   a primary source; if only a secondary summary is reachable, mark the claim
   accordingly. Open the source before citing it; check existence, date, and
   retraction/status — `ava-serious-research` `practices/literature`.
2. **Confidence tags** on every learning: `consensus` (≥ 2 independent sources
   agree) / `single` / `contradicted` / `unverified`.
3. **Separate established fact from inference from speculation** in the
   report's language — never dress inference as fact.
4. **Run the audit** until clean:

```bash
python3 .agents/skills/ava-deep-research/scripts/audit_research.py \
  --state research_state.json --report report.md
```

It fails on: a citation to a source not in the state, a source without
metadata or `accessed_at`, a learning with no source, duplicated URLs, an
invalid confidence or verification level, a "consensus" learning with fewer
than two sources, and (when `meta.budget.max_fetches` is set) more unique
sources than the fetch budget — the global-cap invariant that makes the
per-worker distribution auditable.

Why: the audit is the R2 "tests are verifiers" move — a deterministic check
(not self-assessment) enforces the derivation invariants. It catches
citation-sourcing holes, not semantic truth; say so in the report's limitations.

### Phase 5 — Synthesize & deliver

1. Write the report **from the state file**, never from memory.
2. Every significant claim carries an inline numbered citation `[n]` resolving
   to the source list (format: `references/citation-discipline.md`).
3. Deliver with `ava.ui.serve_markdown(report, name="<slug>")` — the
   user-facing channel (HTML-rendered page). **Offer format choice in the
   delivery message**: A. the served HTML page link (default), B. the Markdown
   source path, C. any other format the user names (user ruling 2026-08-09:
   reports must offer format choice; HTML serve is the default channel). Report
   + state file paths go in the final message; the state file is the audit
   trail a human can open.
4. **Rendering ≠ deciding**: present the process (question → plan → waves →
   decisions) and name the human checkpoint; never mark the research complete
   yourself — the user decides when it is done (`ava-serious-research`
   `practices/present`).

Report structure:

1. Executive summary — bottom line first, ≤ 5 bullets
2. Question & method — three-part question, plan, budget, waves run, sources
   consulted
3. Key findings by sub-question — each with citations
4. Evidence table — claim | evidence | confidence | sources
5. Contradictions & uncertainty — what disagrees, what could not be verified
6. Gaps & limitations — paywalls, recency, unanswerable sub-questions
7. Sources — numbered, full metadata

## Long-running behavior

Deep research takes minutes to hours. Follow the long-running-agent
discipline: while a wave or checkpoint is outstanding, end the turn idle
(`ava.self.pause_heartbeat` while waiting), wake only on watchers and
checkpoints. Never block in-turn on the whole research.

## Anti-patterns

- **Writing the report before verification** — the report is derived from the
  state; write it last.
- **Holding findings in conversation context** — compaction loses it; the state
  file is the single source of truth.
- **Snippet trust** — a search snippet is not a source; open the page.
- **Secondary-only citations when the primary is reachable** — else say so.
- **Padding** — a report that looks complete with unverified claims is the
  cardinal failure (Mission). List gaps instead.
- **Workers messaging the orchestrator** — files are the handoff (`ava-dynamic-workflow`).
- **Endless iteration** — every wave targets a concrete gap; the pre-registered budget stops it.
- **Over-spawning** — a sub-question answerable in a few fetches is done by the
  orchestrator itself.
- **Causal language on correlational evidence** — the Phase 0 classification
  governs.
- **Self-assessment sold as verification** — ladder: formal (strongest) →
  process-level → rubric review → self-assessment (weakest); the audit is
  process-level, label claims accordingly.

## Known limitations — state them in the report

- Search indices lag the live web; "as of today" claims need a dated source.
- Paywalled and login-gated content is invisible to `ava.web.fetch`; say so
  when it matters.
- The audit verifies citation-sourcing consistency, not truth — a source can
  be real and wrong. Confidence tags and the contradictions section carry that
  residual risk.

## References

- `references/research-state.md` — state schema + invariants (read before Phase 0)
- `references/citation-discipline.md` — citation format, ladder, tags (Phase 4)
- `references/prior-art.md` — what Gemini / Perplexity / OpenAI / Claude Code
  do, and what we borrow (read once)

## Related skills

- `ava-serious-research` — the epistemology layer (question, literature, verify, present)
- `ava-dynamic-workflow` — the parallel-collection engine; read it before spawning workers
- `web-ai/deep-research` — external seats; the zero-API-spend alternative
- `web-sources` — adapters for sources `ava.web` cannot reach
- `ava.web.search` / `ava.web.fetch` — the base tools this skill orchestrates
