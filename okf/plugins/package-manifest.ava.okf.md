---
type: doc
title: Package Manifest (spec v2)
description: The `ava-plugin.json` a package may ship — identity, dependencies, declared contribution surfaces, lifecycle shape — validated at install time by `shared/plugin_manifest.py`, with the console-contribution schema (`contributions.ui`) in `shared/plugin_ui_contributions.py`.
tags:
- plugins
---

# Package Manifest (spec v2)

## What It Declares
Packages may ship an `ava-plugin.json` at their root declaring identity
(name/version/`engines.ava` range), dependencies (`plugins` /
`pythonPackages` / `hostCapabilities`), contribution surfaces, and lifecycle
shape. Install paths validate it via `shared/plugin_manifest.py`; runtime
loading, lifecycle states, and context gates land post-open-source. Full
contract: [conventions/plugin-spec-v2.md](../../conventions/plugin-spec-v2.md).

## Console Contributions (`contributions.ui`)
`shared/plugin_ui_contributions.py` validates the web-console half: a plugin
declares `agentInspect` sections, `nav` entries, and `themes` token packs **as
data**, and the console renders them with its own components — it never runs
plugin JavaScript. Every vocabulary is closed: renderers and nav locations are
v1 final, icons are lucide names the frontend maps to components it imports,
and theme tokens are the console's own `:root` custom properties
(`ui/web/src/app/globals.css`), so a skin re-values what the app already
renders through and cannot ship a selector or a rule. Token values are
`oklch(...)` or hex literals only.

Declaration only today — like `skills`/`commands` there is no `register_*`
call behind it, so `agent/plugin_catalog.py:DECLARATION_ONLY_KEYS` carries it
and the declared-vs-registered diff has no registered side to compare against.
The aggregation endpoint, the page proxy, and the renderers are
[future/frontend-plugin-contributions.md](../../future/frontend-plugin-contributions.md).
