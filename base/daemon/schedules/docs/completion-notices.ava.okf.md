---
type: doc
title: Completion Notice Vocabulary
description: Completion notification policies and the outcome-free notice shared by RPC admission and persisted digest/outbox restoration.
tags:
- base
- contract
---

# Completion Notice Vocabulary

A platform completion notice is only `(source, content)`: no outcome, exit
code or success/failure distinction exists on the wire (`completion_notice` is a
boolean marker on the message), in the outbox or in `completion_notice_events`.
The `CompletionNoticePolicy` (`all`, `hourly`) in
`base/daemon/schedules/completion_notices.py` decides delivery. Config editors
retain the exact policy member choices; stored strings and JSON values use the
same spelling. A stored unknown policy (including the retired `failures`) is
rejected loudly.

`completion_notice_events.outcome` / `exit_code` and the old
`(agent_id, source, outcome)` unique constraint are unread and unwritten; the
columns remain until a contract migration drops them.
