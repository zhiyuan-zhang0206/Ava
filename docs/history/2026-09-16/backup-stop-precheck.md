# The update stop refuses while a backup runs

The September 16 wave's second abort (rc=5) was the gateway's local stop meeting
the daily logical backup: its off-site publish runs inside the `pg-backup`
scheduler, the 300-second stop window expired against the still-running job, and
the rollout aborted with the host half-stopped. That night's dispatch was
released only by an operator gate that watched the publish finish.

`ava cluster update`'s gateway leg now replays that gate immediately before its
stop. Two determinations: the scheduler's own `/healthz` `progress` field
(`running <n>s` covers the dump, its encryption, its off-site publish and the
weekly restore drill — none of which a process scan can see), and a live
stand-alone `services.backup --publish-offsite` process publishing this unit's
managed dump. When either is in flight the leg refuses before signalling
anything, returns `RESTART_DECLINED_EXIT_CODE` (nothing was stopped; the host
keeps serving), and the orchestration's compensating resume restores the paused
agent-runners. `AVA_UPDATE_BACKUP_PRECHECK=false` dispatches anyway.

A scheduler that does not answer is reported but not treated as busy — a
disabled or unhealthy daemon must not brick updates. The stop-side change
(backup-worker-shutdown.md) reaps a cancelled job within its own bound; this
gate exists so a planned stop does not cancel one in the first place.

The daemon shutdown line also stops passing its name as the loguru `label`
extra: `shared.log` treats `label` as an event alias, so every daemon stop
emitted an unregistered-event error from inside the handler and lost its row.
