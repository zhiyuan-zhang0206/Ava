---
name: ava-qa-inspection
description: Inspects rendered Ava frontend pages for visual and structural defects that source tests miss. Use when asked for UI QA, visual regression checks, stray scrollbars, misalignment, empty blocks, duplicate controls, or off-canvas bleed; inspection remains read-only.
---

# qa_inspection

The frontend has two blind spots no existing check covers. Static lint
(`local/sentence-case`, the CJK rule) reads **source strings**, so it misses
anything that only exists once the page is *rendered*: a block that lays out
misaligned, a container that shows a scrollbar it shouldn't, an empty box, two
buttons that ended up doing the same thing. Vitest runs in jsdom, where
`getBoundingClientRect` is always zero — it can assert a button *exists*, never
that it is *placed right*. This skill inspects the real rendered page to catch
that class, and turns each confirmed defect into a deterministic check so it is
never found by eye twice.

## Safety — read-only, no exceptions

You are auditing production. **Navigate and observe only.**

- Allowed: navigate to a URL, take a screenshot, take an accessibility
  snapshot, evaluate a read-only script (the probe below), hover to reveal a
  tooltip, scroll.
- **Forbidden**: clicking or submitting anything that mutates state —
  spawn / restart / update / stop / delete / destroy, form submits, config
  writes, sending a message. If a check needs a menu open, open it by hover or
  by reading the DOM, not by triggering an action. When unsure whether a
  control mutates, do not click it.

## Browser hygiene — leave the shared Chrome as you found it

The shared Chrome is the user's desktop browser, and it does not clean up
after us: a session that rides `ava.mcps.chrome` and never closes its tab
leaves that tab behind forever (2026-08-30 — review-worker dev-server pages
accumulated until the user reported the tab pile-up). Two rules, both about
the page the bridge owns for this agent:

- **One tab per session.** The bridge gives each agent one page
  (`_AGENT_AFFINITY`), and a `navigate_page` with no page yet **bootstraps a
  brand-new tab**. Capture your page id on the first call (`new_page`, or the
  `list_pages` listing after the first navigate) and reuse that single tab for
  every route in this sweep; do not let each run carve its own.
- **Close it when done.** End the sweep with
  `ava.mcps.chrome.close_page(pageId=<your page id>)` — the bridge then drops
  that agent's affinity. Never leave a tab on a `localhost` / `127.0.0.1` URL:
  the dev server or stub it pointed at dies with the session and the tab only
  rots into a dead page. If the page id was lost, `list_pages`, find any
  `localhost` / `127.0.0.1` entry this run created, and close exactly that one.

## What to inspect

Work from `reference/checklist.md` — one row per defect class, each with how to
detect it, its severity, and the evidence a finding must carry. The list is the
contract; extend it (see "Graduating a finding") rather than improvising new
classes ad hoc.

Cover every top-level route and the states that are easy to miss: empty states
(no agents / no notices), long-content states (overflow), the mobile viewport
(≤ 430px wide — the sidebar/hamburger and picker live only there), and both
color themes. A defect that only appears in one state is exactly the kind that
reaches the user.

## Procedure per page

1. **Navigate** to the route with `ava.mcps.chrome` (the shared logged-in
   Chrome). Set the viewport for the state you're testing.
2. **Run the deterministic pass first.** Inject `reference/probes.js` via the
   evaluate tool. It returns structured signals — horizontal overflow, zero-box
   blocks, sibling-edge misalignment, duplicate accessible-names, off-canvas
   elements — each with a selector and the measured numbers. These are your
   *evidence*, not your judgment: a probe hit is a candidate, you still confirm
   it is a real defect (some overflow is intentional).
3. **Screenshot and read** what the probe cannot judge — the perceptual long
   tail: casing the lint rule's attribute list didn't cover, a tooltip that
   just repeats its label, cramped or unbalanced spacing, an obviously wrong
   alignment the edge-math didn't flag. Judge these from the screenshot.
4. **Record** each finding in the shape below. Attach the probe numbers or the
   screenshot region as evidence — never report a feeling without a measurement
   or a pixel.

## Every finding carries

```
- class:      one of the checklist ids (casing | alignment | scrollbar |
              empty-block | duplicate-control | redundant-tooltip | off-canvas | overflow-clip | …)
  page:       route + viewport + theme
  selector:   a selector that points at the element
  evidence:   the probe numbers (rects, overflow px, edge deltas) OR the
              screenshot region — concrete, reproducible
  severity:   block | warn | nit   (see below)
  suggested:  the fix in one line
  confidence: high | low
```

**Severity** — `block`: broken or wrong (text clipped, control off-canvas,
casing wrong, layout collapsed). `warn`: degraded but usable (a stray
scrollbar, mild misalignment). `nit`: preference (spacing you'd tighten). Only
`block` warrants opening a task; `warn`/`nit` go in the report body.

## Reporting — keep the signal high

- **Only report the diff.** Skip any class whose `codified_as` in the checklist
  is filled — a deterministic check already owns it, re-reporting is noise. When
  run after a rollout, prefer the changed pages and diff against the prior
  sweep; do not re-list standing known-and-accepted items.
- **Group and rank**: `block` first, then `warn`, then `nit`; dedupe repeats of
  the same class across pages into one entry with the page list.
- **Render the report** with `ava.ui.serve_markdown(report, "qa-sweep-<date>", port)`
  so the user reads it as a page, and open a task only for `block` findings.
- Low-confidence perceptual calls: list them under a "worth a human glance"
  tail, do not escalate them to tasks.

## Graduating a finding (the point of this skill)

A confirmed defect must not be found by eye a second time. Once the user
confirms one, push it down into a deterministic check and mark the checklist
row's `codified_as`:

- **Source-visible** (casing an attribute slipped, a forbidden string) → extend
  `ui/web/eslint-rules/sentence-case.mjs` (its COPY_KEYS / scope) or add a
  `no-restricted-syntax` selector in `ui/web/eslint.config.mjs`. Zero false
  positives, blocks at PR.
- **Rendered-DOM computable** (alignment, scrollbar, empty box, duplicate
  control) → add a structural assertion to the Playwright harness in
  `tests/e2e/` — the `playwright_page` + `frontend_proc` fixtures already exist
  (`test_spawn_button_mobile.py` shows the "exactly one visible control" shape).
  The probe that found it is the assertion you write.

The perceptual pass (step 3) is only for what has no deterministic check *yet*.
Everything that can graduate, should — the sweep gets cheaper each round.
