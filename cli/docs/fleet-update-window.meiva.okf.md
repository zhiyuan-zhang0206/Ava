---
type: doc
title: Fleet update window guarantees
description: What refuses, gates and surrounds the cli.fleet_update stops — preflight, stored schedules, the keep decision, the silence and the log.
tags: [cli, updates, operations]
---

# Fleet update window guarantees

The promises `cli.fleet_update` makes around the stops themselves
([[cli/docs/fleet-update.meiva.okf.md]] owns the halves and their order): what
refuses the window, what gates it, what it silences, and what must not leak.
The keep decision itself is
[[scripts/host_ops/docs/fleet-update-keep.meiva.okf.md]].

## Preflight

Each listed host is probed in its own shell before anything is touched. The
probe uses `printf` because zsh's and dash's `echo` reinterprets backslashes,
which once mangled a hold record carrying a quoted `$HOME`.

## The stored-schedule gate

A stored schedule script lives in the database, not the checkout: a library
change leaves it crash-looping the moment the schedule manager starts, and
the gateway's own `meiva schedules verify` only exists after that start. So
`down` checks first, the only code that may read the table being the home's
own OLD checkout, and the check being NEW's, offline, under a throwaway home;
both steps are real files whose text ships to the host, so their imports stay
visible.

## The keep decision

The decision runs as a subprocess of the operator's interpreter with a
`--keep-timeout` budget (default 300 seconds: two scratch worktrees, two
whole-repo analyses). Every
failure returns no keeps with one WARNING, and the stops then restart
everything — never a guess.

## The silence

`down` opens one Grafana silence on the gateway host before the first stop
(comment naming NEW, expiry `--silence-hours`, default 4) and `up` expires it
after the drift checks. A silence that cannot open only warns; a failed `up`
leaves the silence to its expiry. `--dry-run` prints the command without
opening.

## The log and the macOS start

Every printed or logged line passes the secret redaction patterns first
(bearer tokens, secret-named assignments, URL passwords), and the log is
created 0600. The macOS one-shot writes its plist under a fresh 0700
directory in the remote `/tmp` and is left loaded past the start deadline,
reporting its label and log path; its label is its own
(`com.meiva.fleet-update.*`), never a registrar's job.

## Contracts

```meiva-contract
id = "preflight-refusals"
kind = "invariant"
statement = "down refuses before touching anything — exit 2, no effects — when a listed host has uncommitted changes, a post-checkout hook, an active update marker or a maintenance hold that is neither free nor a completed stop, when the hosts disagree on HEAD, and when .python-version changes without --allow-python-change; the probe reads its facts through the host's own shell."
code = ["cli/fleet_update.py:_preflight", "cli/fleet_update.py:_host_refusals", "cli/fleet_update.py:PROBE", "cli/fleet_update.py:main"]
tests = ["cli/tests/fleet_update/test_fleet_update.py::test_preflight_refuses", "cli/tests/fleet_update/test_fleet_update.py::test_a_python_change_needs_the_flag", "cli/tests/fleet_update/test_fleet_update.py::test_the_probe_carries_backslashes_in_the_hold_record_unchanged"]
```

```meiva-contract
id = "stored-schedules-refusal"
kind = "invariant"
statement = "Before anything stops — under --dry-run too — down runs NEW's offline verify over the gateway's stored schedule table from a throwaway worktree: the read step runs on the home's OLD checkout by signature dispatch, the check goes through NEW's schedules CLI under a throwaway home, VERIFY_RC separates a crashed check from a red, a host that has not fetched NEW is reported as not checked, and a red or unevaluable table refuses with the rows listed unless --allow-red-schedules."
code = ["cli/fleet_update.py:down", "cli/fleet_update.py:_stored_schedules", "cli/fleet_update.py:PRE_VERIFY", "cli/fleet_update.py:read_rows_py", "cli/fleet_update.py:check_rows_py", "cli/fleet_update.py:VERIFY_SCRIPTS"]
tests = ["cli/tests/fleet_update/test_fleet_update.py::test_stored_schedules_are_checked_against_new_before_anything_stops", "cli/tests/fleet_update/test_fleet_update.py::test_a_red_stored_schedule_refuses_down_before_any_stop", "cli/tests/fleet_update/test_fleet_update.py::test_a_check_that_could_not_run_refuses_like_a_red", "cli/tests/fleet_update/test_fleet_update.py::test_the_dry_run_of_down_reports_a_red_without_changing_anything", "cli/commands/management/tests/test_fleet_update_pre_verify.py::test_the_read_step_runs_under_an_old_checkout_without_cli_database"]
```

```meiva-contract
id = "keep-decision-subprocess"
kind = "invariant"
statement = "down runs the keep decision as a subprocess of scripts/host_ops/fleet_update_keep.py on this interpreter; every failure — missing script, timeout, non-zero exit, unusable output — returns no keeps with one WARNING and the stops restart everything; the verdicts ride every stop as --keep-service flags; --restart-all skips the decision; a rerun over hosts already on NEW passes NEW as both revisions."
code = ["cli/fleet_update.py:keep_decision", "cli/fleet_update.py:down"]
tests = ["cli/tests/fleet_update/test_fleet_update_keep.py::test_the_kept_services_ride_every_stop_command", "cli/tests/fleet_update/test_fleet_update_keep.py::test_the_decision_judges_the_unswitched_head_and_a_rerun_judges_nothing", "cli/tests/fleet_update/test_fleet_update_keep.py::test_a_missing_decision_script_stops_everything", "cli/tests/fleet_update/test_fleet_update_keep.py::test_a_decision_that_cannot_judge_stops_everything", "cli/tests/fleet_update/test_fleet_update_keep.py::test_a_decision_timeout_stops_everything", "cli/tests/fleet_update/test_fleet_update_keep.py::test_restart_all_skips_the_decision"]
```

```meiva-contract
id = "silence-window"
kind = "invariant"
statement = "down opens one Grafana silence on the gateway host before the first stop, its comment naming NEW and its expiry --silence-hours (default 4); up expires it after the drift checks; a silence that cannot open only warns and the half goes on; a failed up leaves the silence to its expiry; --dry-run prints the command without opening."
code = ["cli/fleet_update.py:down", "cli/fleet_update.py:up", "cli/fleet_update.py:_silence", "cli/fleet_update.py:silence_program"]
tests = ["cli/tests/fleet_update/test_fleet_update.py::test_the_window_is_one_silence_opened_before_the_first_stop_and_closed_after_up", "cli/tests/fleet_update/test_fleet_update.py::test_a_failed_up_leaves_the_silence_to_its_expiry", "cli/tests/fleet_update/test_fleet_update.py::test_the_dry_run_lists_the_silence_without_opening_it"]
```

Parent: [[cli/docs/fleet-update.meiva.okf.md]].
