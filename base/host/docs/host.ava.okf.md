---
type: doc
title: Host primitives
description: Operating-system integration, environment ownership and local host utilities.
tags: [base]
---

# Host primitives

`base/host/` owns operating-system integration, environment ownership and local host utilities.
Its component nodes describe the current contracts and implementation.

`net` groups config-free URL and host predicates, IPv4 HTTPX dials and retry
helpers. HTTPX uses its installed import behavior and public entry points.

## Documented components

- [[base/host/env/docs/audit.ava.okf.md]] — Base — .env write audit & integrity guard.
