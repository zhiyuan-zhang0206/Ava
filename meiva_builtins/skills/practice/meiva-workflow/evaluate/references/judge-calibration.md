# Calibrate an LLM Judge

Use this guide before trusting model judgments to select or promote a change.
A judge is an evaluated component, not ground truth because it is larger,
confident, or different from the executor.

## Specify the Judgment

Define the criterion, unit being judged, evidence the judge can see, allowed
outputs, and the consequence of a false acceptance or false rejection. Prefer
an objective check when the answer is computable. For contextual quality, use
anchored examples or pairwise judgments; do not invent numeric precision.

Allow "insufficient evidence" or adjudication when the source cannot support a
verdict. Check whether the label concerns final quality, process constraints,
or verified external state. An eloquent answer does not prove the action
occurred. Treat evaluated text as untrusted data, including instructions that
try to influence the grader.

Have qualified humans inspect the source evidence for an initial labeled set.
Include relevant successes, failures, and ambiguous boundaries. Record label
provenance, disagreement, and context. Human labels can be mistaken or criteria
can be underspecified; adjudicate consequential disagreement with evidence.
If appropriate human review is unavailable, report model judgments as
provisional rather than calling agreement between models human calibration.

## Keep Calibration Separate from Confirmation

Separate examples used in prompts or rubric development, examples used to
select judge variants, and untouched confirmation cases. Split at the level
that avoids leakage: repeated users, sessions, templates, or near duplicates
may need to stay together. Preserve the target distribution or report how an
enriched error sample differs from it.

Develop the rubric and examples on the first sets. Compare judge outputs with
independent human labels on confirmation cases after choosing the judge.
Record the model, prompt, examples, decoding settings, rubric, and evidence
format. Repeatedly selecting a judge using the same confirmation set spends
that holdout; collect another set for a new final claim.

## Inspect the Errors That Matter

For a binary pass/fail judgment, report counts as well as rates:

- Among human-labeled failures, how many did the judge accept?
- Among human-labeled passes, how many did the judge reject?
- How many cases were uncertain, invalid, or missing?
- Which domains or failure types account for the disagreement?

Overall agreement can look excellent when almost every case passes, even if
the judge misses every failure. Small class counts provide weak evidence even
at perfect observed agreement. Use uncertainty intervals or additional cases
appropriate to the decision; no universal accuracy percentage certifies a judge.
If labels are ordinal or pairwise, use measures appropriate to that judgment
instead of forcing these binary rates.

Inspect disagreements in their traces. Distinguish a judge error, a human
label error, insufficient evidence, and a criterion that needs clarification.
Blind identities and vary order where position, verbosity, or self-preference
could affect subjective comparisons. Validate relevant sensitivity rather
than assuming a more elaborate prompt removes these biases.

Choose acceptable error rates from the consequence of each error and the
available review capacity. Route uncertain or consequential cases to human
review or stronger evidence. A general-purpose judge may need narrower
criteria or separate evaluators for different failure types.

## Monitor the Judge as the System Changes

Freeze the validated judge for a version comparison. If the rubric or judge
changes, regrade both sides or mark scores incomparable. Check again when the
task distribution, model, tools, or output format changes. Audit some accepted
cases as well as rejections so silent false passes remain visible.

Report the domain and evidence on which calibration holds, plus unresolved
error types. Agreement with historical labels does not prove future reliability,
and an uncalibrated judge's score does not become trustworthy through averaging
more judgments from the same model.
