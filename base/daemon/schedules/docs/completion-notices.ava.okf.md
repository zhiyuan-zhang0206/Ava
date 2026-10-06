---
type: doc
title: Completion Notice Vocabulary
description: Separate completion outcomes and notification policies, shared by RPC admission and persisted digest/outbox restoration.
tags:
- base
- contract
---

# Completion Notice Vocabulary

Platform completion metadata and the restart-safe hourly digest share
`CompletionNoticeOutcome` in `base/daemon/schedules/completion_notices.py`:
`exit` carries a process exit code; `missed` carries none. The separate
`CompletionNoticePolicy` (`all`, `failures`, `hourly`) decides delivery, not
completion outcome. RPC validation and persisted metadata/database restoration
produce outcome members and reject unknown values. Damaged outbox metadata
retains its payload-error isolation. Config editors retain the exact policy
member choices; stored strings and JSON values use the same spelling.
