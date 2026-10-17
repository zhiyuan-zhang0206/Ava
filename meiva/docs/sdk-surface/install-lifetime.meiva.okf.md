---
type: doc
title: SDK installation lifetime — retained clock, event producer and atexit owner
description: What an installed SDK surface keeps for the process lifetime, and what uninstalling it removes.
tags: [sdk, lifecycle]
---

# SDK installation lifetime

An SDK installation retains its root-supplied Clock factory without evaluating
it at binding. Timestamped operations obtain a fresh clock from a bound context,
or from the installed process factory when unbound; a partial context missing a
clock fails explicitly rather than borrowing the installation.
`meiva.sdk_surface.process_context.loaded_code_image()` returns the immutable
first-load code fact and does not reread a moved checkout on reload or late
attachment.

SDK recorders capture the installation's lazy event producer, or the admitted
caller's ClientSet producer with identity, tally and durable capture. Changing
context mid-call cannot redirect that event. Sampling precedes producer
construction; durable capture precedes enqueue. An unbound Python caller uses
the installation's retained producer as a system caller without creating an
MeivaContext, and reload preserves that producer.

## The atexit owner follows the installation

`install()` registers the sampling owner's `close` with `atexit` when the caller
supplied no sampling owner — the bare-SDK process, which has no service lifespan
to schedule that cleanup. The registration belongs to that installation rather
than to the process at large: `uninstall()` unregisters the same `close` before
it drops the slot, so a removed surface leaves nothing behind that could
re-raise a refresh failure the surface no longer owns, while an installation
left in place still reports that failure at interpreter exit. This is finite
cleanup, not a promise of a nonzero exit code or of delivery after a hard exit.

```meiva-contract
id = "atexit-owner-follows-the-installation"
kind = "invariant"
statement = "The process-lifetime sampling close is registered with, and removed by, the installation that owns it: uninstalling the SDK surface unregisters that owner's close, so a removed surface cannot re-raise its refresh failure at interpreter exit, while an installation left in place (the bare-SDK process) still reports one."
code = ["meiva/sdk_surface/install.py:install", "meiva/sdk_surface/install.py:uninstall"]
tests = ["meiva/tests/namespaces/sdk_surface/test_sampling_atexit.py::test_installation_atexit_reports_failure_without_promising_a_nonzero_exit", "meiva/tests/namespaces/sdk_surface/test_sampling_atexit.py::test_uninstalled_surface_leaves_no_atexit_callback"]
```

Parent: [[sdk-surface.meiva.okf.md]].
