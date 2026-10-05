# 0003 — "Touched areas" is not the blast radius

**Date:** 2026-07-30
**Anchors:** PR #960 (pre-cutover; not reachable from public `main`). Surviving
code: `shared/message_kwargs.py:NoteTag`, the exhaustiveness assertion at
`tests/gateway/test_timeline.py` (`assert shown | hidden == set(NoteTag)`),
`scripts/lint_note_tags.py`, `agent/state.py:_BASE_STATE_FIELDS`.

## Summary

A change added one member to the `NoteTag` enum in `shared/message_kwargs.py`.
The pre-push test selection was chosen the natural way — the directories where
the edits were — so `tests/agent tests/shared tests/cli tests/ava` ran and were
all green. CI failed on `tests/gateway/test_timeline.py`, which asserts
`shown | hidden == set(NoteTag)`: a deliberate forcing function that lives in the
*consumer's* test file, precisely so that adding a tag cannot skip the show/hide
decision. Edit-adjacency will never find a guard that is placed on purpose where
the edit is not. Anything in `shared/` is consumed by gateway, agent, and cli, so
for `shared/` the blast radius is the whole suite.

## Timeline

The change added a member to `NoteTag`. Before pushing, the repo rule
"run pytest for touched areas" was followed by listing the directories containing
the edits: `tests/agent`, `tests/shared`, `tests/cli`, `tests/ava`. All green.

CI failed. `tests/gateway/test_timeline.py` classifies every `NoteTag` into a
`shown` set and a `hidden` set, then asserts their union equals `set(NoteTag)`.
The new member was in neither, so the assertion failed — which is exactly what it
is for. Its comment says so in place: *a new member added without a decision here
fails this, forcing the show/hide call to be made explicitly.*

Nothing about the failure was mysterious once seen. The cost was the round trip:
a red PR, a CI cycle, and a second push.

## Root cause

Two facts multiply.

First, `shared/` is the bottom of the import layering (`shared < ava < agent <
gateway < cli`), so a change there is by construction visible to every layer
above. There is no such thing as a local `shared/` change.

Second, this repo uses hardcoded exhaustiveness assertions over enums and field
sets as **review forcing functions** — `set(NoteTag)` in the gateway's timeline
tests, `{m.value for m in TerminateResult} == set(get_args(literal))` and its two
siblings in `tests/ava/test_agents_sdk.py`. A forcing function is deliberately
placed at the point where the *decision* must be made, which is the consumer, not
the definition. Its value comes from being somewhere the author of the enum change
was not looking.

So the heuristic "run the tests near your edits" is not merely incomplete here; it
is anti-correlated with where these particular guards live.

The escape analysis:

- **The convention itself.** AGENTS.md says "run pytest for touched areas before
  pushing". The sentence is right; the failure is in resolving "area" to
  "directory of the diff" — the only interpretation available to someone reading
  the diff, and the wrong one for `shared/`.
- **Local test run.** Green, and honestly so. It ran what it was asked to run.
- **The type checker.** Adding an enum member is type-correct everywhere. Only the
  runtime set comparison notices.
- **Review.** The reviewer sees a one-line enum addition. The guard that objects is
  in a file the diff does not touch.

## Guardrails added

- **The rule, scoped to the trigger**: editing anything in `shared/` — especially
  an enum or a `BaseAgentState` field — means running the whole suite,
  `pytest tests -q --ignore=tests/e2e`, not a guessed subset. It is written into
  [`.agents/skills/run-local-tests/SKILL.md`](../.agents/skills/run-local-tests/SKILL.md)
  and condensed in
  [`conventions/defensive-patterns.md`](../conventions/defensive-patterns.md).
- **A cheap pre-check for the enum case**, when the full run is genuinely not
  affordable in the moment: `grep -rn "set(<EnumName>)\|list(<EnumName>)" tests/`
  finds the hardcoded guards. It is a narrowing aid, not a substitute.
- **`scripts/lint_note_tags.py`**, a pre-commit hook gated on
  `shared/message_kwargs.py` and `ui/web/src/components/timeline/markers.tsx`,
  covers the *other* half of the same enum's blast radius: a new tag that the
  frontend dispatch does not handle falls through to `UnknownMarkerChip`, which
  the user sees and the developer does not. It is the model for closing a
  cross-boundary exhaustiveness gap mechanically, where the boundary is stable
  enough to name.

**Still unguarded, deliberately.** There is no general "which tests can this diff
break" analyzer, and hardcoded exhaustiveness guards are worth their cost
precisely because they are hardcoded. Derived sets are the exception that needs
nothing: `agent/state.py:_BASE_STATE_FIELDS` comes from
`BaseAgentState.model_fields` and auto-syncs, so it can never go stale. Only the
hardcoded ones bite.

## Lessons

- **The blast radius of a change is where its consumers' guards live, not where
  its lines are.** For anything in `shared/`, that is every layer above it.
- **A forcing function is placed where you are not looking — that is its job.**
  Any selection heuristic keyed on edit adjacency is structurally blind to it.
- **When a convention says "the areas you touched", resolve "area" by dependency,
  not by directory.** If the answer is not obvious, the full suite is the answer.
- **Prefer a mechanical guard when the boundary is nameable.** `lint_note_tags.py`
  turns one arm of this class into a pre-commit failure with a clear message;
  where a lint can be written, it beats a rule someone has to remember.
