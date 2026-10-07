# Code guards only where the mistake is certain and cannot be undone

## Context

The [one cluster per host](../../../runtime/hosts/2026-09-30-one-cluster-per-host.md) work left a development
checkout on the same host as the production cluster, with `AVA_HOME` unset meaning
`~/.ava`: production. Two guards were built to keep development code off that home.

The first was a scan. Every script under `scripts/`, `.agents/skills/` and
`ava_builtins/skills/` that imports application code had to enter a scratch `AVA_HOME`, or
declare the local cluster state it operates on, from a closed vocabulary of eleven states,
in a comment line or a directory table. The scan came to about 600 lines of test, a dozen
declaration comments and a scratch-home call in 22 scripts. It also taught three lessons
the hard way: a call made at import time replaced `AVA_HOME` for a whole pytest worker; a
call in a script CI runs on a bare `python3` failed because the call's own module imports a
third-party package; and the worktree-removal guard has to read the real home, so a
scratch home broke it. Each fix added a rule to the scan.

The second was the CLI's checkout guard: a home that carries its own `<home>/source` is
changed only by that checkout's CLI, with a list of read-only verbs exempt.

The user asked whether this much protection is needed.

## Decision

A guard in code is kept where both hold: the mistake is made without anyone deciding to
make it, and its consequence cannot be undone. Everything else is a development
convention, written down once.

- **Git hooks** are launched on every commit and push, by nobody's choice, on a checkout
  that may sit beside production, and a hook that boots the config reads that cluster's
  `.env` and credentials. They keep a code guard: each script a hook launches that reaches
  application code calls `enter_scratch_home()` behind `if __name__ == "__main__":`. A test
  derives the hook scripts from `.pre-commit-config.yaml` (so a new hook is covered), follows
  the imports of the `scripts.*` helpers a hook script uses, and one canary run proves a
  hook script never reads the home its environment names.
- **Every other script that imports application code** is run by a person or an agent on
  purpose. The rule is the convention in `conventions/dev-setup.md` and the
  self-development skill: in a development checkout, set a temporary `AVA_HOME`, or run
  inside the Docker or Tart verification boundary. There is no scan, no declaration form and
  no scratch-home call in those scripts.
- **The CLI checkout guard** stays, because the act it prevents is irreversible: a foreign
  checkout's CLI stopping or reconfiguring the production cluster. It is a refusal, not a
  prompt, a warning or an override; no flag or variable lifts it. This entry records the
  principle and the conclusion only; what the guard allows and refuses is stated in the
  change that implements it.

Two small things stay because they are cheap and each was a real failure:
`enter_scratch_home()` raises inside a pytest process (a tool imported at collection must
never replace the session's home), and `scripts/check_worktree_remove.py` keeps the real
home, because it reads this machine's live session registry, and only sets
`AVA_CONFIG_FETCH=skip` so that it dials nothing.

## Alternatives rejected

- **Keep the scan.** It maintained a vocabulary and a declaration for every script in three
  roots that imports application code, though each is run by someone who chose to run it. A
  declaration names what a tool touches; it stops nothing. Its failures cost more than a
  missed convention would have: each of the three lessons above came from the mechanism, not
  from the mistake it targeted.
- **A wrapper every hook entry goes through.** One place, but 48 hook entries (nine of them
  shell commands) to route, and a new hook can forget it. The guard sits in the script that
  boots the config, so it covers every caller of that script.
- **Setting `AVA_HOME` where git hooks are installed.** The hook shims are generated per
  clone by pre-commit, and the repository forbids installing around them
  (`core.hooksPath` overrides).
- **A code guard on every script, and a convention for none.** The first costs what the scan
  cost; the second leaves the hooks, which nobody launches on purpose, to chance.

## Consequences

- A person who runs a non-hook development tool bare, on a host that runs production, reads
  `~/.ava`. The convention is the only guard, and the self-development skill carries it
  into agent briefs.
- The CI smokes, `release_cut.py` and the post-deploy visual check no longer enter a scratch
  home. In CI the runner's home is its own; on a developer host the convention applies.
- A new hook script that reaches application code fails the test until it enters a scratch
  home; a new hook script that does not, costs nothing.
