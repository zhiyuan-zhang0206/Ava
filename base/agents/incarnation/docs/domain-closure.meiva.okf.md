---
type: doc
title: Exec domain closure
description: How the dedicated exec owner proves its domain closed, and what remains unknown.
tags:
- base
- exec
- process
---

# Exec domain closure

The dedicated owner's `ExecProcessDomain.close_confirmed` keeps the POSIX root
unreaped until [[group-closure.meiva.okf.md|group closure]] is proven.
It is distinct from successful signal submission. Query failure or timeout
remains unknown even if a fallback close subsequently kills members.
The operation does not cover unregistered POSIX session escapes.
These stronger operations are used by the independent owner, never by a
historical numeric PGID after its direct-child pin has been released.
