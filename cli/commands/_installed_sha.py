"""Installed-commit bookmark CLI — `python -m cli.commands._installed_sha`.

The parameter-translation seam the cmd.exe update chain needs for
`shared.source_integrity.set_installed`, in the same shape and for the same reason
as `cli.commands._updater_lease`: the update legs on Windows are shell chains that
cannot import Python state, and the state logic itself stays in Python.

**Why the Windows chain needs a seam at all.** The POSIX updater is the in-process
entry (`cli.commands._update_agent_runner`), which records the commit it just
checked out and synced before it stops anything. The cmd.exe chain hand-builds that
ladder instead, and it never recorded the bookmark — so every Windows self-update
handed its own trailing `ava start` a HEAD that did not match `installed_sha`, and
the source-integrity guard read a rollout as tampering:

    SOURCE INTEGRITY VIOLATION / HEAD c5f0539 / installed 902af72

That is a false alarm on the one path that is *supposed* to move HEAD, and it is not
free: the guard's response is to re-run `uv sync` before bringing services up, so a
Windows host paid a second full sync on every update, inside the window the rollout
is waiting out. It also spends the guard — an operator who sees the violation after
every update has no way left to notice a real one.

**Where the chain calls it, and how.** After `uv sync` and before the restart
ladder, chained with `&&` so a failed sync still short-circuits past it, and
wrapped `(... || ver>nul)` so the bookmark itself can never abort an update
mid-chain. A restart-only bounce does not call it at all: it checks nothing out
and syncs nothing, so it has no new install to claim, and writing the bookmark
there would assert one that did not happen.

The bookmark is HEAD as it stands *now*, resolved here rather than passed in,
because the chain does not always know the sha as a literal: a watchdog self-heal
checks out `origin/<track>` and only git can say what that landed on. Called after
`uv sync` succeeds, which is the same point the in-process path records it — the
bookmark means "this commit is fully installed", so it must not be written before
the install it claims.

Fail-soft in the chain (`|| ver>nul`), like the lease verbs: an unwritable bookmark
costs a redundant `uv sync` on the next start, and must never take down an update.
Exit code 0 on success, 1 on any failure.
"""

from __future__ import annotations

import subprocess
import sys

_REV_PARSE_TIMEOUT_S = 10.0


def _main() -> int:
    from cli.commands._repo import _repo_root
    from shared.gitenv import git_env
    from shared.source_integrity import set_installed

    repo = _repo_root()
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            env=git_env(),
            timeout=_REV_PARSE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"_installed_sha: git rev-parse failed: {exc}", file=sys.stderr)
        return 1
    sha = head.stdout.strip()
    if head.returncode != 0 or not sha:
        print(
            f"_installed_sha: git rev-parse HEAD in {repo} returned rc={head.returncode}: "
            f"{head.stderr.strip()}",
            file=sys.stderr,
        )
        return 1
    set_installed(sha)
    print(f"[updater] installed_sha = {sha[:7]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
