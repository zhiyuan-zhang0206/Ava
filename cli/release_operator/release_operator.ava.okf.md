---
type: doc
title: Release operator surface
description: Thin single-host `ava cluster release prepare/request/adopt/status` verbs wired onto the existing release-prepare and release-transition machinery.
tags:
- cluster-lifecycle
- release
---

# Release operator surface

`ava cluster release prepare / request / adopt / status` are operator verbs
over the existing release machinery for **one host**. None of them add or
change release semantics; each is a thin wrapper that resolves paths under
`$AVA_HOME` and calls the same functions `python -m cli.release_prepare`, the
release-cycle preview, and `ava cluster update --prepared` already call.

There is no fleet model on this branch yet (`cli/release_fleet/`, slice FC-7:
`FleetRequest`, a coordinator, per-unit journals). `release request`'s
`--exclude`/`--reason` name a multi-host fleet exclusion and always refuse —
this slice closes the fleet-and-cutover plan's gap 4 ("no operator Request
builder") for exactly one host, not for a fleet.

## `prepare`

```bash
ava cluster release prepare --commit FULL_COMMIT_SHA --inputs LOCAL_INPUTS_JSON [--repo REPO]
```

Calls `cli.release_prepare.prepare_image` ([[cli/release_prepare/release_prepare.ava.okf.md]])
with `work` at `$AVA_HOME/releases/work/<commit>` and `store` at
`$AVA_HOME/releases`, creating both as owner-only directories if missing.
`--repo` defaults to this checkout's own root (`shared.paths.repo_root`).

`--inputs` is the exact `LocalInputs` document the standalone entry point
already required — a managed Python tree, a flat dependency wheelhouse, and
optionally a built frontend/collector/plugin tree, each with its inventory
digest. This package does not acquire those online: `cli.release_prepare.
acquire` exists but is not wired here, and CI instead assembles them by hand
in `.github/workflows/runtime-prepare.yml`. An operator supplies an
already-produced `LocalInputs` JSON from either path.

The work directory is keyed by commit, not by attempt: a first `prepare` for
a commit creates `work/<commit>`, and a **second** attempt for the same
commit — after an earlier failure — refuses (`Preparation.work` must not
already exist) until the operator inspects and clears it. This is the same
"retry needs fresh work, after the caller explicitly accounts for the
retained evidence" rule the wrapped entry point already documents; this verb
only fixes the directory's name so `release request` can find the receipt
again by commit alone.

## `request`

```bash
ava cluster release request --commit FULL_COMMIT_SHA --out REQUEST_JSON
```

Builds one `cli.release_transition.request.Request`
([[cli/release_transition/release_transition.ava.okf.md]]): `previous` is
this home's currently selected release, discovered read-only from the release
store and fully re-verified (`cli.release_operator.current`, never a
release-transition concept — `ReleaseRef` is always caller-supplied
elsewhere); `candidate` is read from the `prepare` receipt at
`$AVA_HOME/releases/work/<commit>/receipt.json`. `verify_pair` (unchanged)
still refuses a schema-changing transition — that needs the fleet migration
barrier, not this verb.

The written file is meant to be handed straight to the existing
`ava cluster update --prepared REQUEST_JSON`; nothing here submits or
dispatches it. Refuses if `--out` already exists, if there is no prepared
receipt for the commit, if this home has no active selection yet (run
`adopt` first), or if the candidate is already the active release.

## `adopt`

```bash
ava cluster release adopt --receipt RECEIPT_JSON
```

First image selection for a source-run home that has never selected a
release before: `activate_release(expected_current=None)` plus
`cli.release_transition.root_service.install_steady` — the same sequence
`scripts/preview/release_cycle_runtime.py::initial` already performs for the
preview's own captured bundle, generalized to a real home/registry and a
real `PreparationReceipt` file. Requires a stopped root
(`cli.commands.root_driver.require_root_absent`) and refuses if a release is
already selected (that is `request` + `ava cluster update`'s job).

Linux only. The persistent macOS home helper only ever starts inside an
existing release operation (`cli.release_transition.root_macos`); there is no
macOS root-seed-from-image action to adopt onto yet (`macos-release-start`).
A macOS host refuses here by design rather than approximating one.

## `status`

```bash
ava cluster release status [--operation OPERATION_ID] [--json]
```

Read-only. Reports the currently selected release (if any) and, by default,
this home's active operation journal (`$AVA_HOME/updates/active`); an
explicit `--operation` reads that operation's journal directly instead. No
lock is taken and nothing is written — `read_operation` only validates bytes
already on disk.
