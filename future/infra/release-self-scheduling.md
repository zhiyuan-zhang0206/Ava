# Release cadence: self-scheduling by Ava

The dated release cadence — `scripts/release_cut.py daily|weekly`, see
[the release cadence](../../.agents/skills/roll-out-a-cluster-update/SKILL.md) — is currently cut
**manually** (or not at all on idle days). It is deliberately **not** wired to OS
cron or a static `ava.watcher.cron` registration yet.

## Why not just schedule it now

A static cron entry would be scaffolding to strip later. Cutting a release is
itself an agent action — read the window's PRs, summarize them, tag, push — which
is a natural fit for a self-hosting agent, not an external timer. Ava already
owns its own upgrades (`ava cluster update` — the CLI, `ava.self.update()`
was removed 2026-08); owning its own release cadence is the same shape.

## The third mechanism, which postdates this note

This was written when the only options were OS cron and a static
`ava.watcher.cron` registration. Since then the gateway-supervised **schedules**
subsystem landed (`ava schedules`, `schedules` / `schedule_versions`; the old
`cron_jobs` table is gone — see `decisions/2026-07-01-cron-to-scheduler-cutover.md`),
and it sits much closer to what this note wants: a schedule is a supervised
persistent session running a versioned script, not a timer firing a shell line.

That does not settle the question, it sharpens it. A schedule still decides
*when* on a fixed rule; the argument above is that the *when* is itself a
judgment — whether this window's PRs are worth a cut. So the open choice is:

- register a schedule whose script asks the agent to judge and cut, which keeps
  the judgment with the agent and uses the mechanism for supervision only, or
- leave it manual until Ava is trusted to initiate the cut unprompted.

The first is buildable today; it is not built because the prerequisite below is
about trust, not mechanism.

## The intent

Once Ava's bootstrapping is complete (Ava reliably self-driving its own ops
loop), **Ava schedules its own releases**: it decides when to cut a `daily` /
`weekly`, runs the cut, reviews the digest, and publishes — the same way a
maintainer would, but as the agent itself.

## Prerequisite & interim

- **Prerequisite**: bootstrapping done — Ava trustworthy on its own ops loop.
- **Until then**: cut manually when useful, or leave gaps. The date-suffixed
  scheme (`v<x.y.z>-<YYYYMMDD>`) tolerates missing days, so nothing breaks if a
  day has no cut.
