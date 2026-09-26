# Numeric limits: config by default, exceptions marked

How this repo treats a number that bounds behavior — a limit, cap, size, count,
or window. Read before adding or reviewing one.

**Scope.** The rule targets numbers that bound *how many / how far / how big* —
where a reader will ask "why this value?" and an operator may want to move it.
Numbers that are not limits (slices like `[1:]`, format widths, arithmetic) are
out of scope.

## The default: config, one declaration, reason in the description

A user- or operator-tunable value is a field under `shared/config/` (declared
once, per the registry doctrine in `shared/config_registry.py`), with:

- `description` stating **why the default is that number** and what changing it
  does — the reason lives with the value, not in a doc beside it;
- `restart_required` naming the process kind that must restart;
- consumers that resolve the value **at call time**, and a parity invariant: an
  unconfigured cluster behaves exactly as before the field existed.

The display domain (`shared/config/display.py`, task #3696) is the reference
shape: user-facing windows and page defaults, each with its reasoning inline.

## The exception: a literal that stays

Some numbers must stay literals. An exception is permitted only when the number
is fixed by something outside the tuning surface — and then it carries a comment
saying **why it cannot be config, and what fixes it** (the spec, the upstream
constraint, or the internal invariant), with the marker string
`task #3696 exception inventory`.

The marker *is* the inventory — there is no second list to keep in sync:

```text
git grep -n 'exception inventory'
```

The permitted exception classes, with real sites:

| Class | Why it stays | Examples |
|---|---|---|
| Protocol / format specs | the number is the format; changing it breaks the wire | msgpack ext-header bytes (`shared/agents/history/checkpoint.py`); short-SHA display width 7 (`shared/source_tree_guard.py`); page-registration name/host/path bounds (`gateway/schemas/pages.py`) |
| External platform caps | the platform dictates it; any other value fails | Telegram caption 1024 (`ava_builtins/skills/telegram-send-file/scripts/send_file.py`); Baidu PCS SVIP single-file size (`services/pitr/baidu_pcs.py`) |
| Self-imposed transport / payload guards | bounds one request or read so a single call cannot park unbounded bytes | message content ceiling (`gateway/routers/agents_state.py` — self-imposed, not an external protocol limit); attach ceilings (`shared/lm/attach_constants.py`); pty capture clamp (`shared/sessions/pty/_paths.py`); events `le=1000` / `offset` 10 000 (`gateway/routers/events.py`) |
| Protective security / resource bounds | memory or abuse guard, not a tuning knob | login limiter's tracked-IP cap (`shared/rate_limit.py`); backup activation slots (`services/backup.py`); labeler poll batch (`services/labeler/daemon.py`); impersonation maintenance quantities (`shared/agents/impersonation/impersonation_maintenance.py`) |
| UI micro-details | rendering density or truncation; a design decision per surface | run-timeline event rail (`ui/web/src/components/run-timeline/run-timeline-chart.tsx`); memory-graph label truncation (`ui/web/src/app/memory/graph/page.tsx`) |
| Reference-script / tool defaults | these scripts run standalone; their knobs ARE their parameters — they never read cluster config | watcher wake-delivery retry contract — one template + five skill-reference copies (`.agents/skills/ship-a-change/reference/ci_watcher.py`, `ava_builtins/skills/ava-dynamic-workflow/reference/gather_files.py`, `ava_builtins/skills/ava-goal/reference/watch_idle.py`, `ava_builtins/skills/ava-watcher/reference/watch_idle.py`, `ava_builtins/skills/ava-use-other-agents/reference/watch_work.py`, `ava_builtins/plugins/ava_fleet/skills/ava-fleet/reference/watch_idle.py`); skill script defaults such as the gmail/sms query pages (`ava_builtins/skills/`) |

A reference-script default follows the same discipline one level down: it is a
parameter or a documented constant with a comment stating why that number.
Why not cluster config: these scripts are reference code — read, copied, and run
standalone, sometimes outside the cluster they were written on — so the knob must
travel with the copy and be visible in the file, not live in cluster state the
script would have to fetch.

## The review action

A naked limit/cap/number in a diff — no config, no marker, no source — is a
request-changes. Ask the three limit questions:

1. **Why this value?** The reason must be in the code (field `description` or the
   marked comment): capacity math, experience, upstream constraint, measured
   distribution. "It looked round" is not a reason.
2. **Can it be tuned?** If yes and it is user-visible: it is config. If no: say
   what fixes it, and mark it.
3. **Who aligns it?** The same semantic on two ends (gateway / CLI / frontend /
   host) converges on one key or one constant with a single source — never two
   copies kept in sync by hand.

Do not add a switch "for symmetry" to a number no operator should move; an
exception with a stated source beats a knob nobody may touch.
