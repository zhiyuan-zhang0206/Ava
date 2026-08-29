"""Unified SDK argument validation — the trailing-comma guard.

Every ava.* entry point validates its own arguments through the shared
`ava._sdk_validation` helpers (one implementation, user ruling 2026-08-28):

- string-expected arguments unwrap a one-element list/tuple whose element is a
  string (the LLM trailing-comma class that 422'd the gateway — issue #1343,
  2026-08-28 send_message agents 2697/2986);
- multi-element sequences and wrong types raise TypeError naming the parameter;
- arguments that are inherently not strings are checked strictly and never
  unwrapped.

The suite covers ① single-element tuple normalization at every entry point,
② multi-element / wrong-type TypeError, ③ zero regression of existing behavior
(str / None / Path / int values still flow through unchanged).
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import psycopg
import pytest

import ava
import ava._boot
from ava import agents
from ava import files as _files
from ava import shell as _shell
from ava import ui as _ui
from ava import watcher as _watcher
from ava._sdk_validation import coerce_str, coerce_typed

# ── helper units ─────────────────────────────────────────────────────────────


class TestCoerceStr:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("plain", "plain", id="plain-string"),
            pytest.param(("one",), "one", id="one-element-tuple"),
            pytest.param(["one"], "one", id="one-element-list"),
            pytest.param(("implicit concat",), "implicit concat", id="implicit-concatenation"),
            pytest.param(None, None, id="none-allowed"),
        ],
    )
    def test_unwraps_or_passes(self, value: object, expected: object) -> None:
        assert coerce_str(value, "x", allow_none=True) == expected

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(("a", "b"), id="multi-element-tuple"),
            pytest.param(["a", "b"], id="multi-element-list"),
            pytest.param((1,), id="non-string-element"),
            pytest.param([1], id="non-string-list-element"),
            pytest.param((), id="empty-tuple"),
            pytest.param(42, id="int"),
            pytest.param(True, id="bool"),
            pytest.param(None, id="none-rejected"),
        ],
    )
    def test_type_errors(self, value: object) -> None:
        with pytest.raises(TypeError, match="x must be a string"):
            coerce_str(value, "x")

    def test_allow_types_passes_legitimate_non_string_forms(self, tmp_path: Path) -> None:
        import datetime

        when = datetime.datetime.now(datetime.UTC)
        assert coerce_str(when, "when", allow_types=(datetime.datetime, datetime.timedelta)) is when
        delta = datetime.timedelta(minutes=5)
        assert (
            coerce_str(delta, "when", allow_types=(datetime.datetime, datetime.timedelta)) is delta
        )
        p = tmp_path
        assert coerce_str(p, "path", allow_types=(Path,)) is p

    def test_allow_types_unwraps_before_passing(self) -> None:
        import datetime

        value = coerce_str(
            ("2026-01-01T00:00:00+08:00",),
            "when",
            allow_types=(datetime.datetime, datetime.timedelta),
        )
        assert value == "2026-01-01T00:00:00+08:00"

    def test_sequence_allowed_only_for_dict_lists(self) -> None:
        """The one sequence the SDK accepts as a non-string value is the
        multimodal content-block list — an all-string array is exactly the
        shape the gateway rejects."""
        blocks = [{"type": "text", "text": "hi"}]
        assert coerce_str(blocks, "content", allow_types=(list,)) is blocks
        assert coerce_str([], "content", allow_types=(list,)) == []
        with pytest.raises(TypeError, match="content must be a string or a list of dicts"):
            coerce_str(["a", "b"], "content", allow_types=(list,))
        with pytest.raises(TypeError, match="content must be a string or a list of dicts"):
            coerce_str([1], "content", allow_types=(list,))


class TestCoerceTyped:
    def test_passes_declared_types(self) -> None:
        assert coerce_typed(5, "n", int) == 5
        assert coerce_typed(5.0, "n", (int, float)) == 5.0
        assert coerce_typed(None, "n", int, allow_none=True) is None
        assert coerce_typed([1, 2], "tags", (list, tuple)) == [1, 2]
        assert coerce_typed((1, 2), "tags", (list, tuple)) == (1, 2)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("5", int, id="string-for-int"),
            pytest.param((5,), int, id="one-element-tuple-for-int"),
            pytest.param(None, int, id="none-rejected"),
            pytest.param([5], int, id="list-for-int"),
        ],
    )
    def test_never_unwraps(self, value: object, expected: type) -> None:
        """A one-element tuple for a non-string parameter is a TypeError — no
        expansion outside the string-expected class."""
        with pytest.raises(TypeError, match="n must be"):
            coerce_typed(value, "n", expected)


# ── agents ───────────────────────────────────────────────────────────────────


class TestAgentsEntries:
    def test_spawn_prompt_unwraps_before_gateway_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(agents._client, "spawn", lambda **kw: seen.update(kw) or 1)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(ava._boot, "require_actor", lambda: "agent:1")

        agents.spawn(prompt=("hello",))  # pyright: ignore[reportArgumentType]
        assert seen["prompt"] == "hello"

    @pytest.mark.parametrize(
        "prompt",
        [
            pytest.param(("a", "b"), id="multi-element-tuple"),
            pytest.param(["a", "b"], id="multi-element-list"),
        ],
    )
    def test_spawn_prompt_multi_element_type_errors(
        self, monkeypatch: pytest.MonkeyPatch, prompt: object
    ) -> None:
        monkeypatch.setattr(agents._client, "spawn", lambda **_kw: 1)  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(TypeError, match="prompt must be a string"):
            agents.spawn(prompt=prompt)  # pyright: ignore[reportArgumentType]

    def test_spawn_fork_from_never_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(agents._client, "spawn", lambda **_kw: 1)  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(TypeError, match="fork_from must be int"):
            agents.spawn(fork_from=(5,))  # pyright: ignore[reportArgumentType]

    def test_send_message_content_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            agents._client,
            "send_message",
            lambda _aid, **kw: seen.update(kw) or None,  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]

        agents.send_message(7, ("hi",))  # pyright: ignore[reportArgumentType]
        assert seen["content"] == "hi"

    def test_send_message_blocks_pass_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            agents._client,
            "send_message",
            lambda _aid, **kw: seen.update(kw) or None,  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]
        blocks: list[dict[str, object]] = [{"type": "text", "text": "hi"}]

        agents.send_message(7, blocks)  # pyright: ignore[reportArgumentType]
        assert seen["content"] is blocks

    def test_send_message_multi_element_type_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(agents._client, "send_message", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(TypeError, match="content must be a string"):
            agents.send_message(7, ("a", "b"))  # pyright: ignore[reportArgumentType]

    def test_send_system_note_unwraps_content_and_tag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            agents._client,
            "send_system_note",
            lambda *_a, **_kw: seen.update(_kw) or 1,  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]

        agents.send_system_note(7, ("note",), tag=("task",))  # pyright: ignore[reportArgumentType]
        assert seen["content"] == "note"
        assert seen["note_tag"] == "task"

    def test_resurrect_prompt_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            agents._client,
            "resurrect",
            lambda *_a, **_kw: seen.update(_kw) or "spawned",  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]

        agents.resurrect(7, ("wake up",))  # pyright: ignore[reportArgumentType]
        assert seen["prompt"] == "wake up"

    def test_resurrect_rejects_none_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(agents._client, "resurrect", lambda *_a, **_kw: "spawned")  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(TypeError, match="prompt must be a string, got None"):
            agents.resurrect(7, None)  # pyright: ignore[reportArgumentType]

    @pytest.mark.parametrize(
        ("call", "match"),
        [
            pytest.param(lambda: agents.terminate(("7",)), "agent_id must be int", id="terminate"),  # pyright: ignore[reportArgumentType]
            pytest.param(lambda: agents.restart(("7",)), "agent_id must be int", id="restart"),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: agents.get_status(("7",)),  # pyright: ignore[reportArgumentType]
                "agent_id must be int",
                id="get_status",  # pyright: ignore[reportArgumentType]
            ),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: agents.get_neighbors(("7",)),  # pyright: ignore[reportArgumentType]
                "agent_id must be int",
                id="get_neighbors",  # pyright: ignore[reportArgumentType]
            ),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: agents.get_neighbors(7, depth=("1",)),  # pyright: ignore[reportArgumentType]
                "depth must be int",
                id="depth",  # pyright: ignore[reportArgumentType]
            ),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: agents.send_message(("7",), "hi"),  # pyright: ignore[reportArgumentType]
                "agent_id must be int",
                id="send_message-id",
            ),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: agents.send_system_note(("7",), "hi"),  # pyright: ignore[reportArgumentType]
                "agent_id must be int",
                id="send_system_note-id",
            ),  # pyright: ignore[reportArgumentType]
        ],
    )
    def test_agent_ids_never_unwrap(
        self, monkeypatch: pytest.MonkeyPatch, call: Any, match: str
    ) -> None:
        monkeypatch.setattr(agents._client, "send_message", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(agents._client, "send_system_note", lambda *_a, **_kw: 1)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(agents._client, "terminate", lambda *_a, **_kw: "enqueued")  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(agents._client, "restart", lambda *_a, **_kw: "enqueued")  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(agents._client, "list_agents", lambda **_kw: [])  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(agents._client, "get_neighbors", lambda *_a, **_kw: [])  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(TypeError, match=match):
            call()


# ── files ────────────────────────────────────────────────────────────────────


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


# ── shell ────────────────────────────────────────────────────────────────────


class TestShellEntries:
    def test_run_cmd_unwraps(self) -> None:
        result = _shell.run(("echo sdk-validation-smoke",))  # pyright: ignore[reportArgumentType]
        assert "sdk-validation-smoke" in str(result)

    def test_run_multi_element_cmd_type_errors(self) -> None:
        with pytest.raises(TypeError, match="cmd must be a string"):
            _shell.run(("echo a", "echo b"))  # pyright: ignore[reportArgumentType]

    def test_run_background_normalizes_and_starts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        created: dict[str, Any] = {}
        sent: dict[str, Any] = {}
        monkeypatch.setattr(
            _shell.sessions,
            "_create_session",
            lambda name, **kw: created.update(name=name, **kw) or (42, "full"),  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(
            _shell._background,
            "allocate_output_path",
            lambda sid, _name: tmp_path / f"{sid}.log",  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(_shell._background, "notified_line", lambda *_a, **_kw: "line")  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(
            _shell.sessions,
            "send",
            lambda sid, line: sent.update(sid=sid, line=line),  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(ava._boot, "agent_id", lambda: 900001)

        run = _shell.run_background(("echo hi",), name=("bg",), ttl=60)  # pyright: ignore[reportArgumentType]
        assert run.session_id == 42
        assert created["name"] == "bg"
        assert sent["line"] == "line"

    def test_run_background_multi_element_name_type_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(TypeError, match="name must be a string"):
            _shell.run_background("echo hi", name=("a", "b"), ttl=60)  # pyright: ignore[reportArgumentType]


# ── ui ───────────────────────────────────────────────────────────────────────


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

        _ui.show(("mypage",), title=("My Page",))  # pyright: ignore[reportArgumentType]
        assert seen["name"] == "mypage"
        assert seen["title"] == "My Page"

    def test_serve_unwraps_dir_and_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(_ui, "_agent_page_port", lambda: 9999)
        monkeypatch.setattr(_ui, "_close_existing", lambda: None)
        monkeypatch.setattr(_ui, "_wait_until_serving", lambda *_a, **_k: True)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(
            _ui,
            "_register_page",
            lambda name, port, title, serve_dir, ttl: (  # pyright: ignore[reportUnknownArgumentType]
                seen.update(name=name, serve_dir=serve_dir, port=port, title=title, ttl=ttl)
                or object()
            ),
        )  # pyright: ignore[reportUnknownArgumentType]

        _ui.serve((str(tmp_path),), ("served",))  # pyright: ignore[reportArgumentType]
        assert seen["name"] == "served"
        assert seen["serve_dir"] == str(Path(str(tmp_path)).resolve())

    def test_close_unwraps_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            _ui._gateway_client,
            "close_page",
            lambda _aid, name: seen.update(name=name),  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]

        _ui.close(("mypage",))  # pyright: ignore[reportArgumentType]
        assert seen["name"] == "mypage"

    @pytest.mark.parametrize(
        ("call", "match"),
        [
            pytest.param(lambda: _ui.show(("a", "b")), "name must be a string", id="show-multi"),  # pyright: ignore[reportArgumentType]
            pytest.param(lambda: _ui.show("x", port=("80",)), "port must be int", id="show-port"),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: _ui.serve(("a", "b"), "n"),  # pyright: ignore[reportArgumentType]
                "dir must be a string",
                id="serve-multi",  # pyright: ignore[reportArgumentType]
            ),  # pyright: ignore[reportArgumentType]
            pytest.param(
                lambda: _ui.serve_markdown(("a", "b"), "n"),  # pyright: ignore[reportArgumentType]
                "content must be a string",
                id="markdown-multi",
            ),  # pyright: ignore[reportArgumentType]
        ],
    )
    def test_multi_element_type_errors(self, call: Any, match: str) -> None:
        with pytest.raises(TypeError, match=match):
            call()


# ── watcher ──────────────────────────────────────────────────────────────────


class TestWatcherEntries:
    def test_at_unwraps_when_message_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            _watcher,
            "_spawn",
            lambda _code, _ttl, name, **kw: seen.update(name=name, **kw) or 1,  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]

        _watcher.at(("2030-01-01T00:00:00+08:00",), ("stand up",), name=("standup",))  # pyright: ignore[reportArgumentType]
        assert seen["name"] == "standup"
        assert "stand up" in str(seen.get("message", ""))

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


# ── web / understand ─────────────────────────────────────────────────────────


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


# ── memory / tasks / notices ─────────────────────────────────────────────────


class TestMemoryEntries:
    def test_search_query_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava import _gateway_client
        from ava_builtins.plugins.ava_memory import sdk as memory_plugin

        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            _gateway_client,
            "memory_search",
            lambda q, k, **_kw: seen.update(q=q, k=k) or [],  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]

        memory_plugin._search(("query",))  # pyright: ignore[reportArgumentType]
        assert seen["q"] == "query"

    def test_search_query_multi_element_type_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava import _gateway_client
        from ava_builtins.plugins.ava_memory import sdk as memory_plugin

        monkeypatch.setattr(_gateway_client, "memory_search", lambda _q, _k, **_kw: [])  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(TypeError, match="query must be a string"):
            memory_plugin._search(("a", "b"))  # pyright: ignore[reportArgumentType]

    def test_write_slug_unwraps(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Slug tuple unwraps; the entry lands under the unwrapped name."""
        from ava_builtins.plugins.ava_memory import sdk as memory_plugin
        from shared.paths import workspace_dir

        root = workspace_dir(900001) / "memory"
        monkeypatch.setattr(ava._boot, "_agent_id", 900001)
        monkeypatch.setattr(
            memory_plugin,
            "_entry_path",
            lambda _slug, _store, _agent_id: (root / f"{_slug}.md", False),  # pyright: ignore[reportUnknownArgumentType]
        )
        monkeypatch.setattr(memory_plugin, "_write_atomically", lambda _path, _content: None)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(memory_plugin, "_upsert_index", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]

        entry = memory_plugin.write(("slug-one",), ("content",), store=("personal",))  # pyright: ignore[reportArgumentType]
        assert entry == (root / "slug-one.md").resolve()

    def test_write_multi_element_slug_type_errors(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from ava_builtins.plugins.ava_memory import sdk as memory_plugin

        monkeypatch.setattr(
            memory_plugin,
            "_entry_path",
            lambda _slug, _store, _agent_id: (tmp_path / "x.md", False),  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(TypeError, match="slug must be a string"):
            memory_plugin.write(("a", "b"), "c")  # pyright: ignore[reportArgumentType]


class TestTasksEntries:
    def test_create_title_unwraps(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Single-element title tuple lands as the unwrapped string (DB round-trip)."""
        from ava_builtins.plugins.ava_fleet import task_registry

        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_tasks (title, description, status, created_by, is_root) "
                "VALUES ('Root', 'root', 'ongoing', 'system', TRUE) RETURNING id"
            )
            row = cur.fetchone()
            assert row is not None
            root_id = row[0]  # pyright: ignore[reportOptionalSubscript]
        db_conn.commit()
        monkeypatch.setattr(ava._boot, "_agent_id", 900001)
        with db_conn.cursor() as cur:
            cur.execute("INSERT INTO agents (id) VALUES (900001) ON CONFLICT (id) DO NOTHING")
        db_conn.commit()
        monkeypatch.setattr(task_registry, "publish_task_created_sync", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]

        task = task_registry.create(title=("My Task",), description="d", parent=root_id)  # pyright: ignore[reportArgumentType, reportUnknownArgumentType]
        assert task.title == "My Task"

    def test_create_multi_element_title_type_errors(self) -> None:
        from ava_builtins.plugins.ava_fleet import task_registry

        with pytest.raises(TypeError, match="title must be a string"):
            task_registry.create(title=("a", "b"), parent=1)  # pyright: ignore[reportArgumentType]

    def test_create_parent_never_unwraps(self) -> None:
        from ava_builtins.plugins.ava_fleet import task_registry

        with pytest.raises(TypeError, match="parent must be int"):
            task_registry.create(title="t", parent=(1,))  # pyright: ignore[reportArgumentType]

    def test_log_message_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava_builtins.plugins.ava_fleet import task_registry

        seen: dict[str, Any] = {}
        monkeypatch.setattr(task_registry, "update", lambda *_a, **_kw: seen.update(_kw))  # pyright: ignore[reportUnknownArgumentType]
        task_registry.log(7, ("note",))  # pyright: ignore[reportArgumentType]
        assert seen["note"] == "note"

    def test_log_multi_element_type_errors(self) -> None:
        from ava_builtins.plugins.ava_fleet import task_registry

        with pytest.raises(TypeError, match="message must be a string"):
            task_registry.log(7, ("a", "b"))  # pyright: ignore[reportArgumentType]

    def test_update_status_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava_builtins.plugins.ava_fleet import task_registry

        # Coercion fires before the DB write: a status tuple unwraps, then the
        # status-value validation runs on the string.
        with pytest.raises(ValueError, match="status must be one of"):
            task_registry.update(7, status=("not-a-status",))  # pyright: ignore[reportArgumentType]

    def test_update_owner_never_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava_builtins.plugins.ava_fleet import task_registry

        with pytest.raises(TypeError, match="owner must be int"):
            task_registry.update(7, owner=(5,))  # pyright: ignore[reportArgumentType]


class TestNoticeEntries:
    def test_notify_title_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ava import _gateway_client
        from ava_builtins.plugins.ava_fleet import plugin as fleet_plugin

        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            _gateway_client,
            "_post",
            lambda *_a, **_kw: seen.update(body=_a[1]) or _FakeResp(),  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(_gateway_client, "_raise_from_response", lambda _resp: None)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(ava._boot, "agent_id", lambda: 900001)

        notice = fleet_plugin.notify(("Hi",), ("detail",), priority=("P2",))  # pyright: ignore[reportArgumentType]
        body = seen["body"]
        assert body["title"] == "Hi"
        assert body["content"] == "detail"
        assert body["priority"] == "P2"
        assert notice == 1  # Notice is an int subclass; the id is the value

    def test_notify_multi_element_title_type_errors(self) -> None:
        from ava_builtins.plugins.ava_fleet import plugin as fleet_plugin

        with pytest.raises(TypeError, match="title must be a string"):
            fleet_plugin.notify(("a", "b"))  # pyright: ignore[reportArgumentType]

    def test_notify_task_never_unwraps(self) -> None:
        from ava_builtins.plugins.ava_fleet import plugin as fleet_plugin

        with pytest.raises(TypeError, match="task must be int"):
            fleet_plugin.notify("hi", task=("5",))  # pyright: ignore[reportArgumentType]


class _FakeResp:
    """Minimal stand-in for the gateway response notify() consumes."""

    def json(self) -> dict[str, Any]:
        return {
            "id": 1,
            "pending_count": 0,
            "superseded": [],
            "pending_notices": [],
        }


# ── self / attach ────────────────────────────────────────────────────────────


class TestSelfEntries:
    def test_compact_summary_unwraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The summary tuple unwraps before the framework's boot guard runs —
        the DB write then receives the unwrapped string."""
        from ava import self as self_mod

        seen: dict[str, Any] = {}
        monkeypatch.setattr(ava._boot, "assert_self_action", lambda _action: None)  # pyright: ignore[reportUnknownArgumentType]

        class _FakeCur:
            def execute(self, sql: str, params: tuple[object, ...]) -> None:
                seen["params"] = params

        class _FakeCursor:
            def __enter__(self) -> _FakeCur:
                return _FakeCur()

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(ava._boot, "agent_id", lambda: 900001)
        monkeypatch.setattr(ava.DB, "cursor", _FakeCursor)
        monkeypatch.setattr(self_mod, "_publish_self_inbound_wake", lambda: None)
        import shared.audit_events as _audit

        monkeypatch.setattr(_audit, "insert_event_log", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        from shared.lifecycle import _SystemHalt

        with pytest.raises(_SystemHalt):
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
