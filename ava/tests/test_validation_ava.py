"""Argument validation of the `ava.files`, `ava.ui`, `ava.watcher`, `ava.web`, `ava.understand` and `ava.self` entry points."""

from __future__ import annotations

import contextlib
import importlib
from pathlib import Path
from typing import Any

import pytest

import ava
import ava.agent_identity
from ava import files as _files
from ava import ui as _ui
from ava import watcher as _watcher


class TestFilesEntries:
    def test_path_and_content_unwrap(self, tmp_path: Path) -> None:
        p = tmp_path / "x.txt"
        _files.write((str(p),), ("content",))  # pyright: ignore[reportArgumentType]
        assert _files.read((str(p),)) == "content"  # pyright: ignore[reportArgumentType]
        _files.append(str(p), ("more",))  # pyright: ignore[reportArgumentType]
        _files.edit(str(p), ("content",), ("CONTENT",))  # pyright: ignore[reportArgumentType]
        assert _files.read(str(p)) == "CONTENTmore"
        assert _files.glob((str(tmp_path / "*.txt"),)) == [p]  # pyright: ignore[reportArgumentType]
        _files.delete((str(p),))  # pyright: ignore[reportArgumentType]

    def test_path_objects_still_work(self, tmp_path: Path) -> None:
        """Zero regression: Path arguments keep working (str | Path params)."""
        p = tmp_path / "y.txt"
        _files.write(p, "ok")
        assert _files.read(p) == "ok"
        _files.delete(p)

    @pytest.mark.parametrize(
        ("call", "match"),
        [
            pytest.param(
                lambda: _files.write(("a", "b"), "x"),  # pyright: ignore[reportArgumentType]
                "path must be a string",
                id="multi-path",  # pyright: ignore[reportArgumentType]
            ),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: _files.write("x", ("a", "b")),  # pyright: ignore[reportArgumentType]
                "content must be a string",
                id="multi-content",
            ),  # pyright: ignore[reportArgumentType]
            pytest.param(lambda: _files.read(("a", "b")), "path must be a string", id="read-multi"),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: _files.edit("x", ("a", "b"), "c"),  # pyright: ignore[reportArgumentType]
                "old must be a string",
                id="edit-old",  # pyright: ignore[reportArgumentType]
            ),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: _files.delete(("a", "b")),  # pyright: ignore[reportArgumentType]
                "path must be a string",
                id="delete-multi",  # pyright: ignore[reportArgumentType]
            ),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: _files.read("x", start=("1",)),  # pyright: ignore[reportArgumentType]
                "start must be int",
                id="read-start",  # pyright: ignore[reportArgumentType]
            ),  # pyright: ignore[reportArgumentType]
        ],
    )
    def test_multi_element_and_wrong_types_type_error(
        self, tmp_path: Path, call: Any, match: str
    ) -> None:
        p = tmp_path / "z.txt"
        _files.write(str(p), "x")  # ensure the read anchor exists
        with pytest.raises(TypeError, match=match):
            call()


class TestUiEntries:
    def test_show_unwraps_name_and_title(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            _ui,
            "_register_page",
            lambda name, port, title, serve_dir, ttl: (  # pyright: ignore[reportUnknownArgumentType]
                seen.update(name=name, title=title, port=port, serve_dir=serve_dir, ttl=ttl)
                or object()
            ),
        )  # pyright: ignore[reportUnknownArgumentType]

        _ui.show(("mypage",), 9999, title=("My Page",))  # pyright: ignore[reportArgumentType]
        assert seen["name"] == "mypage"
        assert seen["title"] == "My Page"
        assert seen["port"] == 9999

    def test_serve_unwraps_dir_and_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(_ui, "_reject_foreign_port_occupant", lambda _port: None)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(_ui, "_wait_until_serving", lambda *_a, **_k: True)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(
            _ui,
            "_register_page",
            lambda name, port, title, serve_dir, ttl: (  # pyright: ignore[reportUnknownArgumentType]
                seen.update(name=name, serve_dir=serve_dir, port=port, title=title, ttl=ttl)
                or object()
            ),
        )  # pyright: ignore[reportUnknownArgumentType]

        _ui.serve((str(tmp_path),), ("served",), 9998)  # pyright: ignore[reportArgumentType]
        assert seen["name"] == "served"
        assert seen["port"] == 9998
        assert seen["serve_dir"] == str(Path(str(tmp_path)).resolve())

    def test_close_unwraps_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            _ui.gateway_client,
            "close_page",
            lambda _aid, name: seen.update(name=name),  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]

        _ui.close(("mypage",))  # pyright: ignore[reportArgumentType]
        assert seen["name"] == "mypage"

    @pytest.mark.parametrize(
        ("call", "match"),
        [
            pytest.param(
                lambda: _ui.show(("a", "b"), 9999),  # pyright: ignore[reportArgumentType]
                "name must be a string",
                id="show-multi",
            ),
            pytest.param(lambda: _ui.show("x", port=("80",)), "port must be int", id="show-port"),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: _ui.serve(("a", "b"), "n", 9999),  # pyright: ignore[reportArgumentType]
                "dir must be a string",
                id="serve-multi",  # pyright: ignore[reportArgumentType]
            ),  # pyright: ignore[reportArgumentType]
        ],
    )
    def test_multi_element_type_errors(self, call: Any, match: str) -> None:
        with pytest.raises(TypeError, match=match):
            call()


class TestWatcherEntries:
    def test_at_unwraps_when_message_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            _watcher,
            "_spawn",
            lambda code, _ttl, name, **kw: seen.update(name=name, code=code, **kw) or 1,  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]

        _watcher.at(("2030-01-01T00:00:00+08:00",), ("stand up",), name=("standup",))  # pyright: ignore[reportArgumentType]
        assert seen["name"] == "standup"
        # `message` is no longer passed to `_spawn` (ava/watcher.py) — it is
        # baked into the generated script by `build_at_script` instead.
        assert "stand up" in seen["code"]

    def test_launch_unwraps_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            _watcher,
            "_spawn",
            lambda _code, _ttl, name, **_kw: seen.update(ttl=_ttl, name=name) or 1,  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]

        _watcher.launch(("print(1)",), ("30m",), name=("x",))  # pyright: ignore[reportArgumentType]
        assert seen["ttl"] == 1800.0
        assert seen["name"] == "x"

    @pytest.mark.parametrize(
        ("call", "match"),
        [
            pytest.param(
                lambda: _watcher.at(("a", "b"), "m", name="n"),  # pyright: ignore[reportArgumentType]
                "when must be a string",
                id="when-multi",
            ),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: _watcher.at("2030-01-01T00:00:00+08:00", ("a", "b"), name="n"),  # pyright: ignore[reportArgumentType]
                "message must be a string",
                id="message-multi",
            ),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: _watcher.cron(("a", "b"), "m", name="n"),  # pyright: ignore[reportArgumentType]
                "expr must be a string",
                id="expr-multi",
            ),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: _watcher.launch("code", ("30m", "1h"), name="n"),  # pyright: ignore[reportArgumentType]
                "timeout must be a string",
                id="timeout-multi",
            ),  # pyright: ignore[reportArgumentType]
        ],
    )
    def test_multi_element_type_errors(self, call: Any, match: str) -> None:
        with pytest.raises(TypeError, match=match):
            call()


class TestWebEntries:
    def test_fetch_effort_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava import web

        async def fake_batch(
            targets: list[tuple[str, str]], fn: Any, max_concurrent: int | None
        ) -> list[Any]:
            return [fn(t) for t in targets]

        seen: dict[str, Any] = {}
        monkeypatch.setattr(web, "run_batch", fake_batch)
        monkeypatch.setattr(
            web,
            "_fetch_one",
            lambda *a: seen.update(max_chars=a[2], effort=a[3]) or "answer",  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]

        out = web.fetch([("https://example.com", "summarize")], effort=("low",))  # pyright: ignore[reportArgumentType]
        assert out == ["answer"]
        assert seen["effort"] == "low"

    def test_fetch_effort_multi_element_type_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava import web

        with pytest.raises(TypeError, match="effort must be a string"):
            web.fetch([("https://example.com", "summarize")], effort=("low", "high"))  # pyright: ignore[reportArgumentType]

    def test_search_count_never_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava import web

        async def fake_batch(items: list[str], fn: Any, max_concurrent: int | None) -> list[Any]:
            return [fn(i) for i in items]

        monkeypatch.setattr(web, "run_batch", fake_batch)
        monkeypatch.setattr(web, "_search_one", lambda *_a, **_k: [])  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(TypeError, match="count must be int"):
            web.search(["q"], count=(5,))  # pyright: ignore[reportArgumentType]


class TestUnderstandEntries:
    def test_effort_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava.understand import understand

        async def fake_batch(
            targets: list[Any], fn: Any, max_concurrent: int | None, *, after: Any = None
        ) -> list[Any]:
            return [fn(t) for t in targets]

        understand_module = importlib.import_module("ava.understand")
        monkeypatch.setattr(understand_module, "run_batch", fake_batch)
        monkeypatch.setattr(understand_module, "_understand_one", lambda **kw: kw["effort"])  # pyright: ignore[reportUnknownArgumentType]
        out = understand([{"prompt": "p", "text": "t"}], effort=("low",))  # pyright: ignore[reportArgumentType, reportCallIssue]
        assert out == ["low"]

    def test_effort_multi_element_type_errors(self) -> None:
        from ava.understand import understand

        with pytest.raises(TypeError, match="effort must be a string"):
            understand([{"prompt": "p", "text": "t"}], effort=("low", "high"))  # pyright: ignore[reportArgumentType, reportCallIssue]


class TestSelfEntries:
    def test_compact_summary_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The summary tuple unwraps before the framework's boot guard runs —
        the DB write then receives the unwrapped string."""
        from ava import self as self_mod

        seen: dict[str, Any] = {}
        monkeypatch.setattr(ava.agent_identity, "assert_self_action", lambda _action: None)  # pyright: ignore[reportUnknownArgumentType]

        class _FakeCur:
            connection = None

            def execute(self, sql: str, params: tuple[object, ...]) -> None:
                seen["params"] = params

        class _FakeCursor:
            def __enter__(self) -> _FakeCur:
                return _FakeCur()

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(ava.agent_identity, "agent_id", lambda: 900001)
        monkeypatch.setattr(ava.DB, "cursor", _FakeCursor)
        monkeypatch.setattr(ava.DB, "transaction", contextlib.nullcontext)
        monkeypatch.setattr(self_mod, "_publish_self_inbound_wake", lambda: None)
        import base.telemetry.audit_events as _audit

        monkeypatch.setattr(_audit, "record_audit", lambda _conn, event: event)  # pyright: ignore[reportUnknownArgumentType]
        from base.agents.lifecycle import SystemHalt

        with pytest.raises(SystemHalt):
            self_mod.compact(("summary",))  # pyright: ignore[reportArgumentType]
        assert seen["params"] == (900001, "summary")

    def test_compact_multi_element_type_errors(self) -> None:
        from ava import self as self_mod

        with pytest.raises(TypeError, match="summary must be a string"):
            self_mod.compact(("a", "b"))  # pyright: ignore[reportArgumentType]

    def test_pause_heartbeat_duration_never_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava import self as self_mod

        with pytest.raises(TypeError, match="duration must be"):
            self_mod.pause_heartbeat(("1800",))  # pyright: ignore[reportArgumentType]
