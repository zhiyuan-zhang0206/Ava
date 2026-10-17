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
