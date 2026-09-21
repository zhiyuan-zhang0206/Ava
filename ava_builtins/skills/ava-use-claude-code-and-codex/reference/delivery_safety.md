# Delivery safety — long text into a terminal UI

`ava.shell.sessions.send` writes text into a session as a raw burst. A
terminal UI that folds long bursts while rendering can silently deliver only
part of it: Claude Code folds a single burst over ~1022 characters into
collapsed paste fragments, and a submission that lands while a collapsed
fragment sits next to a raw tail drops the fragment's content — the message
still looks submitted while its head is missing (task #4364). Codex (0.155.1)
showed no folding: raw sends up to 15K characters arrived byte-exact (task
#4365).

## Rules of thumb

- **Short steering messages (nudges etc.)** — nothing special; a raw send
  up to one chunk is delivered as-is.
- **Long text (over roughly 1K characters) into a TUI** — either
  - wrap it so the target takes it as one atomic paste: send
    `ESC[200~` + text + `ESC[201~`, stripping any such markers inside the
    text first, or
  - write the text to a file and send a short pointer instead (path + what
    to do) — the portable fallback that works for any receiver.
- **Only wrap when the receiver consumes bracketed paste** — the two coding
  TUIs here and interactive shells do; a target that does not would insert
  the escape bytes literally.

The spawn scripts apply this themselves: `spawn_claude.py` sends its
bootstrap as one bracketed paste (`_bracketed_paste` / `_send_bootstrap`),
and the rebuild-resend recovery uses the same helper. If you add another
long-message send site, follow one of the two patterns above.

Platform stance (evaluation: task #4366): no platform-level auto-wrap — the
rule lives at the sender.
