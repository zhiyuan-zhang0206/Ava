<!-- Template for a postmortems/ entry. Copy to NNNN-<kebab-title>.md (next free
number, zero-padded to four digits), fill the sections, delete this comment.

postmortems/ answers ONE question no other axis owns: **why did a failure
escape** — what broke, why every safety net missed it, and what guardrail now
prevents the class. It is not a bug log. The entry bar is all three at once:

  - SUBTLE     — the mechanism had to be re-derived the hard way; reading the
                 diff does not explain it.
  - SYSTEMIC   — it escaped through a gap in the tests, the tooling, or the
                 conventions, not through one person's slip.
  - COSTLY     — rediscovering it costs hours, or a production incident.

A routine bug that a test caught stays in git history. If you are unsure, it
does not qualify.

A postmortem is a FROZEN NARRATIVE. It is the one axis where war-story
chronology belongs, and it is never rewritten to match today's code — the same
rule decisions/ follows. Current-state facts live on the other axes: what the
system is now goes in the OKF node, the rule you extracted goes in
conventions/defensive-patterns.md, the procedure goes in a skill. Link out to
those; do not restate them here.

Because the narrative is frozen, `scripts/check_doc_references.py` skips this
directory: naming the flag, file, or command that existed AT INCIDENT TIME is
the record working as intended. That also means nothing checks your links, so
mark anything a reader cannot open. Commits and PR numbers from before the
2026-08-18 public-repo cutover are not reachable from public `main`; label them
`(pre-cutover)` so a reader does not hunt for them.

The pipeline: a postmortem produces guardrails; the guardrails that generalize
condense into one rule in conventions/defensive-patterns.md. A postmortem
whose lesson generalizes and does NOT appear there is only half filed. -->

# NNNN — <Title: the rule, stated as a claim>

**Date:** <YYYY-MM-DD of the incident>
**Anchors:** <commits, PR numbers, files — mark unreachable ones `(pre-cutover)`;
write `(summarized)` where the record could not be recovered from the repo>

## Summary

<!-- Thirty seconds. What broke, the one-sentence mechanism, and the guardrail
that now exists. A reader who stops here must still come away with the rule. -->

## Timeline

<!-- What happened, in order. Enough for a reader to place themselves inside the
failure — the state that looked fine, the moment it did not, what was tried. -->

## Root cause

<!-- The mechanism, not the symptom. Name the specific code path, seam, or
assumption. Then the escape analysis, which is the part that earns the entry:
for each safety net that SHOULD have caught this — test, lint, review,
convention — say why it did not. -->

## Guardrails added

<!-- What now prevents the class, each pointing at the thing that enforces it:
a test, a lint, a code path, a convention. State plainly which parts remain
unguarded and rely on the rule alone — an overstated guardrail is worse than an
absent one. -->

## Lessons

<!-- The generalizable rules, one per bullet, written so they apply outside this
incident. Whatever belongs to every reader (not just this subsystem) is then
condensed into conventions/defensive-patterns.md — link the entry from here. -->
