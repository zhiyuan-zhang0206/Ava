---
type: doc
title: Execution Child Bootstrap Ownership
description: Configuration, SDK capabilities and admission belong to the execution entry.
tags: []
---

# Execution Child Bootstrap Ownership

The formal `agent.execution.child` interpreter entry owns its SDK bootstrap.
It validates the request before installing plugins and applies framework pins
to the same `ConfigBoot` supplied to its SDK authority, Clock factory, and
Gateway transport readers. Package import does not install a second owner for
this entry. Bare agent-launched Python retains its existing import-time plugin
surface contract; a request-path environment value alone cannot select the
formal execution posture.

The `agent.graph.exec` package initializer imports no graph or LM machinery:
the child must be able to import `protocol` before user code without loading
the node stack. `agent/tests/execution/test_lazy_child_imports.py` checks this
lightweight import boundary. Keep `agent.execution.child` recognizable as a
`python -m` process entry when tracing callers; another checkout's parent can
name it without a Python import in that parent's source.
