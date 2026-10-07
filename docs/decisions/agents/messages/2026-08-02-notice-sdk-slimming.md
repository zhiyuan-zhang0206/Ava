# Slim the notice SDK: no id on edit/dismiss, drop list_notices, drop self.log

## Context

SDK-alignment discussion (2026-08-02, user-led) reviewed the fleet surface. Three
findings:

1. **`ava.ui.list_notices` had no consumer-side reason to exist.** At most one
   notice is open per agent — `notify()` auto-resolves the previous one
   (`resolved_at = 'superseded'`), a structural invariant. The function's
   dominant real usage (statistics: ~80% of calls) was `list_notices(status="open")`
   purely to recover the id for `dismiss_notice(id)` / `edit_notice(id, ...)`
   — an artifact of the id-required signatures, not a genuine need. The resolved
   history it could also list is consumed by the user's unified inbox (frontend),
   not by the agent; agents had no use for their own notice history.
2. **The id argument on `edit_notice`/`dismiss_notice` was forced ceremony.**
   Since at most one notice is open per agent, "edit/dismiss my open notice"
   needs no id — the id existed only to feed the now-removed list flow.
3. **`ava.self.log` had weak adoption and no distinct value.** Usage data showed
   agents split roughly half/half on calling it; the activity trail it fed is a
   secondary view, and the same progress information flows through the reply
   text and task updates. Keeping an SDK verb for a rarely-consumed trail was
   surface without use.

## Decision

- `ava.ui.edit_notice(...)` — **no id parameter**; edits the caller's single
  open notice. Keyword-only fields unchanged (title/content/priority/blocking,
  `_UNSET` sentinel semantics preserved).
- `ava.ui.dismiss_notice()` — **no id parameter**; withdraws the caller's single
  open notice. Idempotent when nothing is open.
- `ava.ui.list_notices` — **removed** from the SDK (table, endpoints, and the
  frontend unified inbox are untouched; only the agent-facing verb is gone).
- `ava.self.log` — **removed** from the SDK (agent_activity table + snapshot
  field + activity endpoints stay for history/display; only the write verb is
  gone).
- Label semantics clarified: a label is a stable role name (`set_label`),
  task names are legitimate for ephemeral workers, and status lines do not
  belong in labels (they go in replies / activity / task notes).

## Alternatives rejected

- **Keep `list_notices` as a history query** (the "compact-recovery" argument:
  an agent could re-read what it asked the user). Rejected: the shared memory
  pool and the per-agent memory already carry durable context; a notice history
  is an odd, low-value slice of it, and the unified inbox is the human's view.
  If an agent ever needs its own notice rows, direct SQL (documented in the
  guide) covers it.
- **Keep id as an optional parameter** for backward compatibility. Rejected:
  the id was only ever recovered via `list_notices` (now gone), so a kept id
  parameter would be dead surface; fail fast is preferred over a compat shim
  nobody can populate.
- **Keep `self.log` and just document it better.** Rejected: adoption data
  showed the verb was optional in practice; documentation cannot create a
  consumer for a trail nobody reads.

## Consequences

- Notice writes are now shape-honest: `notify` → `edit` → `dismiss` describe the
  single open notice, matching the invariant.
- The system-prompt fleet section no longer instructs `self.log`; agents report
  progress through replies and task updates instead.
- `agent_activity` / activity endpoints remain (history), `agent_notices` table
  and the unified inbox remain; only agent-facing verbs were removed.
