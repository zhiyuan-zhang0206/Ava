---
type: doc
title: Attachment contracts
description: Current implementation constraints and their executable evidence.
---

# Attachment contracts

Parent: [[meiva/impersonation/docs/attachment.meiva.okf.md]].

The attachment module loads on first controller use, so a bare SDK import does not load its kernel codec. The codec and snapshot reader remain the agent kernel owners. Native plugin faces must load before checkpoint state validation; surface-only loading would discard unregistered fields. Checkpoint state excludes journal replay, and already-receipted journal entries are not replayed. Framework state injection and full history replacement are refused by the shared codec. Ordinary SDK effects happen when called and are not rolled back by closing the attachment. Lease use rechecks controller ancestry and expiry; reading a previously loaded Python object does not renew or recheck the lease.
