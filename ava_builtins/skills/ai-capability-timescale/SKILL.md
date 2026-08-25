---
name: ai-capability-timescale
description: Use before scheduling, estimating, or judging the feasibility of work whose pace or scope depends on AI agent capability.
---

# AI Capability Timescale

## When it is invoked

Invoke this skill automatically, as directed by the system prompt's Temporal
awareness section, before scheduling, estimating, or judging feasibility for
work whose pace or scope depends on AI agent capability. This includes
development speed, what can be automated, and what AI can fact-check or
monetize.

Invoke it as well when a plan silently assumes a capability ceiling, such as
“AI cannot do X reliably.”

## Why

AI capability evolves quickly beyond your knowledge cutoff. Feasibility
judgments based on cutoff-era capability are systematically wrong—usually too
pessimistic, often by an order of magnitude or more. Anthropic already bans time
estimates in its agent prompts for the same reason.

## What to do

1. Search the shared memory pool for the latest AI-capability cognition,
   including recent intelligence notes, model updates, benchmarks, and rulings.
2. Search the web for current model and tool capability, including frontier
   model releases, coding-agent benchmarks, and automation tooling.
3. Calibrate: assume current capability is at least an order of magnitude beyond
   your cutoff-era expectation, and state what you verified rather than what you
   assume.
4. Maintain discipline: report facts and status, not time promises. Give a time
   estimate only when the user explicitly asks, and label it as an estimate with
   an uncertainty range.

## Anti-patterns

- Deciding feasibility from training memory alone.
- Assuming a capability ceiling without checking.
- Promising durations.
- Treating a month-old benchmark as current.
