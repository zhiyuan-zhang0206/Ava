# The branch-diff re-run judges HEAD in a throwaway checkout

## Context

`pre-push` re-runs every commit-stage hook over the branch contribution
(`scripts/hooks/prepush-branch-lint.sh`). The nested run executes the fixing
hooks (`ruff --fix`, `ruff-format`, `trailing-whitespace`, `end-of-file-fixer`,
`mixed-line-ending`) in fix mode, because that is how CI's
`pre-commit run --all-files` judges them: a hook that rewrites a file fails.

Two consequences made the push fail for a reason the developer could not act on:

- The fixers rewrote the developer's checkout during a push, so pushing an
  unformatted branch silently left the working tree dirty.
- `check-code-comments` asserts that the tracked working tree matches the index,
  which matches HEAD (its `git-snapshot` invariant). A fixer that ran earlier in
  the same nested run broke that assertion, so the push failed with
  "Code comment snapshot mismatch" instead of naming the hook that rewrote the
  file. The outer pre-push run could only report "files were modified by this
  hook" for the branch-diff hook as a whole.

## Decision

The nested run executes in a scratch `git worktree` of HEAD
(`scripts/provision/prepush_branch_diff.py`), created at push time and removed
when the run ends, with the checkout's `.venv` and `ui/web/node_modules` linked
in so `language: system` hook entries resolve. Hooks judge the pushed commit's
own text and may rewrite files without touching the developer's checkout.

The fixers still dirty the scratch tree, so the hooks that failed in the first
pass are re-run one by one, each against a freshly reset scratch tree. A hook
that passes there failed only because another hook rewrote files: it is reported
as such instead of as a verdict. The survivors are the verdict; each fixing hook
is reported with the paths it rewrote, and the summary is the last thing the hook
prints.

## Alternatives rejected

- **Give the nested hooks check-only arguments.** A per-hook flag table drifts
  the moment a fixer is added or changes its flags, and three of the five fixers
  (`trailing-whitespace`, `end-of-file-fixer`, `mixed-line-ending`) have no check
  mode at all: they would silently drop out of the gate.
- **Let `check-code-comments` skip its snapshot assertion inside the nested run.**
  It removes the interlock but leaves the fixers rewriting the developer's
  checkout, and it makes a real invariant depend on who invoked the hook.
- **Replace the index (`GIT_INDEX_FILE`).** The dirt is in the working tree, not
  the index; the cleanliness assertion compares the two.
- **Stash around the nested run.** It cannot cover changes a hook makes, a crash,
  or an edit that happens concurrently, and the outer pre-commit already stashes
  the commit stage's unstaged changes.

## Consequences

- A push pays one checkout of the tree (about 0.5s for ~7k files) and a
  `git worktree` write in the repository's common directory; the checkout is
  removed when the run ends, and a removal that fails leaves only a `/tmp`
  directory behind.
- Each hook that fails costs one more pre-commit invocation, in the failure path
  only.
- The nested verdict no longer depends on the developer's checkout state: staged
  but uncommitted files, partial commits and unrelated working-tree edits can
  neither pass nor fail the pushed commit.
- The gate judges the checkout's HEAD whatever ref is pushed, as it always did;
  a push of another ref now says so instead of implying the pushed ref's verdict.
- A future hook that needs another untracked local path must add it to
  `scripts/provision/prepush_branch_diff.py:_TOOLCHAIN`, or it sees the scratch
  checkout without that path.
