"""`ava.ui.show` / `.serve` / `.close` SDK entry point tests (R3 door ③).

show/close go through in-process FastAPI TestClient: SDK calls hit gateway
endpoints directly, using a real ava_test DB to exercise the full wire
protocol. serve() declares the page (writes the agent_pages row + records
serve_dir) and waits for the page_server daemon's server to answer on the
port — in tests, a stub HTTP server stands in for the daemon's spawn.

(`ava.ui.notify` is registered by the ava_fleet plugin; tests are in
tests/agent/test_ava_fleet_plugin.py.)
"""

from __future__ import annotations

import http.server
import math
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

import ava
from ava.ui import InvalidPageName, PageClosed
from gateway.app import app
from shared.machine import reset_identity, set_identity
from tests.conftest import spawn_agent

_HOST = "127.0.0.1"  # loopback — the single-box posture the SDK registers (audit P1-4: only loopback / the agent's own machine are legal proxy targets)


@pytest.fixture(autouse=True)
def _sdk_via_inprocess_gateway(monkeypatch: pytest.MonkeyPatch):

    set_identity(host=_HOST)
    with TestClient(app, base_url="http://test-gateway") as tc:
        monkeypatch.setattr("ava._gateway_client._client", tc)
        yield
    reset_identity()


def _open_pages(db: psycopg.Connection, agent_id: int) -> list[tuple[Any, ...]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT name, port, title, serve_dir FROM agent_pages "
            "WHERE agent_id = %s AND closed_at IS NULL ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()


class _StubPageServer:
    """A real HTTP server answering /health — the stand-in for what the
    page_server daemon spawns for an open row."""

    def __init__(self, host: str, port: int = 0) -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/health":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"ok:stub")
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                pass

        class _ReuseServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True

        self._server = _ReuseServer((host, port), Handler)
        self.port = self._server.server_address[1]
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _StubPageServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


class _SilentServer:
    """Reserve a loopback port while deliberately withholding an HTTP response."""

    def __init__(self, host: str, port: int = 0) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind((host, port))
        self._socket.listen()
        self.port = self._socket.getsockname()[1]

    def __enter__(self) -> _SilentServer:
        return self

    def __exit__(self, *exc: object) -> None:
        self._socket.close()


class TestShow:
    def test_show_registers_page(self, db_conn: psycopg.Connection) -> None:
        ava._boot._agent_id = spawn_agent()
        port = 10000 + ava._boot.agent_id()  # default reserved port
        page = ava.ui.show("cleanup", title="Picker")
        assert page.name == "cleanup"
        assert page.port == port
        assert page.title == "Picker"
        assert page.url == f"http://test-gateway.invalid:8000/pages/{ava._boot.agent_id()}-cleanup/"
        db_conn.rollback()
        assert _open_pages(db_conn, ava._boot.agent_id()) == [("cleanup", port, "Picker", None)]

    def test_show_same_name_auto_closes_previous(self, db_conn: psycopg.Connection) -> None:
        """Single page per agent: re-showing auto-closes the old page."""
        ava._boot._agent_id = spawn_agent()
        port = 10000 + ava._boot.agent_id()
        ava.ui.show("p1")
        ava.ui.show("p2")
        db_conn.rollback()
        # Only p2 is open; p1 was auto-closed (single page per agent).
        assert _open_pages(db_conn, ava._boot.agent_id()) == [("p2", port, None, None)]

    def test_show_custom_port(self, db_conn: psycopg.Connection) -> None:
        """An explicit port overrides the default reserved one."""
        ava._boot._agent_id = spawn_agent()
        page = ava.ui.show("custom", port=13579, title="Custom")
        assert page.port == 13579
        assert page.url == f"http://test-gateway.invalid:8000/pages/{ava._boot.agent_id()}-custom/"
        db_conn.rollback()
        assert _open_pages(db_conn, ava._boot.agent_id()) == [("custom", 13579, "Custom", None)]

    def test_show_invalid_name_raises(self) -> None:
        ava._boot._agent_id = spawn_agent()
        with pytest.raises(InvalidPageName):
            ava.ui.show("bad/name")
        with pytest.raises(InvalidPageName):
            ava.ui.show("")

    def test_show_passes_integer_ttl_to_gateway(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ava._boot._agent_id = spawn_agent()
        captured: dict[str, object] = {}
        monkeypatch.setattr(ava._gateway_client, "list_open_pages", lambda _aid: [])  # pyright: ignore[reportUnknownArgumentType]

        def _register(agent_id: int, **kwargs: object) -> dict[str, object]:
            captured.update(agent_id=agent_id, **kwargs)
            return {
                "id": 1,
                "name": "timed",
                "port": 12000,
                "title": None,
                "url": "http://gateway/page",
            }

        monkeypatch.setattr(ava._gateway_client, "register_page", _register)
        ava.ui.show("timed", port=12000, ttl=12.9)
        assert captured["ttl_seconds"] == 12

    def test_show_without_ttl_omits_gateway_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ava._boot._agent_id = spawn_agent()
        captured: dict[str, object] = {}
        monkeypatch.setattr(ava._gateway_client, "list_open_pages", lambda _aid: [])  # pyright: ignore[reportUnknownArgumentType]

        def _register(_agent_id: int, **kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            return {
                "id": 1,
                "name": "plain",
                "port": 12000,
                "title": None,
                "url": "http://gateway/page",
            }

        monkeypatch.setattr(ava._gateway_client, "register_page", _register)
        ava.ui.show("plain", port=12000)
        assert "ttl_seconds" not in captured


@pytest.mark.parametrize("ttl", [0.0, -1.0, math.inf, math.nan])
@pytest.mark.parametrize("api", ["show", "serve", "serve_markdown"])
def test_page_apis_reject_invalid_ttl(api: str, ttl: float, tmp_path: Path) -> None:
    ava._boot._agent_id = spawn_agent()
    call = getattr(ava.ui, api)
    args = ("content", "page") if api == "serve_markdown" else (str(tmp_path), "page")
    if api == "show":
        args = ("page",)
    with pytest.raises(ValueError, match="ttl"):
        call(*args, ttl=ttl)


class TestClose:
    def test_close_marks_closed(self, db_conn: psycopg.Connection) -> None:
        ava._boot._agent_id = spawn_agent()
        ava.ui.show("p")
        ava.ui.close("p")
        db_conn.rollback()
        assert _open_pages(db_conn, ava._boot.agent_id()) == []

    def test_close_missing_raises_page_closed(self) -> None:
        ava._boot._agent_id = spawn_agent()
        with pytest.raises(PageClosed):
            ava.ui.close("never-registered")


class TestServe:
    """serve() declares the page (row + serve_dir) and waits for the
    page_server daemon's server to answer on the port — no process is
    spawned by the SDK anymore (R3 door ③)."""

    @pytest.fixture(autouse=True)
    def _loopback_identity(self):
        """serve() polls reachable_host() — pin it to loopback so the stub
        server can actually bind it."""
        set_identity(host="127.0.0.1")
        yield
        reset_identity()

    def test_serve_registers_serve_dir(self, db_conn: psycopg.Connection, tmp_path: Path) -> None:
        ava._boot._agent_id = spawn_agent()
        (tmp_path / "index.html").write_text("<h1>served</h1>", encoding="utf-8")
        with _StubPageServer("127.0.0.1") as stub:
            page = ava.ui.serve(str(tmp_path), "srv", port=stub.port, title="Served")
        assert page.name == "srv"
        assert page.port == stub.port
        db_conn.rollback()
        assert _open_pages(db_conn, ava._boot.agent_id()) == [
            ("srv", stub.port, "Served", str(tmp_path))
        ]

    def test_serve_passes_ttl(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        ava._boot._agent_id = spawn_agent()
        captured: dict[str, object] = {}
        monkeypatch.setattr(ava._gateway_client, "list_open_pages", lambda _aid: [])  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(ava.ui, "_wait_until_serving", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]

        def _register(_agent_id: int, **kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            return {
                "id": 1,
                "name": "srv-ttl",
                "port": 12000,
                "title": None,
                "url": "http://gateway/page",
            }

        monkeypatch.setattr(ava._gateway_client, "register_page", _register)
        ava.ui.serve(str(tmp_path), "srv-ttl", port=12000, ttl=30)
        assert captured["ttl_seconds"] == 30

    def test_serve_waits_for_server_to_come_up(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        """serve() polls until the daemon's server answers (stub starts late)."""
        ava._boot._agent_id = spawn_agent()
        (tmp_path / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
        with _StubPageServer("127.0.0.1") as stub:
            page = ava.ui.serve(str(tmp_path), "late", port=stub.port)
        assert page.port == stub.port

    def test_serve_times_out_without_daemon(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No server on the port -> serve() raises after the wait window
        (the page row stays registered; the daemon keeps retrying)."""
        import ava.ui as ui_mod

        ava._boot._agent_id = spawn_agent()
        (tmp_path / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
        monkeypatch.setattr(ui_mod, "_SERVE_READY_TIMEOUT_S", 0.5)
        with (
            _SilentServer("127.0.0.1") as silent,
            pytest.raises(ui_mod.PageError, match="did not come up"),
        ):
            ava.ui.serve(str(tmp_path), "nod", port=silent.port)
        db_conn.rollback()
        # The declaration is durable even when the server is not up yet.
        assert _open_pages(db_conn, ava._boot.agent_id()) == [
            ("nod", silent.port, None, str(tmp_path))
        ]

    def test_serve_same_name_replaces(self, db_conn: psycopg.Connection, tmp_path: Path) -> None:
        ava._boot._agent_id = spawn_agent()
        (tmp_path / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
        with _StubPageServer("127.0.0.1") as stub:
            ava.ui.serve(str(tmp_path), "once", port=stub.port)
            ava.ui.serve(str(tmp_path), "twice", port=stub.port)
        db_conn.rollback()
        assert [r[0] for r in _open_pages(db_conn, ava._boot.agent_id())] == ["twice"]


class TestServeMarkdown:
    """serve_markdown materializes content to disk and lets the daemon
    serve it; close() cleans the temp dir up."""

    @pytest.fixture(autouse=True)
    def _loopback_identity(self):
        set_identity(host="127.0.0.1")
        yield
        reset_identity()

    def test_markdown_widget_dir_resolves(self) -> None:
        from ava.ui import _markdown_widget_dir

        widget_dir = _markdown_widget_dir()
        assert (widget_dir / "md.html").is_file()
        assert (widget_dir / "vendor").is_dir()

    def test_serve_markdown_materializes_and_closes(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ava._boot._agent_id = spawn_agent()
        with _StubPageServer("127.0.0.1") as stub:
            ava.ui.serve_markdown("# Hello", "md", port=stub.port)
        db_conn.rollback()
        rows = _open_pages(db_conn, ava._boot.agent_id())
        assert len(rows) == 1
        serve_dir = Path(rows[0][3])
        assert (serve_dir / "index.html").is_file(), "content materialized to disk"
        assert serve_dir.name.startswith("ava_md_")

        # close() unregisters AND removes the temp dir.
        ava.ui.close("md")
        assert not serve_dir.exists(), "close() cleans up the markdown temp dir"
        db_conn.rollback()
        assert _open_pages(db_conn, ava._boot.agent_id()) == []

    def test_serve_markdown_forwards_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def _serve(
            _dir: str,
            name: str,
            port: int | None = None,
            title: str | None = None,
            *,
            ttl: float | None = None,
        ) -> ava.ui.Page:
            captured.update(name=name, port=port, title=title, ttl=ttl)
            return ava.ui.Page(id=1, name=name, port=port or 12000, title=title, url="x")

        monkeypatch.setattr(ava.ui, "serve", _serve)
        ava.ui.serve_markdown("# Hi", "md-ttl", ttl=45)
        assert captured["ttl"] == 45

    def test_render_markdown_fenced_code_in_list_item(self) -> None:
        """A fenced code block nested inside a list item must render as a
        real <pre><code> block (python-markdown's fenced_code extension
        leaked it through as literal text — user bug #1268)."""
        from ava.ui import _render_markdown

        md = """- **Python snippet**\uff1a

  ```python
  def finish_current_turn():
      return "hello_world"
  ```
"""
        html = _render_markdown(md)
        assert '<pre><code class="language-python">' in html
        assert "finish_current_turn" in html
        assert "```" not in html, "fence markers must not leak through"

    def test_render_markdown_intraword_underscore_is_literal(self) -> None:
        """CommonMark: `_` between word characters is not emphasis
        (finish_current_turn must not render as finish<em>current</em>turn)."""
        from ava.ui import _render_markdown

        html = _render_markdown(
            "\u8c03\u7528 finish_current_turn \u7ed3\u675f\u672c\u8f6e\uff1bfoo_bar_baz\u3002"
        )
        assert "<em>" not in html
        assert "finish_current_turn" in html
        assert "foo_bar_baz" in html

    def test_render_markdown_standalone_fenced_code_with_lang(self) -> None:
        from ava.ui import _render_markdown

        html = _render_markdown("```python\nprint(1)\n```")
        assert '<pre><code class="language-python">' in html

    def test_render_markdown_gfm_table(self) -> None:
        from ava.ui import _render_markdown

        md = """| a | b |
|---|---|
| 1 | 2 |"""
        html = _render_markdown(md)
        assert "<table>" in html
        assert "<th>a</th>" in html

    def test_render_markdown_strikethrough(self) -> None:
        """GFM strikethrough was silently missing under python-markdown."""
        from ava.ui import _render_markdown

        html = _render_markdown("~~\u5220\u9664\u7ebf~~ \u548c ~~done~~")
        assert "<s>\u5220\u9664\u7ebf</s>" in html
        assert "<s>done</s>" in html

    def test_render_markdown_math_delimiters_preserved(self) -> None:
        """LaTeX `$...$`/`$$...$$` is left untouched for client-side KaTeX."""
        from ava.ui import _render_markdown

        html = _render_markdown(
            "\u884c\u5185 $E = mc^2$\uff0c\u5757\u7ea7\uff1a\n\n$$\\int_0^1 x^2 dx$$"
        )
        assert "$E = mc^2$" in html
        assert "$$\\int_0^1 x^2 dx$$" in html
        assert "<em>" not in html

    def test_render_markdown_nested_list(self) -> None:
        from ava.ui import _render_markdown

        md = """1. \u7b2c\u4e00\u9879
   - \u5b50\u9879 A
   - \u5b50\u9879 B
2. \u7b2c\u4e8c\u9879
   1. \u5b59\u9879 1"""
        html = _render_markdown(md)
        assert html.count("<li>") >= 4

    def test_render_markdown_link_and_image(self) -> None:
        from ava.ui import _render_markdown

        md = "[\u94fe\u63a5](https://github.com) \u548c ![alt](https://example.com/x.png)"
        html = _render_markdown(md)
        assert '<a href="https://github.com">' in html
        assert '<img src="https://example.com/x.png" alt="alt"' in html

    def test_render_markdown_inline_code_and_emphasis(self) -> None:
        from ava.ui import _render_markdown

        html = _render_markdown("`code_here`\u3001*italic*\u3001**bold**")
        assert "<code>code_here</code>" in html
        assert "<em>italic</em>" in html
        assert "<strong>bold</strong>" in html
