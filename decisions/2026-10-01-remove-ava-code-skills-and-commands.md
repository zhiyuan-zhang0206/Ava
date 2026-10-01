# The ava_code plugin ships no skills and no slash commands

## Context

The `ava_code` plugin carried content next to its runtime face: four on-demand
skills (`conventions`, `pr`, `testing`, `worktree`, loaded as
`ava.skills.ava-code:*`) and three composer commands (`/ava-code:pr`,
`/ava-code:review`, `/ava-code:worktree`). The system prompt's coding-tools
preamble already carried the short form of the same rules and pointed at two of
the skills for detail (`worktree`, and `ava-code:conventions` for the `rg`
benchmarks).

On 2026-10-01 the user reviewed them and ruled that all of it was redundant or
misleading, and that the commands go with the skills. The skills restated what
the preamble, the repository's AGENTS.md and the repo's own `.agents/skills/`
already say, which put one rule in several places that had to be kept in step;
the commands were short prompts restating the same workflow.

## Decision

Delete the plugin's `skills/` tree, its `commands/` tree and the OKF index that
listed the skills. Keep the plugin: its runtime face is unchanged (`ava.cwd`,
the `files` / `shell` / `understand` / `ui.serve` path wraps, the AGENTS.md /
CLAUDE.md context injection, the two prompt sections, the project-local skill
source). The two pointers in the coding-tools preamble are dropped; the rules
beside them stay.

Everything that named the deleted skills is cleaned in the same change, and the
operator tool `scripts/data_repair/migrate_skill_identity.py` now treats the
bare `ava_code` entry and the four former identifiers as dead references to
drop, instead of expanding the bare name into them.

## Alternatives rejected

- **Delete the whole plugin.** The skills and commands were the only part that
  was surplus. Nothing in `plugin.py`, `_code_namespace.py`, `_walk.py` or
  `agent_runtime.py` was written for them. Removing the plugin would lose
  `ava.cwd`, the cwd-relative path wraps, the AGENTS.md injection and the
  project-local skill source, and would orphan the persisted `ava_code__*`
  checkpoint channels (`ava_code__cwd` is read on restart and by the ops
  `agent_skill_view` op).
- **Delete the skills, keep the commands.** The ruling covers both, and the
  commands were thin restatements of rules that live elsewhere. Keeping them
  would leave `/ava-code:pr` and `/ava-code:worktree` as a second copy of the
  workflow with no skill behind it.
- **Trim or rewrite the skills instead of deleting them.** The ruling was that
  they are surplus, not that parts of them are wrong. A thinner copy is still a
  second place for rules that AGENTS.md owns, which is the drift this removes.

## Consequences

- The composer picker loses five entries: the three commands, plus the
  `conventions` and `testing` skill-commands (the `pr` and `worktree` commands
  used to shadow their same-named skill-commands). A removed `/ava-code:...`
  typed by hand matches nothing and is passed through as plain text.
- The preamble no longer sends the agent to a skill for worktree naming or
  `rg` timings. The worktree, commit-trailer and PR-title rules stay in it, and
  a project's own rules come from its AGENTS.md as before.
- An installed copy is converge-managed derived state. When the source
  disappears, the next converge removes an untouched `$AVA_HOME/skills/ava_code/`
  and its registry row, and keeps a locally edited copy with a warning.
  `ava packages refresh` never deletes a copy: for a vanished source it only
  records an error on that row. No migration is involved.
- Stored `skills_to_inject_into_system_prompt` / `skills_to_expand_at_start`
  lists may still name the old identifiers. The prompt builder warns once per
  process and skips an unresolved name; the data-repair tool above clears them
  from the stored config.
- A running agent keeps the `# Capabilities` index it built until its next
  rebuild. The drift hook announces additions only, so the removed skills stay
  listed, and fail to load, until then.
- `ava_code` is now purely a runtime plugin. The example of a plugin that ships
  skills next to code in `conventions/plugin-spec-v2.md` names `ava_fleet`.
