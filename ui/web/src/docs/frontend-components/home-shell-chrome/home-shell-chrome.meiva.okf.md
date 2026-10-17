---
type: doc
title: Home shell chrome
description: The home columns' title bars and where the Details mode is chosen.
tags:
- frontend
---

# Home shell chrome

## Column title bars

The three home columns — Agent Tree, Timeline, right panel — each carry a title
bar `BAR_HEIGHT_PX` (44px) tall that paints `BAR_DIVIDER_CLASS`: a 1px rule inset
4px from both ends, so the three read as three separate rules on one line rather
than as one page-wide divider. The right panel's document bar (the "Updated …"
strip) is such a bar, not a section header — it takes the wrapping variant
(`COLUMN_TITLE_BAR_WRAP_CLASS`: the same 44px as a minimum, allowed to grow only
when its content cannot fit one line), so at desktop widths its rule ends on the
same line as its neighbours. The notice header above it takes the fixed variant
for the same reason. Both variants are `relative`: the rule is the bar's own
`after` element, painted against the bar and not against the section around it.

```meiva-contract
id = "column-title-divider"
kind = "invariant"
statement = "Every home column title bar is BAR_HEIGHT_PX tall and paints BAR_DIVIDER_CLASS's inset rule, so the Agent Tree, Timeline and right-panel dividers sit on one line; the right panel's document bar takes the wrapping variant (44px minimum, growing only when its content cannot fit one line) and its notice header takes the fixed variant — neither draws a border on its section."
code = ["ui/web/src/lib/layout/layout.ts:COLUMN_TITLE_BAR_CLASS", "ui/web/src/components/context/to-user-document.tsx:ToUserDocumentView", "ui/web/src/components/context/shared-context-panel.tsx:SharedContextPanel"]
tests = ["ui/web/src/components/context/to-user-document.test.tsx::To user Markdown::draws the panel title rule on the shared column title bar", "ui/web/src/components/context/shared-context-panel.test.tsx::uses the shared column title bar for the notice header"]
```

## Details mode

The mode the timeline expands work blocks to (`display.expand_runs_mode`: All /
Last / None) is chosen in one place — the Display settings row — and nothing on
the composer shows or changes it (user decision 2026-10-17: the home screen
carries no Details control, not even a read-only summary; the chrome belongs in
settings). That row re-applies the current mode when it is picked again (the
manual-expansion reset). Because the picker's page unmounts the timeline, the
reset is recorded as a pending flag in the store and consumed by whichever
timeline mounts next — a token alone could not survive that gap.

```meiva-contract
id = "details-mode-owner"
kind = "invariant"
statement = "The Details mode has one writer: the Display settings row bound to display.expand_runs_mode, which keeps all three modes (All / Last / None) reachable. A pick leaves a pending reset that the next mounted timeline consumes, so re-picking the current mode still collapses manual expansions even though the picker's page had unmounted the timeline. The home screen carries no Details control or summary of any kind; settings is the only entry point."
code = ["ui/web/src/app/control/display/page.tsx:resetManualExpansions", "ui/web/src/lib/layout/content-toggle-store.ts:<module>", "ui/web/src/components/timeline/index.tsx:TimelineView"]
tests = ["ui/web/src/app/control/display/page.test.tsx::DisplaySettingsPage::writes the details mode picked in settings", "ui/web/src/app/control/display/page.test.tsx::DisplaySettingsPage::re-applies the current mode when it is picked again", "ui/web/src/lib/layout/content-toggle-store.test.ts::useContentToggleReset::a re-pick leaves a pending reset for the next mounted timeline", "ui/web/src/components/timeline/timeline.test.tsx::Details mode re-pick made while the timeline was unmounted::collapses the manual expansions on the next mount"]
```
