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

The converge step runs the same pass on every converge, so an install, a restart
and a manual verb behave identically; only the ask set differs. A host whose
helper predates `request_grants_v1` keeps the checklist and loses the automatic
ask: `onboard` reports that in `diagnostic` and asks the folder rows.

## A restart is custody, not a button

The helper is the ancestor of this home's root, so nothing inside that tree may replace it: `install_and_load` refuses a loaded helper that differs from the requested artifact ('stop and unregister ... from outside its process tree'), and the restart verb prints the custody path instead of running `launchctl kickstart -k`. The symptom of the alternative, measured on 2026-10-17: a kickstart replaced the helper under the running root, every agent host exited with os._exit(70) -- 26 agents offline for six minutes -- and the host only recovered when the old root was shut down by hand. A grant macOS holds but the running helper has not read is fixed by `meiva stop`, then `meiva start`, which retires and recreates the helper under custody.
