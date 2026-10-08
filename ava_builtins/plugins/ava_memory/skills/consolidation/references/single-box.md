# Single-box memory consolidation

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Single box

You are the only consolidator; the checkout tracks `main` directly and there is
no pull-request fan-out.

Check the actual pool's remotes before choosing a command. A keep-local pool
has no remote. The current `../scripts/consolidate.py` unconditionally pushes;
it does not implement an `AVA_MEMORY_KEEP_LOCAL` switch. Do not use that wrapper
for local-only consolidation or add a remote to make it work.

For a local-only pool, inspect and validate the intended changes, then run:

```bash
git -C <POOL> add -A
git -C <POOL> commit -m '<date>: <short summary>'
ava memory refresh
```

If there are no changes, skip the commit. Re-curate `MEMORY.md` before the final
commit, preserve `## Setup`, and stay within the 16000-character cap. Check the
validator/commit-hook result and verify that a representative changed note is
searchable after refresh; a commit alone does not prove index freshness.

The wrapper procedure below is for a remote-backed single-box pool.

**For assigned ongoing maintenance,** reuse an existing consolidation schedule.
Only create the daily schedule when that standing work is authorized. A one-off
consolidation does not require a new schedule.

**Each time you are woken to consolidate:**

1. Run `python ../scripts/consolidate.py -m "<date>: <short summary>"`.
   This stages, commits, pushes, and refreshes the gateway index in one command.
   If the commit is rejected by the pre-commit hook, read the error, fix the
   offending file(s), and re-run.
2. Re-curate `MEMORY.md`: it is what every agent sees each session, so keep it a
   tight, current index under the 16000-char cap — promote into it what is being
   reached for often, demote stale or rarely-used lines into pointed-to notes,
   keep the `## Setup` header. Commit it:
   `ava.shell.run("cd <POOL> && git add MEMORY.md && git commit -m 'curate MEMORY.md' && git push")`.
   (The cap hook rejects an over-long `MEMORY.md` — split it if so.)
