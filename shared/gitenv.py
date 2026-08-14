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
over `core.sshCommand`, and the fleet's Windows agent-runner carries a global
`~/.gitconfig` with `core.sshCommand = "C:/Windows/System32/OpenSSH/ssh.exe"`
(embedded quotes and all) left behind by a hand-run debug script during the
Windows port. It is masked today only because the repo-local `.git/config`
overrides it. Setting the env var neutralises that at the source without editing
anyone's global config.

`ConnectTimeout` is worth setting but is **not** the bound. On the Windows box,
all 66 orphaned `ssh.exe` processes had zero TCP connections — they never dialed
GitHub, so they were wedged locally *before* the connect that `ConnectTimeout`
governs (a 4-day-old `ssh-keyscan.exe` sat in the same state despite a hard
5-second internal timeout). The only real bound is the caller's:
`shared.proc.run_bounded`.
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
