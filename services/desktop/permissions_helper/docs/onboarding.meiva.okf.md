---
type: doc
title: Permissions onboarding
description: "First-run authorization pass for the desktop helper: what it requests, when it refuses to, and how the result is re-read."
tags: [desktop, permissions, onboarding]
---

# Permissions onboarding

The gate's domain lives in `base/host/permissions_helper/grants`. This module is
the wiring a real host needs: the helper client, the child environment, a scratch
directory, the home directory and the record path, carried together as
`GateHost`. `GateService` is the object `meiva permissions`, the converge step
and the gateway endpoint share.

## No dialog without a human at the machine

```meiva-contract
id = "no-dialog-without-a-human"
kind = "invariant"
statement = "onboard raises a permission dialog only when its caller attests a person is present (a TTY on the CLI and the converge step, a confirmed request on the API). Without that, it degrades to check and requests nothing: a pending dialog blocks synthesized input machine-wide until someone answers it."
code = ["services/desktop/permissions_helper/onboarding.py:onboard"]
tests = ["services/desktop/permissions_helper/tests/test_onboarding.py::test_a_non_interactive_pass_never_raises_a_dialog"]
```

## A request is never its own evidence

```meiva-contract
id = "request-is-not-evidence"
kind = "invariant"
statement = "After requesting, the gate re-probes and reports what the system says. A child that reports 'granted' while the system still denies leaves the item denied."
code = ["services/desktop/permissions_helper/onboarding.py:check"]
tests = ["services/desktop/permissions_helper/tests/test_onboarding.py::test_the_request_outcome_is_not_its_own_evidence"]
```

The converge step requests on a host's first converge only (no record yet) and
reminds afterwards, so a restart loop cannot turn into a dialog loop.
