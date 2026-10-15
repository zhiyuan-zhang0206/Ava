from __future__ import annotations

from base.sessions.pty.screen import DEFAULT_COLS, DEFAULT_ROWS, PtyScreen


def _feed(screen: PtyScreen, text: str) -> None:
    screen.feed(text.replace("\n", "\r\n").encode("utf-8"))


def test_visible_screen_renders_current_rows() -> None:
    s = PtyScreen(cols=20, rows=5)
    _feed(s, "line one\nline two\n")
    out = s.render(scrollback=False)
    rows = out.split("\n")
    assert rows[0] == "line one"
    assert rows[1] == "line two"
    assert rows[2:] == [""] * 3
    assert out.endswith("\n")


def test_scrollback_render_returns_history_tail() -> None:
    s = PtyScreen(cols=20, rows=3, scrollback=10)
    for i in range(6):
        _feed(s, f"row-{i}")
        if i < 5:
            _feed(s, "\n")
    out = s.render(lines=200, scrollback=True)
    assert out.split("\n") == ["row-0", "row-1", "row-2", "row-3", "row-4", "row-5"]
    capped = s.render(lines=2, scrollback=True)
    assert capped.split("\n")[0] == "row-4"


def test_scrollback_tail_ignores_empty_screen_padding() -> None:
    s = PtyScreen(cols=20, rows=DEFAULT_ROWS)
    _feed(s, "older row\nnewest row")

    captured = s.render(lines=25, scrollback=True)

    assert "older row" in captured
    assert "newest row" in captured
    assert s.render(lines=1, scrollback=True) == "newest row"


def test_scrollback_render_empty_screen_is_empty() -> None:
    assert PtyScreen(cols=20, rows=5).render(lines=25, scrollback=True) == ""


def test_scrollback_render_keeps_history_before_visible_content() -> None:
    s = PtyScreen(cols=20, rows=3, scrollback=10)
    _feed(s, "row-0\nrow-1\nrow-2\nrow-3")
    _feed(s, "\x1b[2J\x1b[Htop")

    assert s.render(lines=2, scrollback=True).split("\n") == ["row-0", "top"]
    assert s.render(lines=1, scrollback=True) == "top"


def test_incremental_utf8_split_across_reads() -> None:
    s = PtyScreen(cols=20, rows=5)
    b = "\u4f60".encode()
    s.feed(b[:1])
    s.feed(b[1:])
    _feed(s, "\u597d\u4e16\u754c")
    out = s.render(scrollback=False)
    assert "\u4f60\u597d\u4e16\u754c" in out
    assert "\ufffd" not in out


def test_tui_cursor_addressed_redraw() -> None:
    s = PtyScreen(cols=20, rows=8)
    _feed(s, "\x1b[2J\x1b[HHEADER-LINE\x1b[4;3HMID-CELL\x1b[8;1HBOTTOM")
    out = s.render(scrollback=False)
    rows = out.split("\n")
    assert rows[0].startswith("HEADER-LINE")
    assert rows[3] == "  MID-CELL"
    assert rows[7].startswith("BOTTOM")
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
    assert len(s._raw) <= 100
    assert bytes(s._raw).endswith(b"b" * 60)


def test_raw_tail_is_line_aware_fallback() -> None:
    s = PtyScreen(cols=20, rows=5)
    _feed(s, "one\ntwo\nthree")
    tail = s.raw_tail(lines=2)
    assert tail.split("\n") == ["two", "three"]


def test_default_geometry_matches_classic() -> None:
    assert (DEFAULT_COLS, DEFAULT_ROWS) == (120, 40)


def test_capture_survives_an_orphaned_wide_char_stub() -> None:
    s = PtyScreen(cols=20, rows=3)
    _feed(s, "\u4e2d")
    _feed(s, "\x1b[H" + "x")

    out = s.render(lines=3, scrollback=False)
    assert out.split("\n")[0] == "x"
    assert s.render(lines=3, scrollback=True) == "x"
    assert s.current_line() == "x"
