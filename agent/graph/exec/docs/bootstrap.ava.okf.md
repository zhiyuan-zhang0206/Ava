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
