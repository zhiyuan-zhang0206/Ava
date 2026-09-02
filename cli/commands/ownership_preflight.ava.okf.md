---
type: doc
title: Converge Ownership Preflight
description: Warn about non-user-owned $AVA_HOME paths before later converge writes fail unclearly.
tags:
- cli
- converge
---

# Converge Ownership Preflight

`cli/commands/_ownership_preflight.py` runs immediately after the `$AVA_HOME`
directory skeleton converge step. On POSIX it compares `Path.stat().st_uid` to
the current user for the home directory, `.env`, `logs/`, `configs/`,
`secrets/`, and the source tree when it exists.

Findings are warning-only: converge and start continue, while the console and
`$AVA_HOME/logs/ownership_preflight.log` name each foreign-owned path and its
exact `sudo chown -R <user>:<group> <path>` repair command. It intentionally
does not chown automatically. If the root-owned log directory itself prevents
the log write, the console warning remains and explains that the log is
unavailable. Non-POSIX backends skip the check.
