# A PTY closure requires a complete PID census

## Context

The PTY closure captures birth-identified session membership, HUPs its shells,
TERMs its other members, and grants a bounded grace before freezing and killing
what remains. Its grace ended after two immediate scans read no live member.
The scan enumerated PIDs once, then read each session id. A fork-and-exit hop
could disappear between those operations, with its successor born outside the
list. Two negative non-atomic snapshots could miss a continuously live chain.

A real PTY service probe using the existing 8 ms / 40-hop job demonstrated the
escape at revision `120bb56b2`. A diagnostic 20 ms scheduling delay after PID
enumeration made `close_all` report no survivors in 0.1282 s; the last hop still
ran in the original shell's session after the job's own run had elapsed. The
undelayed control took 2.0913 s and reclaimed the whole chain. The isolated
probe services and leftover processes were cleaned up.

This demonstrates a scheduling defect, not the exact cause of the earlier
Linux [shard failure](https://github.com/zhiyuan-zhang0206/Ava/actions/runs/37419239352/job/112125566078),
whose preserved trace lacks the outcome and session scan history.

## Decision

After reading session ids, enumerate PIDs again and read newcomers. Repeat
until a final census contains no unread PID. A real fork/reap handoff between
the first enumeration and the session-id reads is now locked by a regression:
the old leader is reaped and its live successor must appear in the scan.

Bound a census to 32 passes. The process table is global, so sustained unrelated
process creation can also prevent a complete census. An incomplete census
keeps the grace busy; the freeze leg fails through the existing request error
channel instead of declaring that its membership was closed. Its existing
exception cleanup still ends frozen members. No new wire outcome is added.

Keep the existing captured-birth checks, session-id proof, sovereign-process
exclusion, HUP-before-TERM ordering, grace/kill windows and normal freeze-pass
behavior. A complete census does not extend an expired ownership proof. The
known behavior for unproven session members remains an independent open item
in `future/tech-debt/ledger.md`.

## Alternatives

More empty polls or an added sleep would merely reduce a timing window. A census
must account for births during its reads. An unbounded census would make a stop
unbounded on a host with persistent process churn. A new unverified-session wire
outcome could retain partial results, but would require a separate contract for
operator completion and owner notices; this repair uses the current error path.
