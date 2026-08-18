# Communicating with the user

Rules for agent-to-user communication. Read when reporting to the user or
writing agent-facing output.

## No dev time estimates

Don't give time estimates — "~1 hour", "~30 minutes", "estimate half an hour".
Only list scope + trade-offs, leaving the timing decision to the user.
Made-up time numbers only interfere with scope judgment.

## Describe current, not historical

When describing current code behavior, describe only the current state. Don't
surface deprecated APIs as "used to be X, then changed to Y" historical
baggage. When historical context is truly needed to explain a decision, a
brief one-liner; the detailed rationale lives in git history.

## Clean residuals

When you find a residual mention of an old API in code or docs, clean it up
in passing — don't quote it as context.

A decision record lives in git history: don't rewrite a
real past entry to scrub an old API name. Supersede it with a new entry if the
decision actually changed. Only clean residuals in `conventions/` and code.

(A same-day typo or misread in a fresh entry that never recorded a real
decision is fine to fix or delete; git backstops.)

## Candidate next steps

When reporting after finishing work, list 2–3 candidate next steps — work
options only. Each with a one-line reason and which you'd pick first. Avoid the
"deliberately not doing" items listed in [`non-goals.md`](non-goals.md).

Don't include "stop here for today" / "take a break" / "call it a day"
wrap-up suggestions.
