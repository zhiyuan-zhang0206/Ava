---
type: doc
title: Inspector widgets
description: The plugin inspector-widget face — a closed-set data declaration the gateway resolves per agent and the console renders without executing plugin code.
tags: [plugins, contracts]
---

# Inspector widgets

`base/packages/plugins/surface/inspector.py` is the declaration half of the
inspector-widget surface (task #2909): a plugin's `inspector.py` exports
`contribute()` returning a `PluginContributions` with `inspect_widgets`, exactly
parallel to `metrics.py`. The gateway loads each enabled plugin's face into the
data registry (`base/packages/plugins/data_registry.py`, served by
`gateway/inspect/_plugin_widgets.py`) and answers
`GET /api/agents/{id}/inspect/widgets`; the console renders the resolved payload
inside the panel's section list (`ui/web/src/components/inspector/inspector-panel.tsx`).

**Declaration is data; resolution is kernel-side.** A spec carries no callables
and no queries — which rows a widget resolves for an agent is the gateway's job
(`taskList` lists the agent's active tasks, complete, no cap). A widget with no
data renders nothing: it shrinks, it does not lie.

**The console never executes plugin code or markup.** A widget is closed-set
data rendered by the console's own components, keyed by a `kind` from
`WIDGET_KINDS`; anything unknown — a kind from a newer kernel, a drifted field —
is skipped at render time or rejected at validation, never interpreted. A new
kind family is a deliberate change in this module plus a renderer branch in
`ui/web/src/components/inspector/inspector-widgets.tsx`, not a free-form field
(task #3216 reshaped the #2909 jump buttons into the `taskList` family).

The registry admits a face whole and registers nothing process-wide. A duplicate
id within one plugin is refused; the same id across plugins is fine; `plugin` is
filled from the registry entry over any author-supplied value (mirroring
`MetricSpec.plugin`). `DuplicateInspectWidget` reads as its refusal event, the
sibling of the metric face's `DuplicateMetric`; inline suppressions cannot carry
that rationale under the structured-comment gate, so the naming rule N818 is
silenced for this module in `pyproject.toml` per-file-ignores. The cross-face
load rules live in [[docs/plugins/declared-contributions.meiva.okf.md|declared contributions]].

A widget is closed-set data: its kind comes from WIDGET_KINDS, unknown kinds and unknown fields are refused at validation (or skipped at render), and the spec carries no callables or queries. The gateway resolves each widget's payload per agent server-side; an empty payload drops the widget from the response.

## Key Dependencies

- [[runtime-contributions.meiva.okf.md]] — the face/registry model this declaration joins
- [[docs/plugins/declared-contributions.meiva.okf.md]] — every declared contribution face
