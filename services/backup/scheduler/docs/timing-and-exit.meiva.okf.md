---
type: doc
title: Scheduler timing and exit
description: Current ownership boundaries and their executable evidence.
---

# Scheduler timing and exit

The scheduler calculates due times on the cluster Clock supplied by its boot owner. Failed dumps wait until the next scheduled hour; failure alone does not ask the supervisor for a restart/retry loop. Health evaluates successful-backup freshness after boot grace. The scheduler waits with a single `clock.asleep` call (task #5241): the earlier chunked sleep and its chunk-count test are gone; the count did not prove signal responsiveness, and the bounded-exit test above carries that evidence.

The maintenance pause does not stop direct read-only pg_dump backups: they use the direct URL rather than the pooled business-admission path. SIGTERM raises KeyboardInterrupt and explicit task cancellation drains application cleanup. The Runner is not closed as a context manager because that would wait for wedged default-executor work. Further SIGTERM is ignored during the bounded exit; the process hard-exits after its drain.

The job shutdown tests use disposable trusted children and short Unix socket paths to stay below the macOS AF_UNIX limit. Their parents retain cleanup ownership because SIGKILL bypasses child finally blocks. Only the fixed worker entry changes in the harness; production bootstrap, process group, logs and cancellation remain exercised. Cleanup targets only PIDs recorded by that harness. A request captures resolved paths so later configuration changes cannot redirect restore or parent cleanup. Cross-filesystem publication keeps its private copy open through the exclusive final link, preserving a prior artifact and surviving concurrent sweep; busy backup locks defer instead of waiting.

```meiva-contract
id = "daily-dump-policy"
kind = "invariant"
statement = "A failed dump is reported and waits for the next backup hour, without making health request a restart solely for that failure."
code = ["services/backup/scheduler/daemon.py:<module>"]
tests = ["services/backup/scheduler/tests/test_backup_scheduler_daemon.py::test_failed_backup_is_reported_and_not_retried_before_the_next_backup_hour", "services/backup/scheduler/tests/test_backup_scheduler_daemon.py::test_failed_dump_does_not_make_healthz_ask_the_supervisor_for_a_restart"]
```

```meiva-contract
id = "bounded-backup-exit"
kind = "invariant"
statement = "SIGTERM exits within the shutdown bound even while a default-executor job is wedged."
code = ["services/backup/scheduler/daemon.py:main"]
tests = ["services/backup/scheduler/tests/test_daemon_shutdown.py::test_sigterm_bounded_exit_with_wedged_executor"]
```

Parent: [[services/backup/scheduler/docs/scheduled-workers.meiva.okf.md]].

The shutdown harness launches the running test interpreter with fixed
repository-owned child scripts and explicit argument lists. Temporary paths and
enumerated test modes are data; no shell or caller-selected executable is used.
Real child processes are necessary to exercise signal delivery and process-group
cleanup. The harness parent always retains cleanup ownership if an assertion
fails before the scheduler finishes.

```meiva-contract
id = "shutdown-harness-processes"
kind = "invariant"
statement = "The shutdown harness checks that scheduler SIGTERM reaps its worker and child processes while preserving the previous completed backup."
code = ["services/backup/scheduler/tests/test_scheduler_shutdown.py:_block", "services/backup/scheduler/tests/test_scheduler_shutdown.py:_assert_sigterm_reaps_job_before_scheduler_exits"]
tests = ["services/backup/scheduler/tests/test_scheduler_shutdown.py::test_sigterm_reaps_job_before_scheduler_exits"]
```
