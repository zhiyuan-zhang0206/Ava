# Fleet release core: candidate discovery, resume failures, distinct commits

## Context

FC-7 made every release one fleet operation run by the gateway home's
coordinator (`cli/release_fleet/`): a single box is a fleet of one, and remote
units follow the coordinator over the frozen release handoff and the
authenticated coordinator channel. It left choices open:

- **Candidate discovery.** Before the coordinator can include a remote unit it
  needs that unit's `UnitReceipt` (candidate image, SQL inventory,
  configuration digest, enrollment). The handoff's `receipt` entry can answer
  it, but `release_image_exec` runs a named image, so the gateway must already
  know which candidate the unit prepared. Nothing told it.
- **Failures after admission reopens.** A failing candidate after the fence
  recovers once to its predecessor, but at `resuming` admission may already
  be open, and a unit whose start fails could be retried or left failed.
- **What a release moves between**, and **which runtime builds the request.**
  The request builder reads the home's release store and registered units
  with a database login, which only the currently admitted runtime receives.

## Decision

User rulings, 2026-09-27:

1. **Units publish their prepared receipt to the gateway.** `ava cluster
   release prepare` on each unit publishes its receipt to the gateway over the
   machine-token API, so the coordinator learns each unit's candidate image;
   the request builder then verifies each receipt through that unit's handoff
   (`release_image_exec`, entry `receipt`). This is slice FC-7b. Until it
   lands, the request builder refuses any included remote unit (a unit must be
   excluded or its machine paused). Execution is narrower still: a networked
   release also needs slices dbgen-8 and FC-9, so until both land the
   operation refuses any request that names a unit or an exclusion.
2. **A failure at `resuming` holds for an operator.** The coordinator journals
   the error and a `held` alert and exits; there is no automatic recovery from
   that phase. There is also no automatic retry of a failed unit start for
   now: the unit is marked failed, leaves the operation and is ordered to
   close and stay closed.
3. **A release moves between two distinct commits.** `FleetRequest` refuses a
   candidate with the previous source commit; rebuilding the same commit is
   not a release and goes through the single-host `ava cluster release adopt`
   path. The request builder runs in the currently admitted image for now; it
   moves behind the release handoff in FC-7b.

## Alternatives rejected

- **The operator hands every unit's receipt to `ava cluster release
  request`.** It makes the operator copy files between machines for each
  release, and a stale or wrong receipt is only caught after the fact; a
  published receipt arrives over an authenticated, per-unit channel the
  gateway already trusts.
- **Automatic recovery at `resuming`.** Agents may already be admitted on the
  candidate, and the failure may be the resume step rather than the candidate;
  draining again and switching images under live work without an operator
  risks more than it saves.
- **Automatic unit start retry.** It lengthens the outage window and can hide
  a unit that fails deterministically; a retry policy needs production
  evidence first.
- **Same-commit releases.** Published release state (current release,
  last-known-good, rejections) names a release by its source commit, schema
  and SQL inventory, never by image, so two images of one commit could not be
  told apart; and a rebuild with no source change does not warrant a fenced
  fleet transition and watch window.
- **Building the request in the candidate image now.** The candidate does not
  receive the home's database login before it is admitted; giving it one
  early needs the handoff work planned for FC-7b.

## Consequences

- Nothing networked releases until dbgen-8 (per-unit write-generation
  delivery over the coordinator channel) and FC-9 (converge, the only way a
  unit left out rejoins) have both landed, besides FC-7b. Until then the
  operation refuses any request that names a remote unit or an exclusion, and
  any home with a cluster secret, a data-plane host or another registered
  unit (`cli/release_fleet/inventory.py`, `require_topology` and
  `require_fleet_of_one`): a release is a fleet of one, the gateway home of a
  single-unit cluster.
- A failure at `resuming` leaves the cluster held, possibly with admission
  open, until an operator continues the operation (`ava cluster update
  --prepared`) or intervenes.
- A failed unit start leaves that unit out of the release until it converges.
- Releasing requires a new commit; a same-commit image rebuild uses `adopt`.
  `adopt` today selects only a home's first image (stopped root, no selected
  release, Linux only), so a same-commit rebuild on a home that already runs
  a release has no operator path until `adopt` grows one.
- While the builder runs in the admitted image, the request an older image
  writes must stay readable by the next image's executor.
