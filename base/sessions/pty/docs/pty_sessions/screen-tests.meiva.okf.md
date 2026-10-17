---
type: doc
title: Screen fixture input
description: Terminal line-ending semantics used by screen-model tests.
---

# Screen fixture input

`tests/test_pty_sessions_screen.py` feeds CRLF to model the PTY wire stream
with its normal output line discipline. In pyte, LF moves down without returning
the cursor to column zero; substituting bare LF changes the terminal behavior
under test. This is a fixture premise, not a claim that every terminal mode
translates LF to CRLF.

## Shell output markers

`base/sessions/pty/tests/pty_shells.py:output_until` waits for a complete stripped output line.
A substring can match the terminal's echo of the typed command before the
command executes. The wait budget is a ceiling, not an unconditional sleep.
