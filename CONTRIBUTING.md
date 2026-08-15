# Contributing to Ava

Ava is an Apache-2.0 **code-as-action** agent with a deliberately small,
minimal core. Skim [conventions/philosophy.md](conventions/philosophy.md)
before a non-trivial change — a contribution that fits the small-core /
fail-fast charter is far more likely to land. [AGENTS.md](AGENTS.md) is the
architecture overview (much of it is maintainer ops you can skip).

## Dev setup

[.agents/skills/deploy-ava-cluster/SKILL.md](.agents/skills/deploy-ava-cluster/SKILL.md) is the full bring-up (Postgres
17 + Redis native — no Docker — plus Python 3.12 + uv, Node). For just
hacking on the code:

```bash
uv sync                      # Python deps + the `ava` CLI into .venv
.venv/bin/pre-commit install    # lint hooks (fast; the test suite runs in CI)
```

## Workflow

Standard fork-and-PR — use whatever local git setup you like:

1. Fork, branch from `main`, one focused change per PR.
2. Make the change; run the tests for the area you touched
   (`.venv/bin/pytest tests/<area>`) and `.venv/bin/pre-commit run --all-files`.
3. Open a PR with a clear **what + why**. Rebase onto latest `main` before
   pushing (see [`.agents/skills/ship-a-change/SKILL.md`](.agents/skills/ship-a-change/SKILL.md)). CI must
   be green; a maintainer rebase-merges (linear history — no merge commits).

For a larger change, a description that shows the reviewer *where the critical
path is* — the file-tree-diff style in
[.agents/skills/write-a-pr-description/SKILL.md](.agents/skills/write-a-pr-description/SKILL.md) — gets reviewed
faster. It's a suggestion, not a gate.

## A note on internal references

Comments and docs reference the maintainer's internal task tracker
(`Task #N` / `#N` / `PR #N`). Those numbers carry no meaning for outside
readers — the surrounding text always states the reason, so read the
sentence, not the number.

## Conventions (enforced by pre-commit)

- Latest stable everything; no beta/nightly.
- English for all code, comments, docstrings, and user-facing strings.
- **Fail fast** — don't add shims for "mistakes the model might make"; let it
  blow up and feed the error back. No `case _:` catch-alls on enum dispatch, no
  `get(...) or {}` for required fields.
- Structural rules (import direction, file-size ceiling, no `print` in framework
  code) are linted: `.venv/bin/pre-commit run --all-files`.

By submitting a contribution you agree it is licensed under Apache-2.0
(inbound = outbound).
