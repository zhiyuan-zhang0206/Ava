# A presentation-only feature that fails degrades and warns; everything else still fails fast

## Context

Meiva's default is to fail fast: an error in an action surfaces to the caller instead of being
papered over (see [fail-fast vs reconcile](2026-07-19-fail-fast-vs-reconcile-boundary.md) and
[no silent failures](2026-10-04-no-silent-failures.md)).

Per-agent tab groups in the shared Chrome are the first feature that does not fit that default. The
browser daemon files each page an agent opens into a Chrome tab group titled after the agent, so a
person looking at the shared browser can tell whose tabs are whose. The grouping needs an extension
loaded over CDP, a mapping from a chrome-devtools-mcp page id to a CDP target, and a call into the
extension's service worker. Any of those can fail: the extension is refused, the worker is not up, a
user opens a tab at the same moment and the new target is ambiguous, Chrome is wedged.

The grouping changes nothing about what the browser operation does. `new_page` has already opened
the page and the agent's tool call is already correct whether or not the page ends up in a group.
Failing the call would report an error for an action that succeeded, and would let a cosmetic feature
block a core tool (the agent would retry and open a second page).

## Decision

A feature whose **only** effect is presentation may degrade instead of failing the operation it
decorates, on all of these conditions:

1. **Presentation only.** Skipping the feature leaves the operation's result, state and side effects
   exactly correct. If a failure could leave the action itself wrong, partial or unverifiable, the
   feature is not in this class and fails fast as usual.
2. **Never silent.** Each failure is logged at WARNING with the cause (traceback for unexpected
   errors) and says what degraded and that the operation itself succeeded. This is the same bar
   `no_silent_failures` enforces for any broad handler; the exception removes the re-raise, not the
   report.
3. **Contained at one boundary.** The degrade lives in the feature's own entry points, which swallow
   `Exception` (never `CancelledError`) and are bounded by a timeout so a wedged dependency cannot
   stall the caller. Callers do not wrap the feature in their own handlers.
4. **No guessing.** When the feature cannot determine its input unambiguously, it does nothing and
   warns rather than acting on a best guess (for tab groups: anything other than exactly one new page
   target between the before and after snapshots leaves the page ungrouped).

Tab groups (`services/desktop/browser/tab_groups/grouping.py`) are the implementation: `AgentTabGroups`
exposes `snapshot()` and `group_new_page()`, both of which report their own failures and return.

## Alternatives rejected

- **Fail the browser call when grouping fails.** Consistent with the default, but it turns a cosmetic
  problem into a tool error for an action that worked, and invites duplicate pages on retry.
- **Degrade silently (debug log or no log).** Meiva already paid for that shape: a failure nobody can see
  is indistinguishable from a feature that works. The warning is what makes the degrade acceptable.
- **A general "optional feature" switch or fallback framework.** The class is narrow (presentation
  only, with an unaffected action) and one boundary function per feature states it better than a shared
  mechanism would. Each new use should be able to show conditions 1-4 on its own.
- **Retry grouping in the background.** More machinery, and the cases that fail (ambiguous target,
  extension refused) do not get better by retrying.

## Consequences

- The class of presentation-only failures is visible in logs as WARNING lines naming the agent and
  the cause; read them as findings, not noise.
- Reviewers decide a new use against condition 1. When the feature's failure could change what the
  action did, this record does not apply.
- Tab groups depend on Chrome deleting a group when its last page closes rather than on code of ours:
  no deletion path exists in this repository, and a test asserts that every page-closing path goes
  through `close_page`.
