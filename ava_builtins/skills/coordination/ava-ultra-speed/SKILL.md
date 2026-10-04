---
name: ava-ultra-speed
description: Keeps ultra-speed workers reporting early, avoiding silent waits, and terminating cleanly. Use whenever the agent runs the `ultra-speed-worker` preset, even if the assigned task is brief.
---

# Ultra Speed

## Report as you go

Anything worth the spawner or user knowing, say it now and keep working —
don't hold it for a final wrap-up.

## Never wait silently

No single poll or wait longer than 15 seconds. Prefer another short round
over one long silent one.

## Finish means end your own process

When the mission is done, report it and end your own process. Never take
extra rounds just to stay warm — a message resurrects a finished worker
instantly with its context intact, so ending yourself is the fastest path to
the next piece of work. The rules above govern you *while a mission is
running*; they do not license idling after it.
