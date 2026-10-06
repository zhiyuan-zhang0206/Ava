# PTY closure is best effort

## Decision

A PTY session owns its known shell and terminal. Closing it uses bounded
signals to known shell and foreground process groups and birth-identified
processes. It closes the terminal and verifies the known shell has stopped.
It does not certify that every descendant, background job or detached process
has disappeared.

Do not enumerate the host process table, walk descendants, freeze processes
with SIGSTOP, infer membership from a recently observed session id, or maintain
periodic member snapshots to manufacture that guarantee. Preserve shell birth
checks, allocation fencing, isolated development homes and deployment authority.
Signal-operation failures raise; a surviving known shell remains a failed
closure. Known job leftovers are diagnostics and do not themselves gate
`ava stop`. `Outcome.closed` and `Outcome.survivors` retain their wire shapes;
`survivors` describes only known identities observed alive, not a complete
inventory of the host.

## Rationale

The user chose a smaller, truthful terminal contract over increasingly complex
process-tree disappearance proofs. Shell job control and daemonization do not
provide a durable cross-platform ownership boundary. Linux cgroup containment
would require a separately designed launch/delegation and service-custody
contract; macOS has no equivalent simple primitive in the current design.
Neither is introduced by this decision.

Residual host processes and OS stalls belong to agent/user operational
investigation. Inspect identity, ownership and current work before taking an
explicit action. Do not automatically kill every host process, reboot, or turn
an unexplained incident into a larger cleanup mechanism. Passing a rerun is not
evidence that an earlier stall or race was repaired.

## Consequences and supersession

Owner notices mean that the shell/terminal closed and work was interrupted or
may have been interrupted. They never mean all processes were killed. A crash
ledger records known shell identities for bounded cleanup and notices; it is
not a periodically refreshed descendant census.

This supersedes the PTY whole-session membership/proof requirements in
[session-id proven by a live member](2026-09-28-session-id-proven-by-a-live-member.md)
and the all-process disappearance requirement of
[stop escalates to SIGKILL](2026-09-28-stop-escalates-to-sigkill.md).
Those historical records remain unchanged. The bounded signal escalation remains;
other service and native data-plane shutdown contracts are unchanged. The
[PTY service decision](2026-10-03-pty-sessions-service.md) still owns service
placement, while this record owns its closure guarantee.
