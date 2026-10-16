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
- [[base/host/docs/brew_pin.ava.okf.md]] — Operator-approved tool pins: brew formula manifest, uv release digests, apt-series pins, brew probes.

`base/host/system/hostport.py` is the port to this machine. A component that starts a process,
looks up a program or branches on the platform takes a required `host: Host` with four fields:
`run` (`proc.run_bounded`, whose timeout bounds the whole process tree), `which`
(`shutil.which`), `platform` (`base.native_process.os_platform.Platform`: `system` as `linux` or
`darwin`, `machine`, `executable`; it is defined there once because `native_process` sits below `base.host`) and `uid`. The composition root builds it with `Host.system()`; a test builds the
`Host` it wants with `base.host.system.tests.fake_commands.FakeCommands` as `run`, which records each
argv, answers from a script routed by argv prefix and refuses an unscripted command. A test that
needs a real process uses `Host.system()`. The four fields are the whole port; a new capability
is a reviewed addition, not a field that appears on its own.

`base/host/atomic_io.py` publishes a file by writing a sibling temporary file, syncing it,
renaming it over the target and, when asked, syncing the directory. The three calls whose failure
a test must cause are the fields of `Fs` (`replace`, `fsync`, `fchmod`), and an `AtomicWriter`
runs every write through one. `write_text_atomic`, `write_bytes_atomic` and `fsync_parent` are the
writer over `SYSTEM_FS`; a caller with no policy of its own calls them, while a caller whose own
policy warns, suppresses or never syncs takes a required `AtomicWriter` ([python
conventions](../../../docs/conventions/python-conventions.md)). The module's own tests build
`AtomicWriter(Fs(...))` with the failure they need (a failed replace keeps the old content, a
failed file sync never commits, a failed directory sync leaves the new content committed); other
components' tests use real files and patch none of these calls. A converted caller proves its own
policy with the one `AtomicWriter` its test builds.

`base/host/net/resilience.py` owns immutable `Policy` parameters and the shared
`retry` / `aretry` executors, including backoff, jitter, classification and
idempotency gates. The module wait hooks are the default; an operation that already
holds its own clock passes its wait per call (`retry(policy, sleep=timers.sleep)`),
so its backoff waits with that clock — and a caller that passes `ManualTimers`
records the waits and advances virtual time instead of blocking.
Bootstrap fetches use this owner. Provider and Redis-listener
loops still have dedicated contracts; the remaining Redis audit is a
[future item](../../../future/infra/engineering/retry-consumer-contracts.md).

```ava-contract
id = "retry-caller-wait"
kind = "invariant"
statement = "retry() sleeps through the module wait hook by default; a caller that passes its own wait (sleep=timers.sleep) waits through it at every retry, so a ManualTimers records the waits and advances virtual time while the module hook records nothing."
code = ["base/host/net/resilience.py:retry", "base/host/net/tests/test_resilience.py:TestWaitHooks.test_retry_uses_the_callers_own_wait"]
tests = ["base/host/net/tests/test_resilience.py::TestWaitHooks::test_retry_uses_the_callers_own_wait"]
```

`base/host/net/ports.py` is the port for an HTTP request whose answer a test controls.
`UrlOpener` has `urllib.request.urlopen`'s shape (a request or a URL and a timeout in, a readable
context manager out whose surface is `read` and `read1`: a caller that owns a wall-clock budget
reads with `read1`, one socket read per call, instead of an aggregate `read` that waits for the
requested size); a component that makes the request takes it as a required argument and a
composition root passes `SYSTEM_URL_OPENER`. `base.host.net.tests.fake_url_opener.FakeUrlOpener`
records each request and answers from a script routed by URL prefix, raises `HTTPError` for a
status outside 2xx as `urlopen` does, and refuses a URL nobody scripted. A test that wants the
real client starts a listener on port 0; none patches `urlopen` or `socket`.

The private-storage live Unix-socket test uses a short `/tmp` root so macOS's
AF_UNIX path limit does not fail at bind before the test reaches socket skipping
and ordinary-file mode repair.
