---
type: doc
title: Prepared operator entry work in progress
description: Retained CLI preflight dispatch before an intentionally unimplemented cutover.
---

# Prepared operator entry (unfinished)

The explicit `ava cluster update --local --prepared PLAN` parser runs before
mutable checkout anchoring. It validates a private immutable request against the
loaded POSIX image, complete unit receipt and recovery image. Invalid flag
combinations do not fall through to the source updater or old gateway RPC.

The shared pending dispatch binds one request UUID, full request digest,
complete normal plan and original deadline. Only the unique registered gateway
unit can create an operation from stable state. Each unit then binds that exact
operation, records deterministic local preflight evidence, and waits for every
registered unit at the all-unit prepared barrier. Participants never create a
lease, select a latest pending operation, or reattach to a replacement holder.
Unit preflight records are immutable and are not post-stop closure or normal
service readbacks.

An existing pending operation refuses ordinary creation. Coordinator recovery
requires a new request ID, exact predecessor CAS, and a canonical owned fresh
all-unit writer-closure file. That collection supplies the new operation holder
and acquisition time; its target SHA is cross-checked against the validated new
plan. The closure producer is not implemented by this entry.

**This is not a working first-cutover command.** It refuses expired plans,
invalid flag combinations, missing projection variables, non-gateway
coordinators, and recovery without the fresh closure. It also has no remote
participant-leg transport: the operator starts the retained entry separately on
each unit.

After the prepared barrier, nothing stops services, collects/adopts the writer
closure, migrates, changes selectors, starts services, reads them back, or
finalizes publication. Missing implementation includes the normal LKG artifact
producer, first source/native orchestrator handoff, complete non-session and
launcher quiescence, writer-closure producer, coordinator collection/adoption/
migration dispatch, checked reverse transitions, and remaining normal-service
readiness adapters. The fixed-base legacy cold-boot proof does not provide these
capabilities.

Parser, dispatch, and real PostgreSQL regressions are written but not executed.
No activation, production readiness, full CI, or all-unit writer closure is
claimed.
