# Run timeline: whole-lifetime window, all layers, drill by window

Decision (2026-10-05): the single-agent run timeline has no compact/current
session switch. Without `from`/`to` the endpoint serves the agent's whole
lifetime and every level of the understanding tree, one row per level.
Drilling a node narrows the window to its span; breadcrumbs step back.

Why: a compact is a context boundary, not a semantic boundary of the work, so
splitting the view at it hid the task's real shape. The understanding tree is
the navigation structure, and a window is its only cursor.

Rejected: keeping the session route as a second axis (two cursors for one
tree); capping each level at N nodes and serving the finest level that fits
(the `AVA_RUN_TIMELINE_LAYERS_MAX_NODES` setting is removed). Cost accepted:
the response carries every node in the window, so a long-lived agent's
full-lifetime read is the largest one; narrowing the window is the relief.

Superseded in part: the window is still the whole lifetime, but the data under it is now the message history and the understanding tree, not events — see `decisions/2026-10-05-run-timeline-from-messages-and-tree.md`.
