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

- [[ops/docs/env-audit.ava.okf.md]] — Base — .env write audit & integrity guard.
- [[base/host/env/docs/registry.ava.okf.md]] — Environment declarations and boot/child projections.
- [[base/host/docs/runtime_interpreter.ava.okf.md]] — Loaded-runtime interpreter binding.

`base/host/system/hostport.py` is the port to this machine. A component that starts a process,
looks up a program or branches on the platform takes a required `host: Host` with four fields:
`run` (`proc.run_bounded`, whose timeout bounds the whole process tree), `which`
(`shutil.which`), `platform` (`base.native_process.os_platform.Platform`: `system` as `linux` or
`darwin`, `machine`, `executable`; it is defined there once because `native_process` sits below `base.host`) and `uid`. The composition root builds it with `Host.system()`; a test builds the
`Host` it wants with `base.host.tests.fake_commands.FakeCommands` as `run`, which records each
argv, answers from a script routed by argv prefix and refuses an unscripted command. A test that
needs a real process uses `Host.system()`. The four fields are the whole port; a new capability
is a reviewed addition, not a field that appears on its own.

`base/host/net/resilience.py` owns immutable `Policy` parameters and the shared
`retry` / `aretry` executors, including backoff, jitter, classification and
idempotency gates. Bootstrap fetches use this owner. Provider and Redis-listener
loops still have dedicated contracts; the remaining Redis audit is a
[future item](../../../future/infra/engineering/retry-consumer-contracts.md).
