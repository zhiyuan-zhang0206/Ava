---
type: doc
title: Frontend Typography
description: Font-family boundaries, named text-size tokens, and the remaining audited typography deviations in the Ava web UI.
tags:
- frontend
- design-system
---

# Frontend Typography

## Family boundary

Application chrome uses Inter through `font-sans`: headings, buttons, labels,
descriptions, navigation, empty states, and error actions. Technical content
uses Geist Mono through `font-mono`: source code, command output, identifiers,
URLs, timestamps, configuration values, and numeric measurements.

Apply `font-mono` to the smallest leaf that contains technical content. A mixed
row stays sans and scopes mono to its ID, time, or measurement. Whole containers
use mono only when every child is technical content, such as a terminal output
pane. The page composer is a deliberate content-input exception and remains
mono; the Notice reply box belongs to the surrounding Notice UI and is sans.

## Named size scale

The Tailwind theme in `app/globals.css` pins the supported UI scale:

| Class | Token | Size | Use |
|---|---|---:|---|
| `text-2xs` | `--text-2xs` | 10px | Compact badges and secondary metadata |
| `text-xs` | `--text-xs` | 12px | Small labels, controls, and compact body copy |
| `text-sm` | `--text-sm` | 14px | Default UI copy |
| `text-base` | `--text-base` | 16px | Composer and emphasized body copy |

Use these names instead of absolute pixel utilities. The relative
`text-[0.85em]` on inline markdown code is intentional: it scales with the
surrounding prose and is not part of the fixed UI scale.

## Current adoption

HeaderBar, Fleet chrome, agent rows, Inspector chrome, ContentToggle, shared
small buttons, Notice controls, timeline headers, and common labels use the
family boundary and named scale. Numeric badges, agent IDs, timestamps, URLs,
metrics, terminal content, and code payloads retain explicit mono styling.

The task #2560 source audit eliminated `text-[10px]`, `text-[9px]`,
`text-[12px]`, `text-[13px]`, `text-[7px]`, `text-[6px]`, and
`text-[0.8rem]` from production TS/TSX. The exact 10px and 12px substitutions
preserve size; the other fixed values moved to the nearest supported token.

## Remaining deviations

These technical or geometry-sensitive surfaces remain deliberately scoped out
of the first migration. Re-audit their wrapping and graph geometry before
moving them to the nearest named token.

| Deviation | Count | Locations | Reason for deferral |
|---|---:|---|---|
| `text-[11px]` | 11 | `timeline/item.tsx`; `control/presets/page.tsx`; `control/schedules/page.tsx`; `shell/[agentId]/[sessionId]/page.tsx`; `memory/graph/page.tsx` | Dense message, JSON, log, shell, and graph metadata |

Run the inventory from `ui/web/` with:

```bash
rg -n --glob '*.{ts,tsx}' --glob '!*.test.*' 'text-\[' src
rg -n --glob '*.{ts,tsx}' --glob '!*.test.*' 'font-(sans|mono)' src
```
