---
type: doc
title: Permissions onboarding
description: "Every gate run: probe the macOS authorization matrix, ask for what is still undetermined, and re-read the system's verdict."
tags: [desktop, permissions, onboarding]
---

# Permissions onboarding

The gate's domain lives in `base/host/permissions_helper/grants`. This module is
the wiring a real host needs: the helper client, the child environment, a scratch
directory, the home directory and the record path, carried together as
`GateHost`. `GateService` is the object `meiva permissions`, the converge step
and the gateway endpoint share.

## Every run asks what is still undetermined

```meiva-contract
id = "every-run-asks"
kind = "invariant"
statement = "onboard probes first and then asks. An automatic pass (the converge step, so every start) requests exactly the rows the system reports not_determined -- Screen Recording and Accessibility through the helper's `request_grants`, folders and Automation targets through their own children. A row whose record is denied is not asked again automatically, and a stale row (record granted, helper not restarted) is never asked at all: neither can be resolved by another dialog. An explicit pass (the `meiva permissions onboard` verb, the console's Ask button) also asks denied prompt-mode rows, which is the only way back to a denied grant without a terminal. No caller attests that a person is present: a dialog nobody answers stays pending, and that cost was chosen over discovering a missing grant in the middle of a task."
code = ["services/desktop/permissions_helper/onboarding.py:onboard"]
tests = ["services/desktop/permissions_helper/tests/test_onboarding.py::test_the_first_pass_asks_both_desktop_grants_and_each_folder", "services/desktop/permissions_helper/tests/test_onboarding.py::test_a_denied_grant_is_not_asked_again_until_a_person_asks"]
```

The converge step runs the same pass on every converge, so an install, a restart
and a manual verb behave identically; only the ask set differs. A host whose
helper predates `request_grants_v1` keeps the checklist and loses the automatic
ask: `onboard` reports that in `diagnostic` and asks the folder rows.

## A request is never its own evidence

```meiva-contract
id = "request-is-not-evidence"
kind = "invariant"
statement = "After requesting, the gate re-probes and reports what the system says. A child that reports 'granted' while the system still denies leaves the item denied, and a request that raised no dialog (macOS answers a denied or already-asked grant silently) leaves the grant exactly as it was."
code = ["services/desktop/permissions_helper/onboarding.py:check"]
tests = ["services/desktop/permissions_helper/tests/test_onboarding.py::test_the_request_outcome_is_not_its_own_evidence"]
```

## A restart is custody, not a button

```meiva-contract
id = "restart-runs-outside-the-tree"
kind = "invariant"
statement = "The helper is the ancestor of this home's root, so nothing inside that tree may replace it: `install_and_load` refuses a loaded helper that differs from the requested artifact ('stop and unregister ... from outside its process tree'), and the restart verb prints the custody path instead of running `launchctl kickstart -k`. The symptom of the alternative, measured on 2026-10-17: a kickstart replaced the helper under the running root, every agent host exited with os._exit(70) -- 26 agents offline for six minutes -- and the host only recovered when the old root was shut down by hand. A grant macOS holds but the running helper has not read is fixed by `meiva stop`, then `meiva start`, which retires and recreates the helper under custody."
code = ["services/desktop/permissions_helper/onboarding.py:restart_helper", "services/desktop/permissions_helper/lifecycle.py:install_and_load"]
tests = ["services/desktop/permissions_helper/tests/test_onboarding.py::test_the_restart_verb_prints_the_custody_path", "base/host/permissions_helper/grants/tests/test_checklist.py::test_a_stale_row_points_at_the_custody_path_not_at_a_kickstart"]
```
