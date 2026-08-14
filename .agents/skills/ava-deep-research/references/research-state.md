# Research State — the single source of truth

The research state file (`research_state.json` in the research working
directory) is the **only** place findings live during a deep-research run. The
report is a *derived view* of it; the audit script (`scripts/audit_research.py`)
is the *verifier* of that derivation — the R2 discipline (one fact, declared
once; collections derived; tests/verifiers, not hand-held copies).

Why a file and not the conversation: a deep-research run spans many turns,
waves, and possibly workers. Compaction and restart must lose nothing, and a
human must be able to open the state and check any claim. The state file is
that audit trail.

## Top-level shape

```json
{
  "meta": {
    "agent_id": 2962,
    "task_id": 1106,
    "created_at": "2026-08-09T12:00:00Z",
    "budget": {"breadth": 8, "depth": 3, "max_fetches": 200, "wall_clock_min": 120}
  },
  "question": {
    "three_part": "studying X to find out Y, so that the audience understands Z",
    "so_what": "if this goes unanswered, <who> loses <what>",
    "type": "practical | conceptual",
    "evidence_bar": "decision-grade | understanding | correlation-only | causal"
  },
  "plan": [
    {"id": "q1", "sub_question": "...", "queries": ["...", "..."],
     "source_types": ["primary", "secondary"], "status": "planned | active | covered | abandoned"}
  ],
  "sources": [
    {"id": 1, "url": "https://...", "title": "...", "publisher": "...",
     "date": "2026-05-01", "accessed_at": "2026-08-09T12:10:00Z",
     "kind": "primary | secondary | tertiary", "note": "optional context"}
  ],
  "learnings": [
    {"id": 1, "fact": "...", "source_ids": [1, 3],
     "confidence": "consensus | single | contradicted | unverified"}
  ],
  "claims": [
    {"claim": "headline claim that will appear in the report",
     "source_ids": [1, 3],
     "verification": "formal | process | rubric | self"}
  ],
  "waves": [
    {"wave": 1, "queries_run": ["..."], "fetches": 12,
     "gaps": ["q2: no primary source found"], "at": "2026-08-09T12:15:00Z"}
  ],
  "audit_log": [
    {"at": "2026-08-09T12:16:00Z", "action": "wave 1 collected; q1,q3 covered"}
  ]
}
```

## Field contracts

| Section | Field | Contract |
|---|---|---|
| `meta` | `budget` | pre-registered breadth/depth/fetch/wall-clock caps — set in Phase 1, never silently raised; `max_fetches` is a GLOBAL budget enforced by invariant 8, so parallel orchestrators must split it into per-worker caps that sum exactly to it |
| `question` | `three_part` | the Phase 0 sentence; `so_what` names the loser; `type` + `evidence_bar` govern what counts as evidence (causal claims need causal evidence) |
| `plan` | `id` | `q1..qN`; `status` tracks coverage across waves; abandoned sub-questions keep their row and say why |
| `sources` | `id` | **integer** — the inline citation number in the report refers to this id |
| `sources` | `url` | must be openable by a human; no `javascript:` / archive-only placeholders |
| `sources` | `accessed_at` | the proof the source was actually visited; a source never visited is not a source |
| `sources` | `kind` | primary / secondary / tertiary relative to THIS question — same URL can be primary for one question and tertiary for another |
| `learnings` | `fact` | one fact per row, stated neutrally; no interpretation smuggled in (interpretation goes in the report's analysis, attributed as such) |
| `learnings` | `source_ids` | non-empty; every id must exist in `sources`; `consensus` requires ≥ 2 distinct ids |
| `learnings` | `confidence` | `consensus` (≥ 2 independent sources agree) / `single` / `contradicted` (sources disagree — keep the disagreement in the report) / `unverified` (could not reach a source; the fact is flagged, not dropped) |
| `claims` | `verification` | the ladder from `ava-serious-research` `practices/present`: formal verifier (strongest) → process-level check → rubric review → self-assessment (weakest); the audit script counts as `process` |
| `waves` | `gaps` | the reflection output of each wave — what is still missing; the next wave must target these |

## Invariants (enforced by `scripts/audit_research.py`)

1. Required sections present: `question`, `plan`, `sources`, `learnings`.
2. Every `source_id` referenced by a learning or claim exists in `sources`.
3. Every source has `url`, `title`, and `accessed_at` (visited ⇒ real).
4. Every learning has at least one source; `consensus` has at least two.
5. Source URLs are unique (dedup is mandatory — the same page fetched twice is
   one source).
6. `confidence` and `verification` values are from their fixed sets.
7. Every inline citation `[n]` in the report resolves to a source id.
8. When `meta.budget.max_fetches` is set, `len(sources)` must not exceed it.
   Sources are URL-deduped and every source was fetched, so the unique-source
   count is a lower bound on fetch calls — exceeding it means the budget was
   blown even after dedup. Orchestrators distribute the cap across parallel
   workers (sum of per-worker caps = max_fetches) so merged states stay under
   it; the audit is the enforcement.

The audit is a *process-level* check: it proves citation-sourcing consistency,
not truth. Semantic verification (is the source reliable? does the claim
survive the contradictions section?) stays a human/rubric judgment — the
report's confidence tags and contradictions section carry it.

## Worker output files

Parallel workers write `learnings-<n>.json` and `sources-<n>.json` (each a
JSON array of the corresponding section's objects, source ids local to the
worker's file). The orchestrator merges them into the single state file and
re-numbers source ids — merging is the orchestrator's job, so a worker never
needs to know the global id space.

## Examples

A good learning row:

```json
{"id": 12, "fact": "DeepSeek V3 was released in December 2024 with 671B total parameters (37B active).",
 "source_ids": [34, 41], "confidence": "consensus"}
```

A bad learning row (interpretation smuggled in, no source):

```json
{"id": 13, "fact": "DeepSeek V3 is the best open model ever released.", "source_ids": [], "confidence": "consensus"}
```

The first states a fact with two sources and a tag the audit can check; the
second is an opinion with no source and a `consensus` tag the audit rejects.
