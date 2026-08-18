---
name: write-a-pr-description
description: Use when writing or reviewing a PR description. The required shape is a file-tree diff with ★ critical paths, a prose data-flow supplement, and an explicit not-tested section.
---

# PR description: the intermediate state

A PR description isn't just a summary — between summary and commit diff there needs to be an **intermediate
state** so the reviewer can catch design errors without reading the full diff.

Read this skill before writing a PR description. Reviewers also use the "self-check"
section below to verify the description is sufficient.

Intermediate state = **file-tree diff** + **prose data flow supplement**

## 1. File-tree diff (always required)

Draw a file tree following the codebase structure, mark each entry with `(A/M/D/R)` + a note. **Note length
scales naturally with the size of the change** — one line for a 10-line rename, a paragraph for a 500-line new core component;
no upper or lower bound:

```
services/
├── delivery_watchdog/
│   ├── daemon.py              (M)  split the single tick into wake-dispatch +
│   │                                stall-alert jobs; per-job error isolation
│   │                                so one failing job never starves the other
│   └── wake_dispatch.py       (A)  ★ new job: re-publishes the Redis wake for
│                                    pending inbounds of idling owners older
│                                    than the dispatch threshold — collapses
│                                    lost-publish recovery from 30s to ~1.5s
├── healthchecks/
│   └── delivery_watchdog.py   (M)  probe covers both jobs' liveness stamps
gateway/
└── routers/agents.py          (M)  spawn path stamps last_active_at on wake
shared/
└── live_events.py             (D)  ★ retired the legacy wake broadcast channel
                                    (every consumer now rides the keyed wake)
```

`★` marks the **critical path**: new entry point / new long-running process / removed old
entry point / new cross-boundary call (cross-process / cross-container / cross-network). Reviewers
scan for ★ to pick out "what is this new entry point doing / why was that one removed" — i.e. design questions.

**Prerequisite**: the codebase is organized well enough that filenames are self-documenting
(`wake_dispatch.py` is obviously the wake-dispatch job; no comment needed to explain it).

## 2. Prose data flow supplement (as needed)

The file tree shows **what changed**; prose covers **how runtime behavior changes**:

- Walk through the most critical control flow ("A calls B → B drops into D's queue → C consumer ...")
- When crossing a runtime boundary (new docker / CI / lambda), list a prod vs PR behavior table
- Explicitly list invariants / NOT tested / uncovered boundaries

**Trigger condition**: if the reviewer can't answer "what does runtime look like" from the file tree alone —
add prose. If they can, skip it (a typo fix doesn't need a prose section).

## 3. Rendering invariants (don't break the code fence)

The PR body is markdown — GitHub renders it on web, mobile app, and email. One mechanical trap accounts
for most broken renderings:

**Backticks must not be backslash-escaped.** `gh pr create --body "$(cat <<EOF ...)"` (unquoted HEREDOC)
lets the shell interpret `` ` `` as command substitution. The natural workaround is `\``, which leaves the
backslash in the body. GitHub then renders `\`` as a literal `` ` `` character — the triple-backtick code
fence never opens, and the file-tree section collapses into a runs-on paragraph with visible `` ``` `` markers.

Rules:

1. HEREDOC delimiter **must be quoted**: `cat <<'EOF' ... EOF`. Never `cat <<EOF`. The quote prevents shell
   expansion so backticks pass through raw.
2. Never write `\`` in a PR body. If you reach for backslash escaping, the HEREDOC is wrong — fix the
   delimiter, not the content.
3. Verify after push (must return `0`):
   ```bash
   gh api repos/<owner>/<repo>/pulls/<num> --jq .body | grep -c '\\`'
   ```
   Non-zero → `gh pr edit <num> --body "$(cat <<'EOF' ... EOF)"` to rewrite.

## Reviewer self-check (ask yourself 3 questions after writing)

Without looking at the diff, can I answer:

1. Which files changed + where the critical path is (← look at file tree)
2. What runtime behavior changed (← look at prose)
3. Where it's **not** tested / **not** covered (← look at explicit NOT section in prose)

Can't answer → description is too thin.

## Anti-patterns (common in AI collaboration)

- A long file-changed bullet list with no hierarchy → can't see which is the entry point
- Note length artificially compressed → critical context squeezed out
- "Tested happy path X" treated as "everything works" → explicitly mark NOT tested boundaries
- Abstract verbs ("integrated / consolidated / optimized") that don't expose internal control flow
