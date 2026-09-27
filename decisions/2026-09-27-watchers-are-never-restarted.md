# Watchers are never restarted, and carry no database record at all

## Context

`ava.watcher.at/cron/launch` spawns a watcher as a PTY shell session running
a generated script. Since issue #1014 (the fix landed as R1's `agent_watchers`
registry, task #1021), every spawn wrote a row recording "this watcher should
exist"; the agent's boot reconcile (`ava.watcher.reconcile`, called from
`agent/_process_boot.py`) read that registry on every runtime build and
rebuilt any row whose session had died — re-spawning a live cron from its
stored expression, or marking a missed one-shot and alerting its owner.

That mechanism grew a long bug history on top of the original fix: #1726
(killed pty hosts left orphaned watcher processes still firing — fixed by an
external SIGKILL sweep before rebuild), #1825 (a kill/rebuild race stacked a
second live cron on the same schedule), #1858 (a delivered wake was
double-reported as "missed" because the clean-exit delete raced the
reconcile), #2061 (an explicit-end re-registration failed to supersede a
standing twin, so the documented "renew with a longer end" flow double-fired
for days), #2617 (a terminated owner's cron kept firing forever unless a
separate gateway-side reclaimer also knew about the registry), #3411 (the
registry's own notion of a watcher's deadline had to be unified with the
general shell TTL to stop the boot reconcile and the TTL reaper from
disagreeing). Each fix added another special case to a second, parallel
"is this session supposed to exist, and did it restart correctly"
mechanism — on top of the one mechanism that already exists for this exact
question: the agent itself, on its next turn, deciding what to do.

Two separate problems were tangled in the old design:

1. **Should a dead watcher be automatically re-spawned?** A watcher script is
   agent-written and not guaranteed idempotent — it may do one-time setup, or
   be interrupted mid-way inside a `try`/`except` with side effects already
   applied. Blindly re-running it after an unclean end can be unsafe. The
   agent already has an LLM as its universal fallback for "something happened
   while you weren't looking, decide what to do" — a second, code-level
   recovery mechanism duplicates that judgment with none of its flexibility.
2. **Should a watcher have a database presence distinct from its shell
   session at all?** The registry existed so the reconcile technically had a
   "what am I rebuilding, and from what payload" ledger. Once rebuilding is
   off the table, a watcher is *by definition* nothing but an
   `ava.shell.sessions` session running a generated script — the same object
   the SDK already lists, captures, renews, and kills. Keeping a parallel
   ledger for it is dead weight the moment it stops being read for a rebuild.

## Decision

1. **Nothing ever re-spawns a watcher.** `ava.watcher.reconcile` and the boot
   reconcile that called it (`agent/_process_boot.py`, wired from
   `services/agent_host/host.py` / `daemon.py`) are deleted outright, along
   with the host-boot forced-wake that existed only to drive that reconcile
   (`AgentHost.watcher_boot_wakes`, `_watcher_recovery_pending`,
   `_schedule_watcher_recovery`). A watcher whose session ends for any
   reason — its pty host crashed, `ava stop`, a release closing terminals, a
   machine reboot, an update — simply stays gone. The agent decides on its
   own whether and how to re-create it, the next time it has reason to look.
2. **A watcher carries no database record.** `shared/daemon/schedules/watcher_registry.py`
   (the `agent_watchers` table's API) and `gateway/watcher_ttl.py` (its
   TTL-reclaim and terminated-owner-reclaim helpers) are deleted, along with
   every write into `agent_watchers`: `ava.watcher._spawn`'s registration
   (`register_watcher` / `register_cron_atomic` / `register_cron_renewal`),
   the generated bootstrap's clean-exit row delete, and
   `ava.shell.sessions.kill` / `kill_all`'s row delete. A watcher is now
   exactly what it always structurally was — a shell session with a
   generated script and a TTL — and nothing more. Registration-time cron
   twin dedupe/supersede (#1825's atomic reuse, #2061's twin-supersede) goes
   with it: calling `ava.watcher.cron()` again with the same expression no
   longer replaces or renews anything, it starts a second, independent
   session. If an agent wants exactly one live copy of a schedule, it kills
   the old session itself (`ava.shell.sessions.kill`) — visible and listable
   through the same `ava.shell.sessions` surface as any other session, with
   `renew()` now applying to a watcher's TTL exactly as it would to a plain
   shell's.
3. **A watcher's session is reclaimed by the same TTL path as any other
   shell — no special case.** `gateway/ttl_reaper.py`'s `LEFT JOIN
   agent_watchers` and its watcher-specific deadline validation, notify
   suppression, and registry-status write are removed; an expired watcher
   session is killed and its `agent_shell_ttls` row dropped exactly like any
   other TTL-expired shell. The one behavior change this produces: a
   reclaimed watcher session now gets the ordinary shell-reclaimed
   interruption notice (it previously suppressed that notice in favor of a
   registry-status flip nobody reads any more) — and since a watcher always
   has a running job when its TTL fires, that notice fires every time.
4. **Four ends, four outcomes — no fifth path drops a watcher silently, but
   two of the four already carry no message.**
   - *The watcher exits on its own* (script finished, one-shot fired, its own
     timeout watchdog): the shell layer's existing completion notice fires,
     respecting the `notify` policy, and the session (with no row to delete
     any more) simply closes.
   - *The owner agent kills it itself* (`ava.shell.sessions.kill` /
     `kill_all`): no extra message — the call already returned its result to
     the same turn that made the decision.
   - *The platform reclaims it*: at its TTL deadline (the gateway TTL
     reaper, point 3 above), or a **normal** `ava stop` / update force-closes
     a busy terminal (`cli/commands/_temporary_stop.py`'s existing durable
     closure notice for a busy session, unchanged and unaffected by this PR
     — verified nothing watcher-specific ever excluded a watcher session from
     it). Either of these sends a message.
   - *The pty host is killed with no orchestrated closer* — an external
     SIGKILL, a machine power loss, a crash nothing durably recorded, but
     also `ava stop --force` (`cli/commands/stop.py:_stop_terminals_force`,
     which kills every session directly with no notice path at all) and a
     Windows unit's stop (`cli/commands/_temporary_stop.py`'s Windows branch
     returns before the notice-capture step): **no message in any of
     these.** The session is simply absent from `ava.shell.sessions.list()`
     on the agent's next check. This is an accepted gap, not a deferred
     feature — see Alternatives rejected.
5. **The generated bootstrap's orphan guard and timeout watchdog are
   unchanged.** They are not about recovery — the orphan guard makes a
   watcher child self-terminate within seconds of its pty host dying (so a
   host-death path can never leave a watcher firing forever unsupervised, a
   concern the earlier design also carried), and the watchdog is the
   watcher's own bounded-lifetime contract. Both operate entirely inside the
   child process, independent of any registry.

## Alternatives rejected

- **Keep the registry, drop only the rebuild** (a "loss observer" that marks
  a dead `running` row `lost` and sends one notice, without ever
  re-spawning) — this was the first shape of this change. Rejected once it
  became clear the registry's only remaining reader would be that one
  observer: keeping a whole table, its writers, and its status lifecycle
  alive purely to feed a single "tell the owner" pass is exactly the kind of
  parallel bookkeeping this change exists to remove. A watcher session's
  destruction reaching its owner as a message is a fact about *sessions*,
  not about *watchers* — it belongs at the session/TTL layer (point 3),
  where it now lives, not in a bespoke watcher ledger.
- **A PTY-level destruction notice for every session kind** (not just
  watchers) — considered as the natural generalization of "tell the owner
  their session is gone," but the crash/reboot/external-SIGKILL case (point
  4's accepted gap) has no reliable closer to run the notice from: nothing
  is left alive to record the event durably before the process disappears.
  Building this generically is real work with a real payoff, but it is a
  session-layer project independent of watchers, not a watcher-specific fix,
  and is left as future work rather than smuggled into this change.
- **Keep registration-time cron dedupe/supersede, drop only the boot
  rebuild** — rejected because the dedupe existed to prevent a REBUILD from
  stacking a second live generation on a schedule (#1825, #2061); with no
  rebuild, the failure mode it guarded against cannot occur, and the
  dedupe's own machinery (the advisory-lock schedule key, the twin-supersede
  kill loop, the `generation`/`exclude_session` plumbing) has no remaining
  job. An agent that registers the same cron twice now simply has two
  sessions, exactly as if it had called `ava.shell.sessions.new()` twice —
  visible and resolvable through the one surface that already handles it.
- **Auto-restore a watcher on the owner's next turn regardless of
  idempotency** — the whole point of this change: a script's side effects up
  to the point of interruption are unknown to the platform, so re-running it
  is a judgment call only the agent (with an LLM behind it) can make safely.

## Consequences

- `ava.watcher` shrinks to spawn-only: `at`/`cron`/`launch` create a session
  and generated script; nothing reads them back except the session itself.
  `ava/_watcher_reconcile.py` and `ava/_watcher_reexports.py` are deleted in
  full; `ava/watcher.py` loses its dedupe, supersede, and registry-write
  paths.
- The `agent_watchers` table, its CHECK constraints, and the runner-role
  grants in `shared/cluster/provision.py` are **left in place** by this PR —
  expand-contract requires the DROP to be its own later migration once this
  code (which no longer reads or writes the table anywhere) has rolled out
  everywhere. Follow-up: a contract migration dropping `agent_watchers` and
  its grant, once no running code predates this change.
- **Deleting `reap_terminated_owner_watchers` (point 2) means a live watcher
  outlives its owner's termination and can wake it again.** A watcher's wake
  is an ordinary chat inbound (`source="watcher:<id>"`); chat delivery
  auto-resurrects a terminated agent (`gateway/routers/_delivery.py` ->
  `ops.resurrect_if_terminated`) — a watcher's source is neither `system` nor
  `system:<subtype>`, so it does not fall under the framework-notice carve-out
  that skips auto-resurrect, and the delivery path's own reasoning is
  explicit: "the user's reply (or any peer / watcher message) implies they
  want the agent alive to handle it." So a terminated agent with a live
  standing cron is woken again at every fire for as long as that watcher
  lives (up to its 7-day standing-cron cap, or indefinitely for one with an
  explicit longer end) — nothing reaps it early any more. This is intended,
  not an oversight: the LLM decides what a resurrect-by-watcher-wake means
  each time, exactly as it decides everything else that used to be
  code-level judgment in the old design (Context, above). An agent that does
  not want to be woken again kills its watchers (`ava.shell.sessions.kill`)
  before terminating.
- A watcher's `notify` completion policy behaves exactly as it did before:
  an explicit `notify="always"`/`"failure"` is baked into the shell command
  at spawn, while an omitted `notify` is resolved against the agent's
  `completion_notice_policy` by the gateway at delivery time, not at spawn
  (`ava/shell/background.py:notified_line`). It was never truly a registry
  fact (the registry only stored it to hand to a future rebuild), so
  removing the registry write changes nothing about how a watcher's own
  completion notice behaves.
- The crash/reboot-with-no-closer gap (point 4) means a watcher can vanish
  with zero record, same as an ordinary shell session can. Anyone who needs
  to notice a specific watcher's disappearance must poll
  `ava.shell.sessions.list()` for it, or design their watcher's absence to
  be discoverable some other way — this platform makes no promise otherwise
  for any session kind today.
- Registering `ava.watcher.cron()` a second time with an unchanged schedule
  now yields two independent, simultaneously-firing sessions rather than one
  superseded/reused session — a documented behavior change for anything
  that relied on the old dedupe (nothing in this repo's shipped skills or
  schedules did).
