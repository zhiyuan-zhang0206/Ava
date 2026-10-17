# Soft compact preparation contributions

## Scope

Preserve user-facing state and improvement evidence while the agent still
has its full working context and ordinary tools. Initial consumers are Fleet
and an independently maintained self-evolution plugin. The operating target
is at least 95% agent-authored compactions after a soft reminder among
autonomous window endings.
Do not claim this target without measurements from real model runs.

## Prior art

Meiva already has a once-per-window soft reminder, a hard automatic
summarization fallback, immutable PluginContributions, SectionFn builders,
manifest admission, and plugin activation telemetry. Reuse those mechanisms.

## Simplest sufficient design

Add one text contribution surface to the existing reminder. Core retains
thresholds, bookmarks, formatting and routing. Plugins return short
instructions, and the ordinary agent turn carries out the preparation.
This cannot guarantee execution when context jumps over the soft band or
when a model ignores the request. Preserve hard fallback and measure both.

## Context

The old reminder said the agent might want to wind down soon and deferred
on peer inbounds. Frequent peer traffic could suppress it throughout the
preparation band. The hook runner now composes message deltas with their
reducer, so that deferral is unnecessary.

The user of the interface is a plugin author who knows the plugin's durable
artifacts but should not need to understand graph state or compaction
thresholds. The agent receives concrete, brief instructions. The operator
needs evidence of completed paths, not a count of reminders emitted.

True constraints: hard summarization cannot run ordinary file-writing tools;
reminder text cannot prove file persistence; providers have finite contexts.
Deliberate constraints: keep the existing soft/hard thresholds, deliver once
per window, keep contributors synchronous and side-effect-free, and retain
the existing plugin load/restart boundary. No new state machine, model call,
priority registry or background service is needed.

## Decision

- Runtime faces declare `compact_reminder_sections: tuple[SectionFn, ...]`.
  The manifest declares their names under `compactReminderSections`.
- Core invokes the builders only when the reminder will fire. Empty text
  opts out; non-string results fail explicitly without committing a shown
  bookmark. Non-empty sections are attributed to their plugin and use the
  existing activation event.
- The core reminder explicitly directs brief preparation followed by
  `meiva.self.compact(summary)` before further substantive work. It includes
  the configured shared document when workspace guidance is enabled.
- Remove peer-inbound deferral. Keep the hard threshold and the exemption
  allowing a freshly committed summary its first normal generation.
- Persist `compaction_completed` and add a boolean for a reminder shown in
  that same compact version. Keep existing compact kinds. The Grafana
  completion breakdown distinguishes reminded and unreminded outcomes.
- The soft-share denominator contains agent-authored and automatic hard
  completions with the new attribution field, excluding manual requests
  and historical events without attribution. The panel shows two
  numerators: all agent-authored completions, and only those after a soft
  reminder. The latter owns the 95% acceptance target: proactive compact
  must not make absent plugin delivery look successful. No
  completions is no data; all hard completions is 0%, not a missing series.

## Consequences

Each initial plugin contributes roughly a paragraph only at the preparation
boundary; there is no extra LLM request or per-turn filesystem/network scan.
The builders receive resolved configuration and the model catalog, not
mutable graph state. The model can skip edits when nothing changed.

The target is observable, but not enforced by delaying safety compaction.
If hard-after-reminder remains high, inspect actual preparation turns. If
hard-without-reminder remains high, inspect context jumps and threshold
overrides before tuning. Validate file quality independently of path counts.
Existing critical-state persistence on meaningful changes remains necessary.

## Alternatives considered

- Separate before-LLM hooks in each plugin would duplicate thresholds and
  bookmarks and couple plugins to the core's internal state.
- A general before-compact hook would imply execution on every compact and
  invite arbitrary effects at a boundary where tools are unavailable.
- Repeated reminders, a new forced preparation phase, or a higher hard
  ceiling add behavior beyond the text extension. Real outcome data should
  establish the remaining failure before adding those mechanisms.
