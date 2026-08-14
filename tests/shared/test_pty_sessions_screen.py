"""Screen-model tests for shared.pty_sessions.screen — the pyte wrapper.

These exercise the PtyScreen in isolation (no daemon): incremental UTF-8
decode across reads, the raw byte ring buffer, screen-parity render semantics
(visible screen vs scrollback, trailing-whitespace trimming, trailing empty
rows), and the TUI behaviors the capture contract is defined against —
cursor-addressed full-screen redraws and ANSI attribute sequences.

Line endings are CRLF on the wire (the pty line discipline converts the
child's LF to CRLF), so feeds use "\r\n" — pyte models LF strictly as
move-down-without-carriage-return, exactly like the terminal it emulates.
"""

from __future__ import annotations

from shared.pty_sessions.screen import DEFAULT_COLS, DEFAULT_ROWS, PtyScreen


def _feed(screen: PtyScreen, text: str) -> None:
    screen.feed(text.replace("\n", "\r\n").encode("utf-8"))


def test_visible_screen_renders_current_rows() -> None:
    s = PtyScreen(cols=20, rows=5)
    _feed(s, "line one\nline two\n")
    out = s.render(scrollback=False)
    rows = out.split("\n")
    assert rows[0] == "line one"
    assert rows[1] == "line two"
    # screen parity: rows are trailing-trimmed; empty rows below the content
    # survive as empty lines, so the rendered text ends in newlines.
    assert rows[2:] == [""] * 3
    assert out.endswith("\n")


def test_scrollback_render_returns_history_tail() -> None:
    s = PtyScreen(cols=20, rows=3, scrollback=10)
    for i in range(6):
        _feed(s, f"row-{i}")
        if i < 5:
            _feed(s, "\n")
    # row-0..2 scrolled off into history; row-3..5 are visible; render is
    # history (oldest first) + display, so the full transcript in order.
    out = s.render(lines=200, scrollback=True)
    assert out.split("\n") == ["row-0", "row-1", "row-2", "row-3", "row-4", "row-5"]
    # lines cap: only the last `lines` rows are returned.
    capped = s.render(lines=2, scrollback=True)
    assert capped.split("\n")[0] == "row-4"


def test_incremental_utf8_split_across_reads() -> None:
    s = PtyScreen(cols=20, rows=5)
    # '你' is 3 UTF-8 bytes; split across two feeds must not render U+FFFD.
    b = "你".encode()
    s.feed(b[:1])
    s.feed(b[1:])
    _feed(s, "好世界")
    out = s.render(scrollback=False)
    assert "你好世界" in out
    assert "\ufffd" not in out


def test_tui_cursor_addressed_redraw() -> None:
    """A full-screen program (top/vim class) addresses cells directly; the
    visible screen must reflect the final layout, not the raw escape soup."""
    s = PtyScreen(cols=20, rows=8)
    _feed(s, "\x1b[2J\x1b[HHEADER-LINE\x1b[4;3HMID-CELL\x1b[8;1HBOTTOM")
    out = s.render(scrollback=False)
    rows = out.split("\n")
    assert rows[0].startswith("HEADER-LINE")
    assert rows[3] == "  MID-CELL"
    assert rows[7].startswith("BOTTOM")
    # No raw escape sequences leak into the render.
    assert "\x1b" not in out


def test_ansi_attributes_stripped_from_text() -> None:
    s = PtyScreen(cols=20, rows=5)
    _feed(s, "\x1b[1;31mbold-red\x1b[0m plain")
    out = s.render(scrollback=False)
    assert "bold-red plain" in out
    assert "\x1b" not in out


def test_resize_updates_geometry() -> None:
    s = PtyScreen(cols=20, rows=5)
    _feed(s, "wide-content")
    s.resize(10, 40)
    out = s.render(scrollback=False)
    assert out.split("\n")[0].startswith("wide-content")
    assert len(out.split("\n")) == 10


def test_raw_ring_buffer_caps_and_roundtrips() -> None:
    s = PtyScreen(cols=20, rows=5, raw_cap=100)
    _feed(s, "a" * 60)
    _feed(s, "b" * 60)
    assert len(s._raw) <= 100  # test observes the ring cap
    assert bytes(s._raw).endswith(b"b" * 60)


def test_raw_tail_is_line_aware_fallback() -> None:
    s = PtyScreen(cols=20, rows=5)
    _feed(s, "one\ntwo\nthree")
    tail = s.raw_tail(lines=2)
    assert tail.split("\n") == ["two", "three"]


def test_default_geometry_matches_classic() -> None:
    assert (DEFAULT_COLS, DEFAULT_ROWS) == (120, 40)
