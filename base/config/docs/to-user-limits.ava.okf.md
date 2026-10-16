---
type: doc
title: To user panel limits
description: The read-side guards of the shared to_user.md panel — configuration, widened defaults and the reason behind each bound.
tags: [configuration]
---

# To user panel limits

The panel's read-side guards are cluster config (`settings.to_user`): the ops
listener on the agent's home machine judges every document and resource, and the
gateway repeats the streaming guard, so both process kinds consume the fields and
all three process profiles carry the domain. The defaults widen the
first-version hardcoded bounds
([limits decision](../../../docs/decisions/frontend/documents/2026-10-17-to-user-panel-limits-configurable.md)).

```ava-contract
id = "to-user-read-limits"
kind = "invariant"
statement = "The four read-side guards are `settings.to_user` fields, cluster-pinned and consumed by both the ops listener and the gateway. max_document_bytes (1 MiB) guards against reading the wrong file as a document, not against writing — the soft word limit shapes the panel. max_references (1024) caps the per-reference stat probes of a pathological document. max_chain_depth (16) stops runaway or circular reference chains. max_resource_bytes (64 MiB) bounds one served file and with it the gateway's per-request buffering. Every bound must be positive; the first-version values were 256 KiB / 256 / 8 / 20 MiB."
code = ["base/config/domains/to_user.py:<module>"]
tests = ["base/config/domains/tests/test_to_user_config.py::test_limits_default_to_the_widened_bounds", "base/config/domains/tests/test_to_user_config.py::test_every_limit_must_be_positive"]
```
