# Hosted lifecycle decisions belong to safe settlement

Graph booleans are return signals, not durable authority. After graph execution
returns under existing single-flight, the host reads the accepted command
pointer and verifies its target and fresh ownership. Restart applies its
decision and releases ownership atomically. A real successor admission observes
that restart before claiming further work. Termination is observed only after
the safe graph return and actual cache drop.

This replaces the host's separate exit-notify writer and the ordinary turn
settlement's `release=True` branch. Ordinary settlement cannot release an
incarnation. No new poller is introduced: the existing PostgreSQL backstop now
also wakes an idling agent with an accepted pointer and no pending rows, which
otherwise vanishes from recovery after a crash between claim and effect.

A replacement cannot execute an old un-applied target. It records the old
request as superseded and then serially accepts the next request. Unknown
ownership is not replacement proof. Applied effects retain their targets.

Remaining activation work includes host-process death between cache drop and
observation, external process-exit proof, old-writer shutdown barriers and
legacy service fakes that bypass admission. CI's blocked graph test proves the
safe return boundary with real PG, not all possible plugin thread behaviors;
the existing single-flight cancellation tests remain required. No production
deployment or new protocol advertisement is part of this change.
