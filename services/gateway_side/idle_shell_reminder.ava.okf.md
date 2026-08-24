---
type: doc
title: Idle Shell Reminder
description: Gateway-owned reminder loop for agent persistent shells that remain at a prompt.
tags: []
---

# Idle Shell Reminder

## What it is

A gateway-capability daemon that polls this machine's live persistent PTY
sessions every 60 seconds and sends one merged chat inbound per owner when one
or more shells have remained at a prompt beyond their current threshold. It is
config-gated by `AVA_IDLE_SHELL_REMINDER_ENABLED` and never closes a session.

## Core responsibilities

- **Truthful idle detection**: the PTY host reports idle only when the tty's
  foreground process group is the interactive shell's own group. A foreground
  job is busy; background jobs do not block a prompt and therefore remain idle.
- **Output-anchored periods**: the host's last-output monotonic timestamp is
  converted to epoch at observation. Busy observations and a changed output
  timestamp start a new idle period.
- **Persistent backoff**: reminders wait 5m, then 30m/1h/2h/4h/8h/16h/24h
  after the previous delivery, and remain on a 24-hour cadence. State survives
  daemon restarts in `$AVA_HOME/run/idle_shell_reminder.json`; sessions absent
  from the live PTY roster are removed.
- **Owner merge and standing exemption**: all sessions due for one owner in a
  tick share one inbound id. An AI reply containing `保留` after that inbound
  exempts every covered session until it ends.
- **Daemon-owned page sessions**: shells whose suffix starts `page-` run page
  servers and are excluded from idle reminders.

## Entry points

- `services/idle_shell_reminder/daemon.py` — lifecycle and I/O loop
- `services/idle_shell_reminder/engine.py` — pure reminder state transitions
- `services/idle_shell_reminder/state.py` — versioned atomic JSON persistence
- `services/healthchecks/idle_shell_reminder.py` — identity-verified liveness
  keepalive

Parent: [[gateway_side.ava.okf.md|Gateway-side services]].
