"""The environment every Ava-initiated `git` call runs under: never prompt,
never hang.

A git subprocess Ava drives has no human behind it, so its two ways of waiting
forever are both pure liability:

- **credential / passphrase prompts.** `GIT_TERMINAL_PROMPT=0` turns a missing
  credential into an immediate error instead of a read from a terminal nobody is
  attached to (under a detached rollout session there is no terminal at all, and
  git blocks on it).
- **ssh.** `GIT_SSH_COMMAND` with `BatchMode=yes` (no interactive
  auth) + `ConnectTimeout=10` (bounded dial).

Why `GIT_SSH_COMMAND` specifically, and not a config edit: it takes precedence
over `core.sshCommand`, so a global `~/.gitconfig` that sets one cannot loosen
the bound, and nobody's global config is edited.

`ConnectTimeout` is worth setting but is **not** the bound: an ssh can wedge
locally *before* the connect that `ConnectTimeout` governs. The only real bound
is the caller's: `base.host.proc.run_bounded`.
"""

from __future__ import annotations

import os

# Bounded, non-interactive ssh. Kept as one string because that is git's
# interface (`GIT_SSH_COMMAND` is shell-parsed by git itself); bare `ssh` so the
# platform's own ssh on PATH is used.
_GIT_SSH_COMMAND = "ssh -o BatchMode=yes -o ConnectTimeout=10"


def git_env() -> dict[str, str]:
    """The process environment plus the non-interactive git knobs — pass as
    `env=` to any git subprocess Ava starts (network or local: a local command
    has no business prompting either).

    A copy, so a caller may add to it. Overrides an inherited
    `GIT_SSH_COMMAND` / `GIT_TERMINAL_PROMPT` rather than deferring to it: this
    is the posture Ava requires of its own git calls, not a default.
    """
    return os.environ | {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": _GIT_SSH_COMMAND,
    }
