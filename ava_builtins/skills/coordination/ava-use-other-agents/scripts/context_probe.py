"""Reference: read how much context the coding agent has left, from a screen capture.

Neither tool feeds the *model* its own context usage, so you read it yourself. The
reliable read is an on-demand command, not the footer (the footer's context field
is configurable and off by default in some builds):

- `codex`: send `/status`; it prints `Context window: NN% left (X used / Y)`.
- `claude`: `/context` prints a grid (no single number); a custom statusline may
  show a `ctx:NN%` figure (percent USED) you can scrape instead.

`parse_context_left(screen)` takes the captured text -- you send the command, then
`ava.shell.sessions.capture(id, scrollback=False)` the result -- and returns the
percent of context LEFT as a float, or None if no known indicator is present. It
does no session I/O, so it is trivial to check against a real capture.
"""

import re

# Formats confirmed on a live host (version-sensitive -- re-check the wording on
# the real machine before relying on a match):
#   codex /status panel : "Context window:  97% left (19.3K used / 258K)"
#   codex footer        : "73% context left"   (only when the statusline shows it)
#   claude statusline   : "ctx:4%"             (percent USED; custom statusline)
_CODEX_STATUS = re.compile(r"context window:\s*(\d+)%\s*left", re.IGNORECASE)
_CODEX_FOOTER = re.compile(r"(\d+)%\s*context left", re.IGNORECASE)
_CLAUDE_USED = re.compile(r"ctx:\s*(\d+)%", re.IGNORECASE)


def parse_context_left(screen: str) -> float | None:
    """Return percent of context LEFT from a captured screen, or None if unknown."""
    for pat in (_CODEX_STATUS, _CODEX_FOOTER):
        m = pat.search(screen)
        if m:
            return float(m.group(1))
    used = _CLAUDE_USED.search(screen)
    if used:
        return 100.0 - float(used.group(1))
    return None
