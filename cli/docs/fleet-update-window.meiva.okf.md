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

down refuses before touching anything — exit 2, no effects — when a listed host has uncommitted changes, a post-checkout hook, an active update marker or a maintenance hold that is neither free nor a completed stop, when the hosts disagree on HEAD, and when .python-version changes without --allow-python-change; the probe reads its facts through the host's own shell.

Before anything stops — under --dry-run too — down runs NEW's offline verify over the gateway's stored schedule table from a throwaway worktree: the read step runs on the home's OLD checkout by signature dispatch, the check goes through NEW's schedules CLI under a throwaway home, VERIFY_RC separates a crashed check from a red, a host that has not fetched NEW is reported as not checked, and a red or unevaluable table refuses with the rows listed unless --allow-red-schedules.

down runs the keep decision as a subprocess of scripts/host_ops/fleet_update_keep.py on this interpreter; every failure — missing script, timeout, non-zero exit, unusable output — returns no keeps with one WARNING and the stops restart everything; the verdicts ride every stop as --keep-service flags; --restart-all skips the decision; a rerun over hosts already on NEW passes NEW as both revisions.

down opens one Grafana silence on the gateway host before the first stop, its comment naming NEW and its expiry --silence-hours (default 4); up expires it after the drift checks; a silence that cannot open only warns and the half goes on; a failed up leaves the silence to its expiry; --dry-run prints the command without opening.

Parent: [[cli/docs/fleet-update.meiva.okf.md]].
