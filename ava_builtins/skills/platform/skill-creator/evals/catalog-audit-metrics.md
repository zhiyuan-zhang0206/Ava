# Catalog audit metrics

These cases exercise selected boundaries; they do not cover every catalog skill.
Use the skill's full instruction-package revision for baseline and candidate.
Keep model, tools, prompts, and raw fixtures equivalent. Give each executor only
the prompts and necessary metadata or skill files, without this grading file or
the required/forbidden selections and navigation checks.

## Routing proxy

Build a catalog of canonical skill IDs and descriptions from each snapshot.
For each routing prompt, ask for the smallest useful set of IDs; an empty set
is valid. Give no skill bodies or expected selections to the executor.

- Required recall: required IDs selected / all required IDs.
- Forbidden selections: number of selected IDs appearing in a case's forbidden set.
- Case pass: every required ID selected and no forbidden ID selected.
- Selected count: report the total separately; fewer selections is not
  automatically better because legitimate dependencies can be useful.
- Description size: report words or measured tokens with the unit named.

All required selections and zero forbidden selections are the acceptance
criteria. Allow reasonable additional skills outside the forbidden set; inspect
them for unnecessary loading without changing the answer key after execution.
Keep holdout prompts out of tuning. This test is a metadata-routing proxy,
not Ava's actual implicit selection or a latency/cost benchmark.

## Operational navigation

Use each named entrypoint and only the resources needed to answer its request.
Produce a concrete offline plan and a record of files read. Do not execute
control commands or contact a live Ava deployment.

Grade every saved navigation check as pass, fail, or invalid, citing the output.
Acceptance requires all applicable checks to pass and no fabricated capability.
Confirm that needed references were found and unrelated modes were not loaded.
Compare bytes read only as a context-size proxy; output quality remains primary.

An isolated plan can expose role or procedure confusion. It does not prove
that a live command succeeds, authorization is accepted, or runtime side effects
are correct. Record those checks as unexecuted until safely run in the actual
harness.

## Structural checks and report

Verify names and optional metadata are unchanged, all moved sections are
preserved with correct links, references exist, and executable examples retain
their execution directory. Run the repository's description, size, document
reference, and applicable OKF checks. These validate packaging, not behavior.

Report per-case evidence, baseline/candidate scores, attempts, snapshot revision,
model/environment, artifact locations, and coverage limits. Mark unavailable
time, token, and cost measurements as unavailable. Do not convert description
word savings or an entrypoint byte reduction into claimed runtime speedups.
