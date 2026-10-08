# Per-machine memory steward

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Steward (multi-host only)

You publish one machine's day of notes. Your prompt carries the arbiter's id.

1. Run `python ../scripts/steward.py -m "<machine> <date>: <short summary>"`.
   This stages, commits, pushes, and creates a PR (if none exists for your branch)
   in one command. If the commit is rejected by the pre-commit hook, read the
   error, fix the offending file, and re-run. If there is nothing to commit, the
   command prints "(nothing to commit)" — message the arbiter "nothing to commit"
   and go to step 3's wait.
2. Tell the arbiter your request is ready:
   `ava.agents.send_message(arbiter_id, "PR ready: branch <your branch>")`.
3. When the arbiter messages you "rebase now":
   `git -C <POOL> pull --rebase origin main`.
