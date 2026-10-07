# Backup jobs own a cancellable process group

The September 16 production update failed its 300-second local-stop deadline
while the daily logical-backup daemon remained alive. It had logged SIGTERM
and `daemon stopped`: the scheduler coroutine had finished, but `asyncio.run`
was still waiting for the executor thread running the synchronous backup.
Cancelling `asyncio.to_thread` cannot interrupt that work. The weekly restore
used the same execution boundary.

Scheduled backup and restore jobs now run in spawned workers. The existing
PITR adoption gate proves process-group ownership before a job can fork; the
controller requires a successful result, zero exit status and no surviving
descendants before recording success. Cancellation sends SIGTERM to the owned
group, allowing synchronous finally blocks to clean partial dumps and keys,
then escalates within a seven-second reap deadline. This fits below the outer
supervisor's ten-second termination grace. A completed encrypted artifact
survives cancellation during its optional off-site publication.

The scheduled restore opts into a foreground throwaway postmaster. pg_ctl
would detach it into another session, defeating group cleanup after a worker
crash. Readiness verifies the expected data directory before executing any DDL,
and the caller owns the child handle before readiness can fail. Unconfirmed
postmaster shutdown retains its data directory and registration. Other
throwaway callers retain their existing pg_ctl behavior.

A hard kill cannot execute Python finally blocks: it may leave private
partials or scratch directories for the existing next-run cleanup. This
change does not delete retained backups, shrink production tables, modify WAL
retention or alter the independently running PITR uploader.

The first rollout still runs the old daemon until it is stopped. Merging this
change does not itself repair an already blocked executor; the operator must
verify the old job's state before attempting that rollout.
