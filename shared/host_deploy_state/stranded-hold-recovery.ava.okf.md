---
type: doc
title: Stranded hold recovery retirement
description: The old automatic controller recovery and its status projection are removed.
tags:
- deploy
---

# Stranded hold recovery retirement

There is no pause-controller recovery session, abandoned-hold auto-release,
OS hold-watchdog verdict/budget, or stranded-hold heartbeat alert. Their settings,
writers, local note queue, API fields, CLI banner, and frontend banner are removed.
Historical database columns are unused and await an explicit cleanup migration
in [the lifecycle plan](../../future/infra/unified-cluster-lifecycle.md).

Ordinary maintenance still owns its captured pause generation and failed receipts.
Release continuation belongs to the retained finite operation executor. Removing
the old recovery actors does not provide the planned PITR operation authority.
