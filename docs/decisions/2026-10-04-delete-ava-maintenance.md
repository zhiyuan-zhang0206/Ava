# `ava maintenance` is deleted: `ava status` reads the hold and `ava start` ends it

## Context

[The previous decision](2026-10-03-delete-manual-maintenance-verbs.md) left `ava maintenance` with
three verbs that were the hold's exits rather than slices of the stop/start kernel: `status`,
`repair` and `cancel`. Each had a reason the kernel could not cover, and none holds up.

- `status` is read by the fleet update's start-of-work refusal and by the out-of-band triage. It is
  a reader of one local file; it did not need a command group of its own.
- `cancel` released a hold that had not started stopping, without launching anything, behind a
  generation check. `ava start` already releases a hold after readiness, over a unit whose services
  are running it launches nothing, and the hold's generation is a property of the journal the start
  reads under its own lock. The check protected against an operator copying the wrong
  `--operation`/`--acquired-at` pair between machines; with no command taking that pair there is
  nothing to copy.
- `repair` was the only release for a hold latched on failed continuation receipts, and `ava start`
  refused while any stood. What it asked of the operator was "fix the root cause, then repair", but
  it verified nothing about that cause. Its only proof was that the agent-host had no active
  continuation. After it ran, the release woke every restart pointer and the agent continued from
  its last durable checkpoint, exactly what a host crash or `ava stop --force` gives. The pointer is
  what carries the agent's continuation, and it lives in Postgres whether or not the turn's final
  flush landed. So the gate was an acknowledgement, not a check, and a start that cannot proceed
  until a person types a command with two copied arguments stalls the update that issued it.

## Decision

1. Delete `ava maintenance` whole: the parser, `cli/commands/lifecycle/maintenance.py` and its
   tests, `repair`'s operator-identity record (`MaintenanceHold.repaired`, `repair_record`,
   `admission.repair`, `validate_repair_record`) and the exact-generation arguments. No alias. A
   journal written with the retired `repaired`/`repair_record` keys still reads; the keys are ignored.
2. `ava status` carries the hold: a first section for people, and `ava status --json` printing only
   the hold as one JSON object (`{"hold": {...}}`), settings-lite, with no database, gateway or
   service probe. The fleet update and the triage read that command. `ava status` itself has no
   other JSON form, so `--json` is the hold's machine reader, not a second rendering of the screen.
3. `ava start` settles a hold's failed receipts, so nothing blocks it. Once the unit serves and
   before the hold releases, each failed agent's restart pointer is checked in Postgres:
   - still pending or claimed: the release's resume wakes it with the rest (re-delivery);
   - gone (command finished, agent row missing): logged at ERROR with the agent, category and
     hold, and told to the owner as an alert row plus IM push, the channel `ava start` already uses
     for a service that missed its window;
   - the agent is terminated: nothing is owed and nothing is revived.

   Then the receipts are cleared from the journal by compare-and-swap and the hold releases. A start
   that fails before the unit serves settles nothing, so the retry sees the same receipts.
4. Abandoning a drain that never stopped services (`cancel`) is `ava start`. Error messages that
   named `repair`/`cancel` name `ava start`.

## Alternatives rejected

- **Keep `repair` as the audited human acknowledgement.** The acknowledgement protects nothing the
  system relies on, as the context shows; the audit it left is replaced by the ERROR log and the
  owner's notice, which are written at the moment of release instead of by whoever ran the command.
- **Block `ava start` on failures that cannot be re-delivered.** The only recourse then is a human
  deleting a receipt, which is `repair` again. The agent has no continuation left, so there is
  nothing for the block to protect; the signal that matters is that the owner is told.
- **Put the hold in the existing `ava status` output only, without `--json`.** The fleet update
  refuses to start work on a held host and the triage classifies a stranded one, both over ssh and
  both parsing; screen-scraping the table would make a display change a protocol break.
- **Make `--json` the whole of `ava status`.** The status screen probes services, the data plane and
  the gateway; the hold's readers need the one thing that answers with all of those down.

## Consequences

- A hold with failed receipts no longer waits for a person. A continuation that raised during the
  drain resumes from its last durable checkpoint, so the tail of that turn after the checkpoint is
  lost, as after a crash; the ERROR log and the owner's notice are the record of it.
- `ava start` over a hold that has not started stopping (`preparing`, `draining`, `drained`) is the
  documented exit; it was previously exercised only by holds that had stopped services.
- The `starting` and `ready` phases stay in the journal vocabulary; removing them remains a separate
  journal-schema change.
- An operator script that ran `ava maintenance ...` fails at argument parsing. None exists in the
  repository.
