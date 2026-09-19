# Explicit classification for launchd registrations that are not this unit's

## Context

`_release_inventory` refuses the whole launchd inventory when a `com.ava.*`
registration in the user's single `~/Library/LaunchAgents` namespace does not
declare this unit's home (`AVA_HOME`). A shared Mac made that unusable: five
live registrations carry no `AVA_HOME` (a machine-level helper server, a
caffeinate job, an out-of-band gateway probe, the prod permissions-helper
keeper, a log-audit job). Refusing everything unexplained is the right default
- a silently skipped registration could be a unit writer's relauncher that the
managed-writer closure then never fences - but the machine needs a way for a
registration to declare, explicitly and auditably, that it is not a unit
writer, and for the keeper (whose plist cannot be edited) a recognition rule
with the same standing.

## Decision

An explicit classification channel, declared in the registration's own
`EnvironmentVariables` and recorded in the prepared receipt:

- `AVA_JOB_SCOPE=machine` - the owner declares the registration machine-level;
  it is excluded from the unit's launcher set and recorded as
  `{"label", "definition_digest", "classification": "machine"}`.
- The keeper is recognized automatically by its identity key:
  `AVA_PERMISSIONS_HELPER_SOCKET` under `<home>/run/permissions-helper.`,
  recorded with `classification: "keeper"`. The keeper plist cannot carry a
  scope key under its own rules, so recognition must be structural.
- Exactly one declaration decides a registration: this home's `AVA_HOME` is a
  unit launcher, the two above are recorded exclusions. Everything else
  refuses: no declaration, another home's `AVA_HOME`, a foreign or malformed
  helper socket, conflicting declarations, or an unknown scope value.
- Exclusions are receipt state: they participate in the receipt digest and in
  every revalidation equality; the loaded-label check counts them, so a loaded
  `com.ava.*` job still refuses when it has no classified definition on disk.

## Alternatives rejected

- **A code-side label allowlist.** Reviewable in PRs, but the declaring party
  for these registrations is their owner on the machine, not this repo; every
  new machine job would become a code change and a release, and ownership truth
  would split between the repo and the plist. The plist key mirrors the
  existing `AVA_HOME` ownership declaration instead.
- **A sidecar registry file.** A second source of truth beside the plists, with
  its own copy/sync and staleness failure modes; the declaration belongs where
  the definition lives.
- **Classifying another unit's registrations as exclusions.** Refused
  deliberately: ownership by another home stays an anomaly this inventory
  surfaces rather than swallows - the shared per-user namespace is also how
  dev-cluster residue becomes visible.
- **Recognizing `launchctl disable` as a classification or fence.** Out of
  scope: the managed-writer design declines disable-based evidence, so a
  classification must never be mistaken for one.

## Consequences

- Machine-level registrations become inventory-transparent only after their
  owners add the key; until then the inventory still refuses (unchanged).
- The receipt grows one field (`excluded_registrations`); both revalidation
  paths and the receipt digest bind it, so a classification change between
  prepare and bootstrap refuses like any other fact drift.
- The shared consumer model (`PreparationReceipt`) carries the exclusions as a
  required member - no default. Every receipt a reader legitimately consumes
  is written by that same revision (prepare runs from the verified installed
  image, and the hop revalidates against its own collector's bytes), so a
  receipt without the field is not this revision's receipt and refuses rather
  than passing as silently empty.
- A wrongly declared `machine` scope hides a registration from launcher
  coverage - deliberately explicit and recorded, not silent; the declaration
  sits in the same trust class as `AVA_HOME` ownership.
