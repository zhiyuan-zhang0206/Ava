---
type: doc
title: Session host generation
description: The generation identity of the process that hosts a backend's sessions, and the recovery rule both a replaced root and a replaced session host follow.
tags: [base, process, supervision]
---

# Session host generation

A backend names the generation of the process that hosts its sessions and answers None when it cannot tell. A changed generation means the host was replaced and every session it held ended with it, which is one new chance for a supervised resident process; a silent death inside one generation stays that component's own failure.

One rule serves both recoveries that follow it: a root retires itself when the
permissions helper that spawned it is replaced, and a schedule is relaunched when
its session host is replaced. In both, a component is never retried inside its own
generation; the supervisor rebuilds it in the new one.

`None` is a deliberate answer, not a failure. A backend with no generation source
(the native named-session backends) says "unknown", and a consumer must then take
no irreversible action on absence — an unreachable host and an empty host are
different facts, even though both answer with no session names.

See [[session-backend.meiva.okf.md|session backend]] for the interface this
belongs to.
