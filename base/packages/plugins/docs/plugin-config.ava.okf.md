---
type: doc
title: Plugin config images and agent views
description: Explicit ownership, boot overlays and validation of plugin configuration.
tags: [base, plugins]
---

# Plugin config images and agent views

Ownership follows the
[plugin config image decision](../../../../docs/decisions/extensions/plugins/2026-10-09-plugin-config-images-have-an-owner.md).

The config face declares one frozen Pydantic model. Its full authority image
lives at `$AVA_HOME/configs/<plugin>/config.json`. Reading a missing image can
use declared defaults; binding creates that initial image. Existing malformed
data and schema drift raise their explicit errors, without repair or fallback.
`ava plugins update` owns schema reconciliation.

The SDK installer binds instances into its local build and publishes them on
`Installation.configs`. The declared class is the instance's type; there is no
second registry to synchronize. Rollback removes local bindings and a failed
installation publishes nothing. The SDK module reads its current installation;
services and agent views receive the resolved image explicitly.

The exec child applies core overlay fields before loading the SDK and plugin
overlay fields after installation. The latter builds a replacement config
image and the installer publishes it. Frozen instances and the caller's input
mapping are not mutated. This does not introduce in-process plugin reload.

The agent host holds its boot image and supplies it when resolving
`AgentSlices`. `PluginConfigView` layers only that agent's pins over the image,
memoizing frozen instances within that view. External attachments construct
the same view from their process's installation. Separate roots and agents
do not share a registration table or overlay state.

Plugin config has no `birth_config`: without an explicit agent override, a
new process reads the current authority image. Core frozen/live field rules,
fresh-file config-service reads, outbox ticks, and bootstrap delivery retain
their own existing update boundaries. Provider credentials remain in the
declared secret-delivery channel, outside plugin config images.

Gateway and ops need no SDK installation to validate an overlay. They read
enabled config declarations; an SDK caller additionally supplies its bound
image so validation retains its existing base values. Unknown keys, ambiguous
owners, non-overridable fields and invalid values are rejected before writes.
