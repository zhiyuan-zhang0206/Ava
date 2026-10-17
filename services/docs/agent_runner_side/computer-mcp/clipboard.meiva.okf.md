---
type: doc
title: "Computer-mcp — clipboard"
description: "Plain-text clipboard read and write through the permissions helper, and why the text never reaches an audit row, a log line or an error."
tags:
- services
- computer-use
---

# Clipboard

`clipboard_read()` returns `{text, types, length, truncated}`: the plain text
(`public.utf8-plain-text`), the list of types currently on the clipboard, the
text's length in code points (Python `len`) and whether the text was cut at
1,000,000 characters. A clipboard with no plain text returns `text: null` and
`length: 0`, never an invented empty string; a present but empty text is `""`.
`clipboard_write(text)` replaces the clipboard with that one plain-text item
(every other type is discarded) and returns `{written}`, the length only. Both
use `NSPasteboard.general` in the helper, which runs in the user's GUI session;
no NSApplication is needed. Neither needs Accessibility. Input is strict: a
missing, non-string or over-long text and any unknown field fail before the
clipboard is touched, and a write the system refuses is an error.

Privacy: the clipboard may hold a password. The text is never put into an audit
row, a log line or an error: the audit row's `coords` field carries
`"<n> chars"` (or `"no text"` for a read), helper errors name the field but not
the value, `ClipboardWriteInput` hides pydantic's echoed input, and the write
reply carries a length only. The text does reach the calling agent (a read
returns it) and the agent's own execute_code source, which this layer does not
govern. `clipboard_write` changes user state, so the corner fail-safe refuses it
like other desktop-changing methods; `clipboard_read` is observation and is not
stopped. Neither requests a capture badge. The helper advertises
`ping.clipboard_v1`; the client (`base/host/permissions_helper/client_stateful.py`) refuses both calls
on a helper without it.
Whether macOS prompts the user to allow a background program to read another
app's clipboard (a privacy gate newer macOS releases are introducing) is not
established by the harness and needs a real-desktop check.
