---
type: doc
title: Service host generation
description: The pty-sessions service's per-process generation id, what it lets a consumer tell apart, and why an absent service is unknown rather than empty for a consumer that acts on absence.
tags: [base, pty, sessions]
---

# Service host generation

The service (`services/agent_runner/pty_sessions/`) mints the generation in its
`__init__` and never persists it: it names one incarnation, which is what a
consumer needs to tell "my session is gone" from "my session's host is gone". The
`ping` handler answers `{"pid", "host_generation"}` and a client reads it with one
`ping`.

The read-only consumers that treat an absent service as "no sessions" (the SDK
listing, the cluster status snapshot) stay as they are: a service that is not
running holds no sessions, and that answer is what the operator wants to see. The
distinction matters only to a consumer that would act on the absence, and such a
consumer asks for the generation before it acts. See
[[../../../docs/host-generation.meiva.okf.md|session host generation]] for the
recovery rule built on it.
