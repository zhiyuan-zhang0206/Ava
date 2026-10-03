"""PTY screen model — pyte terminal emulation over a raw byte stream.

The pty supervisor's reader thread receives raw bytes from the session's
master fd; this module turns that stream into the two views the capture
contract needs, in the shape the classic screen-capture contract defined them:

- **visible screen** (``scrollback=False``): the current screenful, with
  trailing whitespace stripped per row — verified byte-for-byte against
  the classic ``capture-pane -p`` (which trims row padding; empty rows
  below the content survive as empty lines, so the rendered text ends in
  newlines exactly like the classic screen's);
- **screen + scrollback** (``scrollback=True``): the visible screen plus
  the lines that scrolled off it (pyte's history), tail-capped at the
  requested line count, mirroring ``capture-pane -p -S -<lines>``.

Three jobs sit between the bytes and pyte:

- **incremental UTF-8 decoding** — the master delivers bytes; a multi-byte
  sequence split across two reads must not render as U+FFFD, so an
  incremental decoder feeds pyte text while the raw bytes stay untouched;
- **raw byte ring buffer** — the full transcript since spawn (capped), the
  byte-exact record the screen model cannot reconstruct (escape sequences
  consumed, overwritten cells); also the degraded-capture fallback;
- **thread safety** — the reader thread feeds while the daemon's request
  threads resize / capture, so every access sits under one lock.

pyte itself is a faithful VT emulator: ANSI color/attribute escapes are
consumed (text renders clean), cursor-addressed full-screen programs
(top/vim-class TUIs) draw into the right cells, and split escape
sequences across ``feed()`` calls are handled by its state machine.
"""

from __future__ import annotations

import codecs
import threading

import pyte

# Terminal geometry defaults — match the classic default window size, which is
# what the PTY-hosted shells have been running at.
from base.sessions.pty.paths import (  # single definition, stdlib-only home
    DEFAULT_COLS,
    DEFAULT_ROWS,
)

_ = (DEFAULT_COLS, DEFAULT_ROWS)  # re-exported names (existing importers)

# Scrollback kept by the screen model, matching the classic default history-limit.
_SCROLLBACK_LINES = 2000

# Cap on the raw transcript ring buffer (bytes, not lines).
_RAW_RING_BYTES = 512 * 1024


def _history_line_text(line: dict[int, pyte.screens.Char]) -> str:
    """Render one pyte history line (a sparse ``{col: Char}`` map) to text.

    History lines are stored as column-indexed character maps; text is the
    characters at every column up to the last written one, trailing
    whitespace stripped like a display row.
    """
    if not line:
        return ""
    width = max(line) + 1
    return "".join(line[i].data for i in range(width)).rstrip()


def _heal_orphan_stubs(screen: pyte.HistoryScreen) -> None:
    """Put a space into every empty-data cell of the visible buffer.

    ``Screen.draw`` writes an empty stub cell after a two-cell character.
    Overwriting only one cell of the pair (a cursor-addressed redraw in a
    full-screen TUI) leaves the empty stub standing on its own, and
    ``Screen.display`` then fails on it — ``wcwidth(char[0])`` on the empty
    string raises ``IndexError: string index out of range`` (pyte 0.8.2), which
    surfaced as "internal error in capture" for the whole session (2026-10-03,
    codex sessions on company-mini). A stub slot still preceded by its two-cell
    character is skipped by ``display`` either way, so a space is invisible
    there; for an orphaned half it is the blank a real terminal shows.
    """
    for y in range(screen.lines):
        row = screen.buffer[y]
        for x, cell in list(row.items()):
            if not cell.data:
                row[x] = cell._replace(data=" ")


class PtyScreen:
    """The per-session terminal model: pyte screen + scrollback, fed bytes.

    All methods are safe to call from any thread; the reader thread is the
    sole ``feed()`` caller, resize/capture come from the daemon's request
    threads.
    """

    def __init__(
        self,
        cols: int = DEFAULT_COLS,
        rows: int = DEFAULT_ROWS,
        *,
        scrollback: int = _SCROLLBACK_LINES,
        raw_cap: int = _RAW_RING_BYTES,
    ) -> None:
        self._screen = pyte.HistoryScreen(cols, rows, history=scrollback)
        self._stream = pyte.Stream(self._screen)
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._raw = bytearray()
        self._raw_cap = raw_cap
        self._lock = threading.Lock()

    # -- feeding ------------------------------------------------------------
    def feed(self, data: bytes) -> None:
        """Ingest raw bytes from the master fd (reader thread).

        Keeps the raw ring buffer, incrementally decodes UTF-8 (a sequence
        split across reads survives), and feeds the decoded text to pyte.
        """
        with self._lock:
            self._raw.extend(data)
            if len(self._raw) > self._raw_cap:
                del self._raw[: len(self._raw) - self._raw_cap]
            text = self._decoder.decode(data)
            if text:
                self._stream.feed(text)

    # -- geometry -----------------------------------------------------------
    def resize(self, rows: int, cols: int) -> None:
        """Resize the screen model (call alongside the pty TIOCSWINSZ)."""
        with self._lock:
            self._screen.resize(rows, cols)

    # -- capture ------------------------------------------------------------
    def render(self, lines: int = 200, *, scrollback: bool = True) -> str:
        """Render captured text, classic ``capture-pane`` semantics.

        ``scrollback=True`` strips trailing empty rows from history + visible
        screen before returning the last ``lines`` rows (``-S -<lines>``).
        ``False`` returns the visible screen only (``-p``), preserving its
        trailing empty rows so it ends in newlines like the classic screen.
        Rows are stripped of trailing whitespace and joined with ``\n``.
        """
        with self._lock:
            if scrollback:
                rows = [
                    _history_line_text(ln)
                    for ln in list(self._screen.history.top) + list(self._screen.history.bottom)
                ]
                rows += [row.rstrip() for row in self._display()]
                while rows and rows[-1] == "":
                    rows.pop()
                return "\n".join(rows[-lines:])
            return "\n".join(row.rstrip() for row in self._display())

    def _display(self) -> list[str]:
        """``Screen.display`` after healing orphan spare cells (see above)."""
        _heal_orphan_stubs(self._screen)
        return self._screen.display

    def current_line(self) -> str:
        """The line the cursor is on (the prompt line of an idle shell).

        The ready signal for an initial-command submit: bash prints its
        prompt only after setting its terminal modes, so the cursor line
        ending in `$` / `#` means a write will survive the shell's own
        tcsetattr(TCSAFLUSH). ``render()`` cannot serve this — it returns
        the LAST display row, which is empty for a shell sitting at its
        prompt on row 0.
        """
        with self._lock:
            return self._display()[self._screen.cursor.y].rstrip()

    def raw_tail(self, lines: int = 200) -> str:
        """Degraded capture from the raw byte ring buffer (no screen model).

        The fallback when the pyte model is unavailable or suspect: the last
        ``lines`` decoded line-ends of the raw transcript. Escape sequences
        are NOT stripped and cursor movements are not applied — this is the
        byte-faithful record, not a screen render; prefer ``render`` unless
        the screen model is known bad.
        """
        with self._lock:
            text = bytes(self._raw).decode("utf-8", errors="replace")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return "\n".join(text.split("\n")[-lines:])
