---
type: doc
title: Soft compact preparation
description: Reminder delivery, current-window attribution and reply opportunity.
tags: [agent, compaction, contracts]
---

# Soft compact preparation

## Soft preparation delivery

Soft preparation fires once per compact version, only above the soft threshold and at or below the hard ceiling with conversation to preserve. Enabled plugin builders run in declaration order; blank text opts out, non-string results fail before the bookmark is committed, and peer traffic does not starve delivery.

## Occupancy and reply opportunity

Both compaction gates use the latest provider input_tokens, with chars/4 only before a measurement exists. A committed summary remains exempt from another automatic compact or reminder until a later AIMessage is committed.

The measurement describes the previous model request, so the completion buffer must absorb its one-turn lag. The reply exemption is derived from checkpointed messages; cancellation, restart and new inbound notes do not erase it.
