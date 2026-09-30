---
type: doc
title: Release operator surface
description: Thin prepare/request/adopt/exclude/status entry functions over release preparation and the fleet release transition; no CLI verb registers them.
tags:
- cluster-lifecycle
- release
---

# Release operator surface

`prepare / request / adopt / exclude / status` are operator entry functions over
the release machinery. No CLI verb registers them (the `ava cluster release` group
and `ava cluster update` are removed), so nothing in production reaches this
package. None of them decides a release: each
resolves paths under `$AVA_HOME` and calls release preparation or the fleet
release transition ([[cli/release_fleet/docs/release_fleet.ava.okf.md]]), where the
coordinator decides.

## `prepare`

```text
prepare --commit FULL_COMMIT_SHA --inputs LOCAL_INPUTS_JSON [--repo REPO]
```

Calls `cli.release_prepare.prepare_image` ([[cli/release_prepare/docs/release_prepare.ava.okf.md]])
with `work` at `$AVA_HOME/releases/work/<commit>` and `store` at
`$AVA_HOME/releases`, creating both as owner-only directories if missing.
`--repo` defaults to this checkout's own root (`base.paths.repo_root`).

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

```text
request --commit FULL_COMMIT_SHA --out REQUEST_JSON \
  [--receipt RECEIPT_JSON] [--exclude MACHINE:HOME ... --reason R] [--watch-s S] \
  [--alert-agent AGENT_ID] [--alert-webhook-file NAME] [--acknowledged-rejection OPERATION_ID]
```

Builds the gateway home's `FleetRequest`: `previous` is this home's currently
selected release, discovered read-only from the release store and fully
re-verified (`cli.release_operator.current`); `candidate` is read from the
`prepare` receipt at `$AVA_HOME/releases/work/<commit>/receipt.json` (or an
explicit `--receipt` naming that commit). Every other registered unit
(`machine_units`, read with the operator's gateway login) must be accounted
for: a paused machine's units become exclusions (reason `paused`),
`--exclude` with `--reason` excludes a unit (reason `operator`, recorded
with the operator's user name), and any other unit would take part, which
refuses naming slice dbgen-8 (its receipt and capability travel over the
handoff and the coordinator channel). A single box is a fleet of one.
`--watch-s` shortens the captured watch window; every other policy bound
keeps its `FleetPolicy` default. The policy's alert route and oscillation
guard are the operator's too: `--alert-agent` adds an observing agent's
notice to every fleet alert, `--alert-webhook-file NAME` the out-of-band
webhook whose URL the owner-only `$AVA_HOME/secrets/NAME` holds (checked
here: a missing or group/other-accessible file refuses), and
`--acknowledged-rejection` names the latest operation that rejected the
candidate, the only way to request a rejected candidate again. `verify_pair` still refuses a
schema-changing transition.

The written file is the request the release handoff consumes;
nothing here submits or dispatches it. Refuses if `--out` already exists, if
there is no prepared receipt for the commit, if this home has no active
selection yet (run `adopt` first), or if the candidate is already the active
release. The builder runs in the home's admitted runtime (the only operator
process that receives a database login); the candidate consumes its output.

## `adopt`

```text
adopt --receipt RECEIPT_JSON
```

First image selection for a source-run home that has never selected a
release before: `activate_release(expected_current=None)` plus
`cli.release_transition.root_service.install_steady` — the same sequence
`scripts/preview/release_cycle_runtime.py::initial` already performs for the
preview's own captured bundle, generalized to a real home and a
real `PreparationReceipt` file. Requires a stopped root
(`cli.commands.lifecycle.root_driver.require_root_absent`) and refuses if another
release is already selected (that is `request` plus the handoff's job).
The selection commits before the boot action installs; if the install fails
(`sudo -n` wanting a password) or the process dies in between, re-running
adopt with the same receipt keeps the selection and finishes the install. A
release operation that holds startup refuses adopt as it refuses `ava start`
(`base.deploy.release.operation.require_start_authorized`): one that activated its
candidate leaves the same pointer, and its boot action is not adopt's to
replace. It holds the home's start-intent and lifecycle locks throughout.

Linux only. The persistent macOS home helper only ever starts inside an
existing release operation (`cli.release_transition.root_macos`); there is no
macOS root-seed-from-image action to adopt onto yet (`macos-release-start`).
A macOS host refuses here by design rather than approximating one.

## `exclude`

```text
exclude --operation OPERATION_ID --unit MACHINE:HOME --reason R
```

A recorded operator decision in the fleet journal, taken under the home
operation lock (so it refuses while a coordinator runs). An included unit is
excluded only while the operation is held; a unit the coordinator marked
`failed` or `unknown` at any time. The unit never returns to the operation:
the coordinator orders it to close and stay closed, it stays stale in the
published release state, and it rejoins only through a converge operation.

## `status`

```text
status [--operation OPERATION_ID] [--json]
```

Read-only. Reports the currently selected release (if any; one that no
longer verifies, a corrupted member or malformed manifest, is reported as
unverifiable with the reason, and the rest still renders), the published
cluster release state (`releases/fleet-state.json`: current release,
last-known-good, stale units, rejections) and, by default, this home's active
operation journal (`$AVA_HOME/updates/active`): the fleet journal's phase,
decisions, verdicts, every unit's inclusion, last instruction and answer, and
alerts with the deliveries that landed and the routes each has not reached,
or a remote unit's instruction and answer. An
explicit `--operation` reads that journal directly. No lock is taken and
nothing is written.
