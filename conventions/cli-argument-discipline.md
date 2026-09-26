# CLI argument discipline: parse-layer gates, retained defaults with reasons

How the `ava` argparse surface treats a missing, malformed, or defaulted
argument. Read before adding or reviewing a CLI flag (user ruling 2026-09-20:
CLI arguments are fully explicit; tasks #4085 / #4092).

## The default: explicit, and gated at the parse layer

An invocation that cannot be honored is refused before any command code runs,
as a usage error — message on stderr, exit code 2. Three shapes:

- **Mandatory argument**: `required=True` on the argument, or a required
  mutually-exclusive group when exactly one source supplies the value.
- **Conditional requirement** (one of several flags must be present; X is only
  meaningful with Y): enforced in the verb's `_h_*` handler in `cli/parsers/` —
  the parse-layer boundary — never deep in the command body.
- **Value contract** (format, range, duration, JSON shape): a `type=` callable
  on the argument. It may lazily import the canonical validator from its owning
  module (`shared.*`, `services.*`) and re-raise `ValueError` as
  `argparse.ArgumentTypeError`; never duplicate the validator.

No environment variable satisfies a CLI argument that was not passed — the
caller writes it out. Command bodies keep their own guards for programmatic
callers (SDK, internal steps, tests), which bypass argparse.

## The exception: a retained default, with a reason

A flag may keep a default when writing it out would be noise — monitoring
verbs cron invokes bare, presentation bounds, safe-mode switches. Every
retained default carries a one-line reason comment at its definition site,
behind the marker:

```bash
git grep -n 'task #4092 cli-default inventory'
```

The marker *is* the inventory; there is no second list to keep in sync (the
same shape as the exception inventory in [numeric-limits.md](numeric-limits.md)).
The reason states why the default cannot be required and what fixes the value:
the cron payload, the display bound, the safe mode.

## Retained environment inputs

The worktree override deliberately stays environment-only:

- The worktree override in `cli/commands/_converge_skills.py` — the default is
  the protection (never sync a worktree checkout's sources into a production
  home); the environment switch is the deliberate escape hatch for a caller
  that means it.

## The review action

A new CLI argument without `required` / `type=` needs a required form, a
parse-layer gate, or a marked retained default. An invocation that can only
fail after the command starts — a traceback, or a bare `return 1` on a missing
or malformed argument — is a request-changes. Usage-error tests live beside
the verb in `tests/cli/` and assert exit code 2 plus the message.
