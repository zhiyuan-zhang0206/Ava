---
type: doc
title: Durable task notification recovery
description: Recover committed task assignment system notes through the existing inbound queue, with current-owner fencing and bounded failure.
tags:
- delivery
- tasks
---

## Durable task notifications

Task mutation producers commit internal assignment system notes into the existing
inbound queue. The live pending scan repairs lost wake hints; G4 also recovers
terminated recipients only when `task_notification=true`,
`delivery_resurrect=true` and the linked task still belongs to the recipient.
The home wake transaction holds a task-owner fence through commit. Reassignment
supersedes old pending directions, including A -> B -> A. Informational notes
and reminders retain their no-resurrection policy. Age-expired assignment notes
are retained as done with `delivery_result` failure evidence. See
[[../../../../../ava_builtins/plugins/ava_fleet/docs/tasks/task_notification.ava.okf.md|Task notification]].
