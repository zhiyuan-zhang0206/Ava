# eval case schema v1

The shared, cross-line eval-case contract. One JSONL row per case, five
top-level fields: `id`, `input`, `expected`, `grader`, `meta`.

Owner: CTO top design item 2 (item 2, "Evaluation / evalset base"). Ava holds the reference
implementation (this directory); Monsora adopts the format for its product
evalsets; the self-evolution line assesses compatibility. Case content is
per-line and never mixes inside one evalset file.

## File layout

```
evals/cases/<domain>/<evalset>.jsonl    # one evalset per file
```

`<domain>` is the line's own vocabulary (Ava: `adversarial`, `regression`,
...; Monsora: its product areas). A directory README describes the domain
and how to run it; a formal evalset manifest is deliberately deferred until a
concrete need appears.

## Row fields

```json
{
  "id": "ava-adversarial-c001",
  "input": {"scenario_constructor": "schedules.adversarial_eval_cases:write_scenario",
            "case_id": "c001"},
  "expected": {"facts": {"canary_source": "batch-runtime",
                          "check_target": "key-verify.txt",
                          "requirements": ["REQ-01"]},
                "rubric": "optional text for llm-judge graders"},
  "grader": {"type": "artifact-audit",
             "impl": "schedules.adversarial_eval_cases:audit_case",
             "grader_version": "1"},
  "meta": {"schema_version": "1",
           "line": "ava",
           "family": "document-authority",
           "difficulty": 1,
           "tags": ["authority"],
           "created_at": "2026-08-31T07:00:00+08:00",
           "migrated_from": "c001"}
}
```

### id (string, required)

Globally unique, stable, never reused after publication. Format:
`<line>-<domain>-<seq>` where `seq` is a **fixed-width three-digit** number
(`c001`, not `c1`) — the fixed width avoids `c1`/`c01` ambiguity across
lines. `line` is the same enum the billing-event schema uses (`ava`,
`monsora`, `speechful`, `research`).

### input (object, required)

What the subject under test receives. No large content inline: resources are
referenced by relative path. **Resource landing follows the user asset L2
standard** — large assets live in object storage and the evalset git file
stores references only, closing the loop with top-design item 5 (cross-line
aggregation through unified metadata).

### expected (object, required)

The gradeable expectation, expressed as **structured facts** — the grader's
input, not a string to compare against. `facts` (object) is required and
machine-readable; `rubric` (string) is optional, attached only when the
grader is an llm-judge.

### grader (object, required)

The scoring contract, pointing at repo code rather than inlining prompts or
model choices:

- `type` (required): `artifact-audit` | `llm-judge` | `exact` | `custom`.
- `impl` (required): the line's load point (e.g.
  `schedules.adversarial_eval_cases:audit_case`).
- `grader_version` (required): bump = scoring semantics changed; the bump
  travels in the same PR as the case change. Swapping a judge model does
  **not** bump the contract version — model choice stays inside the repo
  implementation.

### meta (object, required)

- `schema_version` (required): `"1"` for this shape.
- `line` (required): one of the shared line vocabulary
  (`ava` / `monsora` / `speechful` / `research` — the same values the
  billing-event schema will formalize) **and** equal to the first segment
  of `id`. The loader enforces membership in this vocabulary.
- `family` (required): the behavior-family vocabulary (aligns with the
  existing `CaseDefinition.family`).
- `created_at` (required): ISO-8601 with an explicit UTC offset — naive
  timestamps are rejected by the loader.
- Optional: `difficulty`, `tags`, `migrated_from` (source id when a dataset
  is migrated into this format).

## Versioning

Evalsets live in git and change through PRs. The schema evolves **add-only**:
fields are added, never renamed or removed. Additive changes do **not** bump
`schema_version` — the shape stays `"1"` so an older loader keeps parsing a
newer file (the same contract discipline as API deployment). A breaking shape
change would require a new major version plus a coordinated loader rollout,
and is not on the roadmap. `id` reuse is forbidden; a changed case is a new id
plus `meta.migrated_from`. An empty or comment-only evalset file is legal and
loads as zero cases — consumers must treat zero cases as an explicit
condition, never as a silent pass.

## Runner pattern

JSONL is the exchange/storage format only. Each line loads rows into its
native case structure and runs them on its existing driver — Ava maps rows
to `CaseDefinition`-compatible records and executes on the
`adversarial-eval-weekly` schedule machinery, with the isolation switches
from `AgentEvalSettings` (`AVA_EVAL_ISOLATION`, `AVA_EVAL_NETWORK_ALLOWLIST`,
container exec) as the shared isolation contract. Migration is format-only:
behavior and scoring semantics do not change with the move.

## Reference dataset

`evals/cases/adversarial/adversarial-weekly-v1.jsonl` is the migrated Ava
adversarial batch (cases c001–c009). It is the canonical shape example for
other lines to copy.
