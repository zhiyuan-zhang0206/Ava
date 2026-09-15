# Visual-defect judgment standards (attach to visual experiments / review briefs)

A visual-experiment or review brief must carry these standards, so that
"design choice" calls cannot drift systematically lenient (2026-08-29 vision
comparison-experiment lesson).

## Severity floors (as fixed in the checklist)
- casing (letter case / sentence case): floor = **warn** — new copy that is not
  sentence case is warn, no "design choice" downgrade; existing strings may hang
  at "worth a human glance" under the diff-only rule.
- alignment / scrollbar / empty-block / duplicate-control / overlay-occlusion:
  floor = **warn**.
- off-canvas / overflow-clip: floor = **block**.
- contrast/theme, redundant-tooltip, empty-state: warn / nit / warn.

## Diff-only rule
- A post-deploy re-check reports only the changed surface; pre-existing items
  the change did not touch hang at "glance" and are not upgraded.

## Negative-sample discipline
- When judging something a non-defect (negative sample), state the reason (e.g.
  ellipsis + title carrying the full value = the information is reachable).

## New classes
- A defect shape the checklist does not cover is reported as a candidate class
  (with evidence) for QA to graduate into the checklist — never invent a class
  inline in a report.
