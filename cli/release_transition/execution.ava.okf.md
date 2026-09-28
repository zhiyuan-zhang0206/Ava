---
type: doc
title: Release transition execution
description: Phase-by-phase release effects, recovery direction, root start through the platform's persistent root owner and fresh readiness before resume.
tags: [cluster-lifecycle, release]
---

# Release transition execution

`execute.py` runs a fleet operation's coordinator
([[cli/release_fleet/coordinator.ava.okf.md]]; a single box is a fleet of one),
a remote unit's follower, or PITR's own phases. The coordinator advances
prepared, dispatching, quiescing, stopping, fencing, selecting, authorizing,
starting, observing, starting_units, resuming, watching and complete; each
phase reconciles actual state. A failure before the fence aborts (`restoring`:
the unchanged previous image on the unchanged generation). A candidate failure
at start, observation, the start barrier or the watch window chooses the
captured predecessor once, closes the candidate, then uses the same
fence/select/authorize/start/observe path. Recovery never chooses another
target or loops between releases. A failure while fencing, selecting,
authorizing, resuming or restoring holds for continuation: none is an
automatic rollback boundary.

The stop phase closes this unit's writers. Persistent terminals — agent
shells, coding sessions, watchers, page and schedule runners — do not survive
a release ([decision](../../decisions/2026-09-27-fleet-release-and-cutover-policies.md)
item 2). While root still serves them, the phase waits the captured policy's
`close_s` for their jobs to finish, signalling nothing. It then stops root, keeping
terminals, so no reconciler re-arms a session. `close_release_terminals`
(`cli/commands/service_stop.py`) captures every recorded shell, every other
member of its session (`shared/sessions/pty/session_tree.py`: descendants and
POSIX session, a double-forked job included) and each PTY host birth, records
the `ava stop` closure notice for each busy session's owner (naming the
release, before any signal), HUPs shells and TERMs jobs, and after the
policy's `cancel_grace_s` kills each session whole through `session_tree`
(frozen, children first, shell last). A session whose shell the kill ended is
recorded again under the same dedup key, naming whatever of it outlived its
SIGKILL, so its owner still gets one notice. A PTY host still running after
its session is gone gets SIGKILL to its captured birth. This is the same closure
`ava stop` runs, with the release's bounds. Closure is
the kernel observation that each is gone and no recorded terminal is live;
selection checks that evidence again. A survivor fails the phase with its
identity. After start, the schedule manager re-arms schedules and the page
server re-arms open pages; other sessions stay closed, and their owners learn
it from the notice.

`local.py` owns those release effects and selects the native root owner by
the operation's recorded executor kind. On Linux `root_service.py` uses the
existing home boot unit, never a second application supervisor. Its
one-operation start action runs `stage.py` in that unit; the new root therefore
survives the finite executor's exit. On macOS the persistent home helper births
root: [[cli/release_transition/root_macos.ava.okf.md]]. Readiness checks every
selected service plus native root identity. Resuming checks fresh readiness
again, including after an interrupted executor or a previously written resume
receipt. A durable serving marker is not a substitute for a live observation.

`stage.py` admits the captured image and uses ordinary `run_start`. Release
startup does not mutate an editable checkout, install dependencies, migrate, or
silently prepare external assets. After observation, `boot.py` becomes the
steady pinned-image boot action on Linux; on macOS the keeper's pinned seed is. `shared/release_operation.py` blocks unrelated
ordinary startup while an operation remains incomplete. Only the exact current
starting journal revision grants the in-process start capability; child
processes do not inherit it.

## Write generations

Every direction runs on a fresh database write generation
([[shared/cluster/authority/authority.ava.okf.md|write-generation authority]]);
`authority.py` orchestrates it and `cli/commands/data_plane/write_generation.py`
performs the data-plane effects. The home ledger is the authority; the journal
(`authority_evidence.py`) carries one `Fence` and one `Issue` per direction:
intent before each ledger transition, a non-secret receipt after it.

- **prepared** also checks, read-only, that exactly one admitted generation
  exists: ledger active with nothing pending or unclosed, the catalog
  invariant for it, no prepared transaction, and a pooler userlist serving
  exactly it.
- **fencing** (root absent): `Fence(revoking)` names the ledger's active
  generation (for a recovery, exactly the candidate's issue), then revoke and
  the NOLOGIN sweep, the owned pooler stopped (escalated to a kill when the
  safe shutdown cannot finish; no listener may remain), termination and census
  until no stale session or prepared transaction survives (ledger `closed`,
  secret deleted), and a prune without CASCADE; `Fence(closed)` records the
  census, the pooler outcome and the ledger's drop outcome. A census failure
  holds, never closes.
- **authorizing** (target selected, root absent): `Issue(minting)` records the
  number the ledger will allocate, then mint (secret, `pending`, the two
  LOGIN roles), a fresh pooler serving exactly the pair, a pooled `SELECT 1` as
  each login, ledger `active`, and `Issue(authorized)` with its credential
  digest. A retry reconciles the recorded number exactly or holds; a foreign
  pending generation or another allocation refuses before any effect.
- **starting**: the stage refuses unless the ledger's active generation is
  this direction's authorized issue, so the launch delivers and binds only it.
- **restoring** (an abort): nothing was fenced or minted; the stage refuses
  unless the active generation is still the one `prepared` recorded.
- **observing / resuming**: after readiness the stage proves the issued
  generation is active, the invariant holds, no fenced session survives, the
  pooler serves only that pair, and both logins answer.

The finite executor itself runs the candidate image, which the boot pass never
admits to a generation. It adopts, in-process, the OS-user administrator over
the owner-only socket acting as `ava_gateway` (`peer`, startup
`-c role=ava_gateway`, which `RESET ALL` keeps): no fence census includes its
session. PITR operations and aborts carry neither record and reuse the active
generation; a remote unit receives its generation over the coordinator channel
(slice dbgen-8). Networked fleets refuse before any effect
(`cli/release_fleet/inventory.py`).
