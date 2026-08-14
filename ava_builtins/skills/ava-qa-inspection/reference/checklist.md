# QA inspection checklist

One row per defect class. Seeded from defects the user has had to report by
hand. `codified_as` names the deterministic check that now owns the class — when
it is filled, the sweep **skips** the class (a lint rule or Playwright assertion
already blocks it at PR). Fill it when a finding graduates (see SKILL.md).

Add a row when a new class of defect shows up; do not invent classes inline in a
report.

| id | what it is | how to detect | severity floor | evidence required | codified_as |
|---|---|---|---|---|---|
| casing | Title Case in copy the lint rule's scope missed — interpolated/templated text, `&`/`/`/`:`-joined caps ("Security & Secrets"), text rendered via a component prop the rule doesn't list, chart/canvas labels | read visible text in the screenshot; compare against sentence-case convention | warn | the exact string + where it renders | — (partial: `local/sentence-case` covers static JSX text + a fixed attribute list) |
| alignment | siblings that should share an edge don't — left/right ragged, a row item indented off its column | probe `edgeMisalignment`: same-parent same-tag siblings whose left (or right) edge deviates > 2px from the group mode | warn | the edge deltas from the probe | — |
| scrollbar | a scrollbar that shouldn't be there — horizontal on a column that should wrap, a nested box that clips | probe `overflow`: element `scrollWidth > clientWidth + 1` with `overflow-x` not intentionally scroll/auto, or page-level horizontal scroll | warn | overflow px + selector | — |
| empty-block | a box that renders with styling (border/background) but no content — a section that should be populated or hidden | probe `emptyBox`: element with border/background, no text and no laid-out children, non-zero area | warn | rect + computed border/background | — |
| duplicate-control | two controls doing the same thing — redundant buttons, a duplicated action | probe `duplicateControl`: 2+ visible buttons/links sharing an accessible name; confirm they aren't intentionally repeated (per-row actions) | warn | the accessible name + count + selectors | — |
| redundant-tooltip | a tooltip whose text just repeats the visible label — adds nothing | hover to reveal; compare tooltip text to the trigger's visible text/aria-label | nit | tooltip text vs label text | — |
| off-canvas | a visible element partly outside the viewport — clipped at an edge, horizontal bleed on mobile | probe `offCanvas`: visible element with `rect.right > innerWidth` or `rect.left < 0` | block | rect vs viewport | — |
| overflow-clip | text or a control clipped by its container (`overflow:hidden` eating content) — truncation with no ellipsis, a cut-off label | probe `overflow` with clipped + no `text-overflow:ellipsis`; confirm in screenshot | block | selector + clipped px | — |
| contrast/theme | copy invisible in one theme — same-color-on-same-color after a dark/light switch | screenshot both themes; look for vanished text | warn | the element + theme | — |
| empty-state | an empty state that shows a raw/blank panel instead of guidance — no "nothing here yet" affordance | navigate to the no-data route; check for an intentional empty state | warn | the route + screenshot | — |

## Probe ↔ class map

`reference/probes.js` emits one array per check; map to classes:

- `overflow` → scrollbar, overflow-clip
- `emptyBox` → empty-block
- `edgeMisalignment` → alignment
- `duplicateControl` → duplicate-control
- `offCanvas` → off-canvas

`casing`, `redundant-tooltip`, `contrast/theme`, `empty-state` have no probe —
they are the perceptual pass (screenshot + judgment) until they graduate.
