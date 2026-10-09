# Repository development skills

This directory is the project-local skill entry point for agents developing
Ava. It follows the [Agent Skills layout](https://agentskills.io): each skill
has its own `SKILL.md`; references and runnable tools stay with their owner.

Start with [ava-self-development](ava-self-development/SKILL.md) and
[the contribution guide](../../docs/contributing.md). The shared references are
[Ava Guide](ava-guide/SKILL.md),
[Serious Engineering](ava-serious-engineering/SKILL.md) and
[Serious Research](ava-serious-research/SKILL.md).
[Review Contribution](review-contribution/SKILL.md) checks this repository's
rules; [Measure Complexity](measure-complexity/SKILL.md) runs the repository's
radon tooling; [Inspect a Trace](inspect-a-trace/SKILL.md) explains runtime
evidence and diagnostics. These independent tools keep their existing owners.

The three shared packages are symlinks to their canonical sources under
`ava_builtins/skills/`. This is a development selection, not a mirror of the
complete built-in catalog. Runtime and personal-service skills remain in their
built-in packages; removing a project entry does not remove those packages.
On checkouts with `core.symlinks=false`, mirrors may appear as plain files;
Ava's built-in loading remains independent of this directory.

Real project skills here are not fleet-wide convergence sources. Ava's Code
plugin discovers project-local skills from the current checkout; `.claude/skills`
and `.ava/skills` point here for the same repository context. Built-in skills
converge from `ava_builtins/skills/` through their existing install/update policy.
The external executor manual is independently owned by
[`impersonator-guide`](../../ava_builtins/skills/platform/impersonator-guide/SKILL.md),
where runtime launchers can find it without exposing a project entry.
