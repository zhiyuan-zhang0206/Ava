---
type: doc
title: Plugin config view — cluster projection gate and pin routing
description: The with_cluster_policy fetch gate and the read-side agent pin routing of base/packages/plugins/config_view.py.
tags: [base, plugins]
---

# Plugin config view — cluster projection gate and pin routing

## Cluster projection and the fetch gate

`with_cluster_policy` composes the local image with the gateway's declared
cluster projection; only a unit that is neither the local config source nor
exempt from fetching expects a packet. A gateway-capable unit that owns its
local image composes from it instead of raising (task #5234).

## Agent pin routing

`resolve_agent_plugin_pins` is the read side of overlay admission; the write
side is `resolve_overlay_targets`.
