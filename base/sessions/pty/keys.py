"""Screen-vocabulary key names to the bytes a terminal receives.

The classic send-keys vocabulary (the prototype `_KEYMAP` with the canonical
names added: BSpace/DC/IC/PPage/NPage/BTab, M-<x>, C-Space/C-/). The client
translates before it dials the service, so the service only ever writes bytes.
"""

from __future__ import annotations

import re

_KEYMAP = {
    "Enter": b"\r",
    "Space": b" ",
    "Tab": b"\t",
    "BTab": b"\x1b[Z",
    "Escape": b"\x1b",
    "Esc": b"\x1b",
    "Backspace": b"\x7f",
    "BSpace": b"\x7f",
    "Delete": b"\x1b[3~",
    "DC": b"\x1b[3~",
    "Insert": b"\x1b[2~",
    "IC": b"\x1b[2~",
    "Up": b"\x1b[A",
    "Down": b"\x1b[B",
    "Right": b"\x1b[C",
    "Left": b"\x1b[D",
    "Home": b"\x1b[H",
    "End": b"\x1b[F",
    "PageUp": b"\x1b[5~",
    "PPage": b"\x1b[5~",
    "PageDown": b"\x1b[6~",
    "NPage": b"\x1b[6~",
    "S-Up": b"\x1b[1;2A",
    "S-Down": b"\x1b[1;2B",
    "S-Right": b"\x1b[1;2C",
    "S-Left": b"\x1b[1;2D",
    "F1": b"\x1bOP",
    "F2": b"\x1bOQ",
    "F3": b"\x1bOR",
    "F4": b"\x1bOS",
    "F5": b"\x1b[15~",
    "F6": b"\x1b[17~",
    "F7": b"\x1b[18~",
    "F8": b"\x1b[19~",
    "F9": b"\x1b[20~",
    "F10": b"\x1b[21~",
    "F11": b"\x1b[23~",
    "F12": b"\x1b[24~",
}

# C-<x> for every printable control char spelled that way (C-a .. C-z,
# C-@ C-[ C-] C-^ C-_ C-?); the rest are explicit below.
_CTRL_RE = re.compile(r"^C-([a-zA-Z@\[\]^_?])$")
_META_RE = re.compile(r"^M-(.)$")
_CTRL_EXTRA = {
    "C-Space": b"\x00",
    "C-@": b"\x00",
    "C-/": b"\x1f",
    "C-\\": b"\x1c",
}


def keys_to_bytes(keys: tuple[str, ...]) -> bytes:
    """Translate screen send-keys key names to the bytes to write to the pty.

    screen semantics: a single character is typed literally; a known key name
    (C-c, Up, Escape, ...) translates to its control/escape bytes; an
    unknown name is typed as literal text (the screen treats unrecognized keys as
    strings of characters).
    """
    out = b""
    for key in keys:
        if len(key) == 1:
            out += key.encode("utf-8")
            continue
        if key in _CTRL_EXTRA:
            out += _CTRL_EXTRA[key]
            continue
        m = _CTRL_RE.match(key)
        if m:
            ch = m.group(1)
            code = ord(ch.upper()) - ord("@") if ch != "?" else 0x7F
            out += bytes([code])
            continue
        m = _META_RE.match(key)
        if m:
            out += b"\x1b" + m.group(1).encode("utf-8")
            continue
        out += _KEYMAP.get(key, key.encode("utf-8"))
    return out
