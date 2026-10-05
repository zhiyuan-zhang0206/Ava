# Agent usage reports and budget reminders

Use `scripts/agent_usage.py` from this skill's directory on Ava's Python, with
cluster database access. It is independent of Fleet and never terminates peers.
Choose IDs based on the work you intend to observe, not task ownership.

```sh
python <skill-dir>/scripts/agent_usage.py \
  --agent-id 123 --agent-id 456 --lineage all \
  --start 2026-10-06T08:00:00+08:00 --end 2026-10-06T10:00:00+08:00
```

- `self`: only the specified IDs (default).
- `spawn`: roots plus recursive non-fork spawn descendants.
- `fork`: roots plus recursive fork descendants; a fork belongs to its context
  source, which may differ from the caller who requested it.
- `all`: both edges, including mixed spawn/fork chains.

All modes include roots, deduplicate overlapping selections, and follow immutable
birth ancestry rather than the mutable display tree. Unknown IDs or modes fail.
A reused peer may do unrelated work during the window: these are agent-scope
statistics, not task attribution. Choose a narrower window or explicit ID set
when ancestry is broader than the work you mean to measure.

`--start` and `--end` require timezone-aware timestamps. Windows are `[start,
end)`; omitting `--end` uses the current time on every poll. `--lifetime` replaces
`--start` and combines the folded lifetime ledger, later daily rows, and the
current event tail; it cannot take a historical end. Arbitrary windows read
retained events only and cannot recover precise times from folded history.

The JSON report contains roots, lineage, resolved IDs, window, per-agent and
per-model usage, and totals. `total_tokens` is input + output: cached input and
reasoning are already included, so do not add them again. `recorded_cost_usd`
sums usage-time prices; `unpriced_calls` exposes missing prices. In-flight calls,
unreported usage, and non-LLM expenses are absent. A recorded zero is not proof
that every possible expense was observed.

For a reminder, set a positive token or USD threshold and explicit recipients:

```sh
python <skill-dir>/scripts/agent_usage.py \
  --agent-id 123 --lineage all --start 2026-10-06T08:00:00+08:00 \
  --token-limit 200000 --notify-agent 123 --notify-agent 456 --poll-seconds 30
```

Run polling inside a watcher with an explicit lifetime timeout. Each poll
re-discovers descendants. The script sends one reminder per recipient when
recorded usage reaches a threshold, then exits. A failed send fails visibly;
a partial delivery may need the agent to retry only the remaining recipients.
Normal peer messages can wake a terminated recipient, so select recipients
intentionally. Watchers are not automatically restarted; keep their session
identity and recovery notes when the work needs continuity.

Treat the reminder like a context-compaction warning: reassess, preserve useful
results, finish a safe unit, prepare a handoff, or request a revised budget.
The responsible agent decides whether and how to stop further work. Notification
neither kills peers nor grants additional spending.
