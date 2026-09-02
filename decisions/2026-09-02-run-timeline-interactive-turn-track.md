# Run timeline uses an interactive linear turn track

## Context

The run timeline needs to explain when each turn happened and let an operator
inspect the measurements already attached to that turn. The existing UI split
time and token quantities into separate SVG panels and requested one-hour
buckets before the default session's turn count was known. That made the
default view non-interactive at turn level. It also placed glyphs in scalable
SVG geometry; reciprocal text scaling had already produced visibly blurred
labels in two prototype reviews.

The API carries completed turn intervals, LLM totals, executions, anomalies,
and event markers. It does not carry raw model or tool input/output text, and it
does not describe multiple agent lanes.

## Decision

The default run timeline requests turn rows first. Explicit windows of six
hours or more still request one-hour buckets up front, and a turn response above
400 rows still switches to buckets before rendering.

The chart uses one linear wall-clock axis with a single row of alternating turn
blocks. Failed turns use the failure series color. Prioritized event chips sit
on collision-managed lanes, connected to their projected timestamps. Selecting
a real turn opens an inline detail panel containing only fields present on that
row: time range, active seconds, token input/output totals, cost, model, latency,
execution tool/duration/status, and anomalies.

Geometry and text have separate rendering boundaries. SVG owns only lines,
curves, nodes, and blocks at actual CSS-pixel dimensions. Every glyph is a
fixed HTML sibling positioned with rounded coordinates from the same pure
layout projection. Connector paths start and end at the exact coordinates of
their explicit source and destination nodes. No transform or reciprocal scale
is used for text.

## Alternatives rejected

- **Keep the time/token waterfall:** it duplicates row correspondence across
  two panels and makes turn selection secondary. Token and cost values remain
  available in the detail panel without implying that token quantity shares a
  time scale.
- **Keep the default bucket-first request:** it bounds rendering before the
  actual run size is known, but removes the individual turns required by the
  primary interaction. The six-hour and 400-turn guards retain the safety
  boundary where it is needed.
- **Render SVG text with inverse `scaleX` compensation:** glyph dimensions can
  be numerically counter-scaled, but rasterization still becomes blurry and the
  prototype failed visual acceptance twice. Text therefore never enters the
  transformable geometry tree.
- **Add raw I/O or multi-agent lanes:** neither concept exists in the response
  contract. Adding them would turn a frontend redesign into backend/schema
  work.

## Consequences

- Typical sessions open directly at turn detail; long explicit windows and
  runs above 400 turns remain aggregated and do not masquerade as turn detail.
- Tokens no longer have a separate overview panel in this MVP. Their exact
  totals, together with cost and model, are available after selecting a turn.
- Layout projection and connector anchoring are deterministic pure functions,
  independently testable from React and browser sizing.
- The route, API, TanStack Query key shape, session selector, and window
  override contract remain unchanged.

## Supersedes

- The UI panel and default-request choices recorded in
  [`docs/history/2026-08-29/run-level-timeline.md`](../docs/history/2026-08-29/run-level-timeline.md).
