# Agent-host control capacity and force recovery

The hosted runner now separates turn/checkpoint database work from lifecycle
control. The workload pool remains bounded by concurrent turns plus headroom;
a fixed four-connection pool is reserved for admission, settlement, ownership,
and durable pending-work scans. PgBouncer was not a substitute for this split:
it multiplexes downstream server connections, while the starvation occurred in
the agent-host process before a borrower could reach PgBouncer.

Pool acquire diagnostics now report current size, available connections,
waiting borrowers, and connection-creation errors. This distinguishes a full
client pool from downstream connection failures without increasing the pool as
a guess. Merely raising the workload limit was rejected because it leaves
recovery capacity contending with the failure it must resolve.

An exclusive agent-host boot also settles an applied, unobserved terminate force
left by its predecessor when the target has no persistent disposable-exec
request envelope. The startup path re-locks and revalidates the exact command,
runtime generation, and old owner before observing it and clearing the active
pointer. A surviving envelope keeps the force deferred because host death,
lease expiry, or a new owner cannot prove that an independent process domain
ended. Request and Windows job-gate leftovers are therefore exempt from stale
envelope pruning; successful exact resource settlement remains their normal
removal path. Automatically clearing evidence by age or owner change, and
killing persistent shell sessions wholesale, were rejected for the same reason.
