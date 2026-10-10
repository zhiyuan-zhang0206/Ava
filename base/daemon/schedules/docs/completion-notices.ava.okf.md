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
`base/daemon/schedules/completion_policy.py` owns the lightweight vocabulary
and stored-value validation; config and delivery readers use the same members.
`completion_notices.py` owns delivery decisions and digest rendering. Config editors
retain the exact policy member choices; stored strings and JSON values use the
same spelling. A stored unknown policy (including the retired `failures`) is
rejected loudly.
