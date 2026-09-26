---
type: doc
title: macOS release root start through the home helper
description: The finite executor starts ava-root on a captured image through the persistent signed home helper, journals which helper and keeper epoch produced which root, and observes it pinned.
tags: [cluster-lifecycle, release, macos]
---

# macOS release root start through the home helper

`root_macos.py` is the macOS root owner of a release operation; the Linux boot
unit is `root_service.py`. `local.py` selects it by the operation's recorded
executor kind (`native.helper_root`), never by probing the host. PITR never
reaches it.

## Ancestry and lifetimes

The ordinary macOS start never spawns root: it persists the image's seed
(`run/ava-root/seed.json`) and asks the home helper's keeper (`root_seed`) to
spawn ava-root as the helper's own direct child in its own session. The finite
executor therefore runs the selected image's `stage --operation` as a bounded
finite tool inside its job group, with the same fixed environment as every
stage action. The root is born outside the job: the job's group closure never
reaches it, and a root stop never reaches the job. `stage.py` accepts this
start only from the recorded executor birth (its parent), in place of the
Linux boot-unit check.

## Helper protocol

Effects use the keeper's existing verbs: `root_stop` persists a `root-stopped`
intent before TERM (no restart, not even at login, until an explicit seed),
`root_seed` never replaces a live root, `root_status` reports state, PID,
unexpected-exit `restarts` and, with `root_seed_report_v1`, the held seed's
argv/cwd/run dir/logs (never its environment). Every boundary authenticates
the helper: kernel socket peer, launchd job of this user, running image valid
with the hardened runtime and satisfying the stable requirement, the
operation's recorded helper artifact, not an ancestor of the executor. Keeper
reads are bracketed by the same peer birth.

## Journal and crash safety

`Operation.root` (`launchd_custody.RootCustody`) is written only while a macOS
release starts. Intent (direction, helper birth, keeper `restarts`) precedes
the stage action; the receipt adds the root birth, and must match the intent's
helper and baseline. A receipt of the current direction is only verified,
never replaced; an earlier intent under the same helper keeps its baseline, so
a keeper restart before the receipt refuses; a new helper birth (reboot,
helper restart) or a recovery direction starts a new intent. The record
survives executor attempts, so a continuation observes instead of starting.
The first start of a direction requires the keeper to hold no root and to be
stopped with intent (or never seeded). The local data plane's births must be
live before and identical after the start action: a data process born inside
the job would sit outside both job and root custody.

Stop authenticates the helper first, uses ordinary stop, then requires the
keeper's durable stop intent. A killed helper leaves its root orphaned; the
restarted keeper sees a held lock and rests in conflict; custody checks and
ordinary stop then refuse, nothing is signalled, and the operation holds.

## Readiness and steady state

Observation runs the selected image's `stage --observe` (full manifest
generation and a fresh readiness round of every selected service), then
requires the journaled root birth live as the helper's kept child with the
same keeper `restarts`: a crash that the keeper silently respawned fails
observation and chooses recovery, like the Linux start action without
restart. The keeper's held seed, `seed.json` and the live root's kernel argv
and working directory must be equal and name the selected image's own
interpreter (`releases/<digest>/...`), never a moving selector. That pinned
seed is the steady state; there is no boot action to install. Recovery stops
the candidate through the keeper and starts the previous image the same way.

## Evidence

Unit tests: `test_root_macos.py`, `test_root_macos_boundaries.py`. Opt-in
native (`AVA_NATIVE_RELEASE_START=1`, `AVA_NATIVE_SIGNED_HELPER=1` for the
stable identity): a disposable helper job and two minimal retained images
whose stage stands in for ordinary start, driving the real keeper, ordinary
root stop, selector CAS, journal and `drive` through A -> B -> A, candidate
start failure, a keeper-respawned candidate, a stage killed mid-start, executor
loss after the effect and a killed helper. The application start itself, the
finite job around the start, the data-plane bracket and logout/reboot are not
exercised natively.
