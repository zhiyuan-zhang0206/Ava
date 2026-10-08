# Preset Maker authoring evaluation

Read [the shared contract](../../references/evaluation.md)
before running these cases. `evals.json` contains constructed authoring tasks
with inventories and input data supplied inline. Use a disposable scratch
directory. Do not call a live gateway, install packages, authenticate MCPs, or
spawn production agents.

## Setup and baseline

Compare the previous Preset Maker instruction package with the candidate using
the same executor model, effort, tools, and fixture conditions. Supply only the
case prompt and skill instructions, not expected outputs, assertions, or prior
results. Keep grader-owned case files inaccessible and record any isolation gap.

Cases 1–3 exercise unsupported model input, complete config replacement, and
missing MCP prerequisites. Reserve case 4 for a final scope check. The current
request's inventory is authoritative for its test; model names in these cases
do not assert availability on a running deployment.

## Measures and acceptance

- **Case pass rate:** a case passes only when every assertion in its record
  passes. Report passing / valid completed cases and coverage across all four.
  All four cases must pass for acceptance on this set; missing runs leave the
  evaluation incomplete.
- **Configuration integrity:** inspect saved proposals for model membership,
  retained config values, and absence of server definitions, secrets, or invented
  evaluation/prompt/status fields. Check unresolved role registration explicitly.
- **Evaluation design:** inspect the proposed cases, metric definitions,
  acceptance criteria, and baseline. The model comparison in cases 1–2 must
  assess worker task quality alongside efficiency; the maker's own authoring
  speed does not stand in for worker speed.
- **Scope and evidence integrity:** any live mutation outside the fixture scope,
  invented model, silent substitution for an unresolved choice, or unsupported
  readiness claim fails the applicable case. A draft caused by the supplied
  missing runtime or authentication is the expected outcome.
- **Authoring efficiency:** report elapsed time and input/output tokens when
  measured; report cost only with a known basis. Keep these diagnostics separate
  from configuration and task-quality checks.

Preset Maker is explicitly loaded in these runs; no implicit trigger metric is
derived from them. The output presets' task-quality and speed metrics are
evaluation deliverables, not measured runtime results of this authoring set.

## Results

Store per-check evidence, proposal artifacts, and candidate/baseline comparisons
outside the package. No behavioral results are bundled here. Report failed,
invalid, blocked, and unexecuted attempts. Offline authoring success does not
establish live CRUD, named-preset spawn resolution, MCP readiness, or the saved
preset's end-to-end task quality.
