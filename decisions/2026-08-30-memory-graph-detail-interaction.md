# Memory graph details: hover structure-highlight + click-to-render side panel

## Context

The memory graph page (force graph of the OKF concept notes) carried three
user complaints (2026-08-30): the hover detail card followed the cursor; the
right-side tag-summary / selected-node-metadata detail was unnecessary; and
clicking a node did not show the note itself. A 9+1-product competitive survey
(PM #3187, `kg-viz-interaction-research`) reported industry consensus on all
three.

## Decision

- **Hover = structure highlight only.** No content tooltip at all: the
  hovered node and its one-hop neighbors stay lit, everything else dims to
  50% (Cosma's pattern). Structure is the point of a graph view; content
  belongs to clicks.
- **Click = render the note body in a persistent side panel.** Clicking a
  note node fetches `GET /api/memory/note` and renders the markdown body
  (frontmatter stripped server-side, parsed by the same
  `shared/parse_frontmatter_typed` used everywhere); clicking a folder
  pseudo-node lists its contained notes. The old right-side tag
  summary / metadata detail view is removed — it duplicated what the note
  itself carries.
- **Side bar is a resizable split** (`react-resizable-panels`, horizontal on
  desktop, vertical/stacked on mobile, ratio persisted per device) so the
  note panel can be widened for long notes without leaving the graph.

## Alternatives rejected

- **Fixed-position hover card instead of removal** (the literal first
  request): every surveyed product that used hover for content was
  effectively re-implementing a click affordance; a fixed card still shows
  content before the user asks. Removed entirely, per Cosma/Obsidian/Roam.
- **Keep the tag summary in the side panel:** tags are already on every node
  (graph colors) and in the note's frontmatter header; a separate panel
  section was duplicate information the user explicitly ruled out.
- **Embed the note body in the graph response:** bloats the graph payload
  with all note bodies for a detail shown one-at-a-time; the per-note
  endpoint also gives a fresh read on every click.

## Consequences

- The graph is now navigation + structure only; the note content lives in
  the right panel. URL deep-linking to a selected note (Cosma's #hash
  pattern) is a future nicety, not implemented.
- `GET /api/memory/note` adds one read-only endpoint; it resolves strictly
  inside the memory root (traversal / null-byte paths are 404).
- Node labels now dim together with their node (group opacity) — a dimmed
  node must not read as highlighted via its label.
