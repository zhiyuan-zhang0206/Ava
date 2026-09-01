---
type: doc
title: Episode-gated reporting — an ERROR once per failure episode, not per round
description: A persistent failure condition (a terminal port occupant, a heal that keeps failing) used to log a fresh ERROR every watchdog round — 1.8k lines/day across two hosts in the 2026-08-12 browser storm. The browser healthcheck now episode-gates its own ERROR lines, the watchdog de-duplicates its failure line per (check, exit code), and nothing here can suppress a heal.
tags:
- ops
---

# Episode-gated reporting — an ERROR once per failure episode, not per round

## What broke

"Report at ERROR every round" ([[services/healthchecks/terminal-verdict/terminal-verdict.ava.okf.md]]'s loud-and-stop policy) proved to be a noise machine once a terminal condition could last for hours. Measured on 2026-08-12:

- **machine-1, 1,094 ERRORs/day**: the unit's own Chrome held CDP 9222 while the `ava-browser` session was gone; the healthcheck named an operator remedy every 60s but never performed it, so the same ERROR repeated for days.
- **win, ~453+123+123 lines/day**: a genuinely foreign Chrome held CDP 9222 for ~6h (2026-08-11 21:14 → 03:13, the machine slept) and every ~67s round produced one healthcheck ERROR plus one watchdog exit line — ~640 lines of pure repetition.

The user ruled: error lines fire on **state change**, not per probe.

## The split

**The browser healthcheck** (`services/healthchecks/browser.py`) episode-gates its own reporting. A failure episode is keyed by a coarse condition class (`terminal` / `orphan-heal-failed` / `respawn-failed` / `waiting-for-macos-readiness`); the first round of an episode, each condition change, and one reminder per `_EPISODE_REMINDER_S` (6h) report at ERROR, except a deliberate macOS readiness wait which reports at WARNING. Quiet rounds log the same fact at DEBUG, so the condition stays visible without re-alarming. A healthy round deletes the record and logs one INFO recovery line.

The episode record lives at `$AVA_HOME/run/healthcheck-state/browser.json`. Three properties are load-bearing:

1. **It gates only REPORTING.** It can never suppress a reap or a respawn — the action path is unconditional, and the exit codes are unchanged (a quiet terminal round still exits `EXIT_PORT_TAKEN`).
2. **It fails open.** An unreadable/corrupt record reports as a new episode, never as silence.
3. **It self-clears.** The verdict itself persists nothing ([[services/healthchecks/terminal-verdict/terminal-verdict.ava.okf.md]]'s "no state" rule); the record only exists while an episode is open.

**The watchdog** (`services/watchdog/daemon.py:_run_check`) de-duplicates its own "healthcheck X reported failure (exit N)" line per `(check, exit code)`: a repeated exit with the same code logs once (DEBUG on quiet rounds), and the first successful round resets the memory so the next failure is a new first sight. This keeps every OTHER healthcheck's per-round ERROR contract intact while the duplicate line stops repeating.

## What stays loud

Nothing here revives the silence the terminal verdict was built against: the quiet round still carries the condition (DEBUG, full detail), and the reminder window bounds how long the alerting surface goes without a fresh ERROR. The episode record can also never hide a condition change — each new class or each recovery-to-failure transition is a fresh first sight.
