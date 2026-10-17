---
type: doc
title: Panel navigation history
description: How a linked document enters the browser history and how Back, Forward and the panel's own Back control restore it.
tags: [frontend, navigation]
---

# Panel navigation history

The panel's reading position is the URL, not component state:
`?agent_id=<agent>&reference=<id>&reference=<id>` is the chain from `to_user.md`
to the document on screen. Every in-app navigation — a linked Markdown document,
or the home control — pushes a history entry, so browser Back returns to the
previous document instead of leaving the app and Forward returns to the one just
opened. A reload or a shared link restores the same chain.

`popstate` is the only way a traversal reaches the component: it re-reads the
chain from the location, so the view and the address bar cannot disagree. The
panel's own Back control follows the same semantics — it calls `history.back()`
for an entry this panel pushed (the entry's history state carries the marker) and
otherwise rewrites the current entry to the parent, because a deep link has no
in-app entry to return to. Selecting another agent drops the previous chain; it
belongs to the agent that was open. External links keep opening in a new tab and
non-Markdown resources keep their own document.

```meiva-contract
id = "panel-navigation-history"
kind = "invariant"
statement = "The panel's document chain lives in the URL as `?agent_id=…&reference=…`: opening a linked document pushes a history entry, a popstate traversal restores the chain it selects, and the panel's Back control calls `history.back()` for entries it pushed while a chain without history rewrites its own entry instead of leaving the app. Selecting another agent drops the previous chain."
code = ["ui/web/src/components/context/to-user-document.tsx:ToUserDocumentView", "ui/web/src/lib/context/reference-url.ts:readReferences", "ui/web/src/lib/agents/use-agents.ts:useAgents"]
tests = ["ui/web/src/components/context/to-user-document.test.tsx::To user navigation history::puts the opened document in the URL and follows Back and Forward to it", "ui/web/src/components/context/to-user-document.test.tsx::To user navigation history::walks browser history from the panel's back control, falling back to the parent", "ui/web/src/components/context/to-user-document.test.tsx::To user document bar::groups the document time with the refresh control at the right edge"]
```
