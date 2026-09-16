---
type: doc
title: Plugin Faces — SDK Surface and Agent Runtime
description: A plugin loads in up to two faces — `plugin.py` (SDK surface — namespaces, wraps, the child-loadable registrations) and the optional `agent_runtime.py` (agent-side registrations — state fields, hooks, prompt sections) — and which load form imports which.
tags:
- plugins
---

# Plugin Faces — SDK Surface and Agent Runtime

## Two faces: `plugin.py` surface, `agent_runtime.py` face (task #3633)
A plugin loads in up to two faces. `plugin.py` is the SDK **surface**:
namespaces, wraps, and the other registrations an agent-launched child needs to
run agent-authored code — its imports must stay off the agent runtime (no
`agent.state`, `agent.hooks`, `agent.graph.*`, or LangChain chain;
`tests/agent/test_lazy_child_imports.py` locks this in clean subprocesses).
`agent_runtime.py` is the optional **face** carrying the agent-side
registrations (state fields, graph hooks, system-prompt sections).

Which faces load is the caller's choice:
- `load_extensions(surface=True)` — surfaces only, no reset. The child's
  stateless boot; a reset would import `agent.state` and put the graph stack
  back on the child's path.
- `load_extensions(surface=False)` (the default) — reset
  (`clear_plugin_registrations()`), then each plugin's surface followed by its
  face. The graph build and agent-side tooling take the full form.
- `load_agent_faces()` — faces alone, for a process whose surfaces are already
  loaded: host boot after `scan_and_load`, and a child whose request carries a
  state snapshot upgrading from the surface load (state fields are part of the
  state schema it rebuilds).

A face loads as `<pkg>.<name>.agent_runtime` through the same by-path
primitive, with per-face fail-soft containment: a face that raises is reported
and skipped, the surface stays loaded.
