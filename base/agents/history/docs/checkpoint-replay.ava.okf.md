---
type: doc
title: Checkpoint Message Replay
description: Complete message ancestry, valid empty states and explicit retained-history reads.
tags: []
---

# Checkpoint Message Replay

`HistoryPostgresSaver` and `HistoryAsyncPostgresSaver` follow an exact
checkpoint's parent chain. A missing parent without a stored seed or a real
message reset raises a reconstruction error. A delta channel version with
neither a seed nor writes is also unavailable. These failures never become
an empty conversation or a guessed chronological chain.

An explicit empty snapshot, an empty write and a fresh non-delta checkpoint
remain valid empty states. Read-side detection neither repairs nor writes
checkpoint storage. A real reset permits replay without its older prefix;
a historical request before that reset still requires its own complete chain.

The public `load_checkpoint_messages` and segment readers preserve the
original cause through `CheckpointReadError`. Gateway `/timeline` surfaces
failed reads as 503. `/timeline/retained` reads an explicitly identified compact
boundary and pages using historical item cursors without loading the live head.
Its boundary identity and lack of a live message count distinguish retained
history from execution state. An independently readable retained boundary
does not prove the current turn can resume safely.

`tests/test_checkpoint_postgres_walks.py` places 1,023 then 1,024 newer
checkpoints ahead of a target to cross the history reader's page boundary. The
retained-segment read exercises that older target, including its writes and seed.
Its serializer test also checks that instrumenting cold readers leaves a shared
serializer's decode method unchanged; timing one read must not install
process-wide instrumentation on other readers.
