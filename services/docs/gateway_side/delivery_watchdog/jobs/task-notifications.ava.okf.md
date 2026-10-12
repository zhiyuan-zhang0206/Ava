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
inbound queue. The live pending scan repairs lost wake hints; it never revives
a terminated recipient. An assignment note marked `task_notification=true` and
`delivery_resurrect=true` requests one resurrect from the committing request, and
the home wake transaction holds a task-owner fence through commit. Reassignment
supersedes old pending directions, including A -> B -> A. Informational notes
and reminders retain their no-resurrection policy. Age-expired assignment notes
are retained as done with `delivery_result` failure evidence. See
[[../../../../../ava_builtins/plugins/ava_fleet/docs/tasks/task_notification.ava.okf.md|Task notification]].
