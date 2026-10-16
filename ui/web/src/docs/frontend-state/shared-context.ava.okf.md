---
type: doc
title: Shared user context
description: Workspace document reading, notification interaction and Inspector layout beside agent messages.
tags:
- frontend
---

# Shared user context

The home page displays the selected agent's workspace document beside its message
stream. Desktop gives the narrow agent tree 16% by default, then divides messages
and the shared-context panel 4:3. The document is the panel's body — notifications
tuck in above it — and it stays present for every selected agent: a missing or
empty document shows the empty state and a legacy `to_user.html` shows the
migration placeholder, never a collapsed panel. The panel itself is a lazy
boundary, loaded when the right panel first renders.

The inspector is the panel's secondary view, not a peer tab: the top-bar
Inspector toggle swaps the body to it (its own queries and ordered section list) and
the same toggle swaps back. The document body stays mounted while hidden, so the
switch preserves reply drafts and the document reading position. Below the
desktop breakpoint the shared context opens as a full-screen reading view from
the header's To user action — available whenever an agent is selected — with a
return-to-messages control.

`["to-user", agentId]` owns the root document snapshot. Linked Markdown uses
that query-key prefix followed by its reference chain. The home-machine ops
server reads `to_user.md`; there is no format setting, HTML frame, publication
registry or database content copy. `ChatMarkdown` owns typography, GFM, CJK
behavior and safe links for both conversation and document surfaces. The document
explicitly skips raw HTML. External links open in a new tab; linked Markdown
opens in the panel with back/home controls. Other files open or download through
the authenticated gateway resource endpoint.

The document response maps Markdown link/image destinations to resource IDs.
The backend parses Markdown with markdown-it-py, including reference-style links;
code fences and raw HTML do not expose resources. Relative paths resolve from
the containing Markdown file, absolute paths and `~/` paths resolve on the
agent's home machine. `file:` URIs with an empty or localhost authority work too.
Root-relative paths denote local files; use a full HTTP(S) URL for a web route.
There is no required assets directory, file copy or agent-owned HTTP server.

The browser submits only a chain of reference IDs. Every read traverses that
chain again from `to_user.md`, checking each link in the current parent document.
Deleted references stop resolving. Symlinked resources resolve to regular files;
the root document and workspace reject symlinks. Filesystem access runs under
the runner's OS identity. External URLs are never fetched by this resolver.

`POST /to-user/resource` on the existing authenticated ops listener transports
bounded binary bytes, using the ops worker pool, admission and shutdown accounting.
The gateway authenticates the user and isolates evaluation readers before resolving
the owner machine. Both ends enforce a 20 MiB per-resource bound. The current
transport buffers a bounded resource; it does not support ranges or large media
streaming. Markdown stays capped at 256 KiB, 256 local references per document
and eight linked-document hops. HTML and other active documents download; inline
previews carry a sandbox CSP and nosniff. No resource response is publicly cached.

One shallow scanner per ops process polls numeric workspace roots and only their
immediate `to_user.md` targets, on a worker thread. Creation, atomic replacement
and deletion broadcast `to_user_changed`; the global fold repairs that agent's
query prefix and includes it in reconnect repair. It does not recursively watch
linked directories. Refresh/focus rechecks referenced file stamps, so same-name
attachment replacements receive new versioned URLs even without a body edit.

Agent workspaces seed an empty `to_user.md` at boot, so the panel surface exists
from the agent's first turn; until the agent writes, the document area shows its
empty state (or the legacy placeholder), never nothing. An existing legacy `to_user.html` is preserved and
reported as awaiting conversion, never rendered. Agent workspace guidance tells
agents to rewrite their current shared context into Markdown. Removed stored
format pins are ignored by the existing config-pin resolver.

A failed read retains an already-cached snapshot and marks it stale; it never
turns a machine failure into an empty file. Files must remain available on their
source machine. This is not an offline document store or an archival snapshot.

On desktop the home page mounts the panel for every selected agent (the document
area owns its empty and legacy states); on narrow viewports the header's To user
action opens the same panel as the reading view.

```ava-contract
id = "panel-composition"
kind = "invariant"
statement = "The panel's body is the agent's document with the notice area inside it; the inspector is a secondary view that replaces the body while open — the document body stays mounted while hidden so reply drafts and reading position survive — and the document shows again when the inspector closes; the home shell mounts the panel for every selected agent, so a missing document shows its empty state instead of collapsing the panel."
code = ["ui/web/src/components/context/shared-context-panel.tsx:SharedContextPanel", "ui/web/src/app/page.tsx:HomeShell"]
tests = ["ui/web/src/components/context/shared-context-panel.test.tsx::shows the document as the panel's body without a view switcher", "ui/web/src/components/context/shared-context-panel.test.tsx::swaps to the inspector view and keeps the mounted document hidden", "ui/web/src/components/context/shared-context-panel.test.tsx::keeps the notice area inside the document body"]
```

```ava-contract
id = "refresh-ownership"
kind = "invariant"
statement = "The global SSE fold invalidates document queries on root file changes and reconnect. Explicit refresh, mount and window focus re-read the document and attachment stamps; attachments are not recursively watched. A changed attachment version refreshes its resource URL even when the Markdown text is unchanged."
code = ["ui/web/src/lib/context/use-to-user.ts:<module>"]
tests = ["ui/web/src/lib/fold/fold.test.ts::repairs only the changed agent's shared context and repairs it after reconnect", "base/agents/tests/test_to_user.py::test_linked_file_revision_changes_without_document_edit"]
```
