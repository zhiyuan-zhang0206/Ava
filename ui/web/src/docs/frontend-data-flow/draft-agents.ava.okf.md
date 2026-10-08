---
type: doc
title: Browser Draft Agent Submission
description: Guarded Guide, Schedule Maker and Package Installer birth acceptance without automatic resend.
tags: [frontend, agents, idempotency]
---

# Browser Draft Agent Submission

Guide, Schedule Maker and Package Installer use their fixed
`/api/keyed/v1/{guide,schedules,packages}/draft` POST paths with one operation
key and `principal-v1` scope. Transport helpers accept an explicit key to replay
the same intent; another default invocation represents a new submission.
Only a 200 containing a positive agent ID opens the accepted conversation.
Pending Enter presses are ignored and mutation retries are explicitly disabled.
An HTTP rejection or uncertain transport error is surfaced without a second
POST or legacy fallback. There is no client Outbox or automatic resend.

The response is historical birth acceptance, not readiness or execution proof.
Original-birth recovery still requires supporting Gateway generations; this
client change performs no runtime rollout.

Server owner: [[gateway/agents/docs/guarded-drafts.ava.okf.md]].
