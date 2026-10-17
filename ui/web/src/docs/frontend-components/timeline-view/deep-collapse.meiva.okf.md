---
type: doc
title: Timeline deep collapse
description: Turn grouping and expansion state — secondary-item classification into TurnBlocks, Details-mode defaults, per-turn and per-item override maps, and the fork boundary rule.
tags:
- frontend
---

# Timeline deep collapse

`runs.classifyItem` classifies items as primary (agent replies + human inbound, always visible) / secondary (thinking / code / output, inter-agent messages, system inbound, compact, system_prompt, note marker) / bare (ephemeral marker). Adjacent secondary items form a `TurnBlock`; Details All/Last/None controls its default expansion, with Last opening the active final turn. In None mode an opened turn keeps its nested blocks collapsed (each opens on its
own click); All/Last cascade the expansion into them (task #4780). The
in-progress step stays visible while the turn is active (the last streaming
item is peeled out of turn-collapse). While the settings query is in flight the
view renders the collapsed state regardless of the reported mode — a stale
`all` read would flash every block open on each cold load, and the reveal when
the real value lands is the lesser evil. A turn's expanded state is a pinned map
keyed by the turn id (its first member's `item_id`, stable across streaming
commits): a click flips the currently rendered state and pins it, so the choice
survives defaults that flip on their own — most notably the active final turn
auto-collapsing when the agent goes idle (#510); absence from the map follows
the mode. Per-item overrides are the opposite-of-default set (membership means
flipped); a mode change clears both maps, and re-picking the same mode bumps a
reset token so manual expansions under that mode revert too (user ruling
2026-08-06). Pinned state is scoped per thread — item ids are message indexes
local to each thread, so an unscoped map would resurrect another conversation's
choices. Fork attaches only under the last agent reply, a clean halt boundary
rather than mid-execution; copy and fork actions appear only for expanded,
settled rows.

## Relationship to Other Nodes

- [[ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md|Timeline View]] — the renderer whose turn grouping and expansion this node owns.
