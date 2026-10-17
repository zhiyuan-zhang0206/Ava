---
type: doc
title: Timestamp display
description: The shared timestamp formatters and the day-context rules for an upcoming instant.
tags: [frontend, contracts]
---

# Timestamp display

`time.ts` is the single owner of turning an ISO instant into text a person
reads. Views see browser-local time — the viewer's zone, deliberately unlike the
cluster timezone an agent sees. Four shapes, one job each: `formatAbsolute`
(full precision with a zone), `formatShort` (compact rows), `formatRelative`
(one signed wording, `in 4m` / `5m ago`) and `formatUpcoming` (an upcoming
instant with its day context).

**Why day context.** A clock reading alone cannot say which day it means. The
Inspector's "Next heartbeat" showed `3:21 PM` while the clock read 4:26 PM: the
value was tomorrow's, but it read as a time that had already passed (user
report 2026-10-17). `formatUpcoming` therefore renders an upcoming instant as a
clock time plus the minimum day context, and never as a bare clock for a future
day:

| Calendar days from now | Rendering                       | Example                |
| ---------------------- | ------------------------------- | ---------------------- |
| later today            | clock                           | `3:21 PM`              |
| tomorrow               | `Tomorrow` + clock              | `Tomorrow 3:21 PM`     |
| 2–6                    | weekday + clock                 | `Tue 3:21 PM`          |
| 7–29                   | day count + date                | `In 7 days (Oct 24)`   |
| 30 and beyond          | date, year only when it differs | `Nov 5`, `Jan 5, 2027` |
| already past           | the shared relative wording     | `5m ago`               |

**Boundaries.** The day changes at the viewer's local midnight, not after 24
elapsed hours: a target 20 minutes away at 00:01 is "Tomorrow". The day count is
calendar arithmetic over the local date fields, so a DST day (23 or 25 hours)
still counts as exactly one day. Seven days is the first day of the day-count
tier; 30 days is the first day of the date tier, because beyond roughly a month
a day count is harder to place than the date itself. The weekday tier stops at
six days — a seventh day repeats the weekday name, so the name would stop
distinguishing anything.

**Rendering details.** A date carries its year only when it falls in another
calendar year, so a target twelve months out cannot repeat the same ambiguity.
The clock is 12-hour (`3:21 PM`) and replaces ICU's narrow no-break space before
the day period with a plain space, so the string is identical across Node/ICU
versions and copies cleanly.

```meiva-contract
id = "day-context"
kind = "invariant"
statement = "formatUpcoming renders an upcoming instant as a 12-hour clock time plus the day context its viewer-local calendar date requires, and never as a bare clock time for a future day: clock only later today, 'Tomorrow' on the next local date, a weekday name from two to six days, a day count with the date from seven to 29 days, and the date alone from 30 days. The boundary is local midnight, not 24 elapsed hours, so a DST day still counts as one day. A past instant keeps the shared relative wording and an unparseable input yields an em dash."
code = ["ui/web/src/lib/format/time.ts:formatUpcoming"]
tests = [
  "ui/web/src/lib/format/time.test.ts::formatUpcoming day context::labels tomorrow instead of showing a clock time alone (the reported case)",
  "ui/web/src/lib/format/time.test.ts::formatUpcoming day context::calls 00:01 tomorrow when the local date changes, 20 minutes from now",
  "ui/web/src/lib/format/time.test.ts::formatUpcoming day context::keeps today's clock for a target 23h59m away that stays on the same date",
  "ui/web/src/lib/format/time.test.ts::formatUpcoming day context::switches to a dated countdown at exactly seven days",
  "ui/web/src/lib/format/time.test.ts::formatUpcoming day context::renders a date at exactly one month (30 days)",
  "ui/web/src/lib/format/time.test.ts::formatUpcoming day context::counts a spring-forward day as one calendar day, not 24 hours",
  "ui/web/src/lib/format/time.test.ts::formatUpcoming input handling::keeps the existing relative wording for a past instant",
]
```

The Inspector's Liveness cells are the first consumer; see
[[ui/web/src/docs/frontend-components/inspector/section-details.meiva.okf.md|Inspector section details]].
