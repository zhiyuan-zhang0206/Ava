"""`ava memory search` request, validation, rendering, and error contracts."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from cli import main as _main
from cli.commands import memory as _memory


class _FakeResp:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://gw:8000/api/memory/search")
            response = httpx.Response(self.status_code, request=request, text=self.text)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request, response=response
            )

    def json(self) -> object:
        return self._payload


@pytest.fixture(autouse=True)
def _gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr(
        "shared.machine.gateway_auth_headers", lambda: {"Authorization": "Bearer secret"}
    )


def _patch_post(
    monkeypatch: pytest.MonkeyPatch, payload: object, *, status_code: int = 200
) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> _FakeResp:
        seen["url"] = url
        seen.update(kwargs)
        return _FakeResp(payload, status_code=status_code)

    monkeypatch.setattr(httpx, "post", fake_post)
    return seen


def test_memory_search_posts_authenticated_query_and_renders_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results: list[dict[str, object]] = [
        {
            "path": "projects/alpha.md",
            "tags": ["project", "active"],
            "description": "Alpha context",
        },
        {"path": "notes/empty.md", "tags": [], "description": ""},
    ]
    seen = _patch_post(
        monkeypatch,
        {
            "paths": ["projects/alpha.md", "notes/empty.md"],
            "results": results,
        },
    )

    assert _memory.cmd_memory_search("alpha", limit=7, json_output=False) == 0
    assert seen["url"] == "http://gw:8000/api/memory/search"
    assert seen["json"] == {"query": "alpha", "k": 7}
    assert seen["headers"] == {"Authorization": "Bearer secret"}
    assert capsys.readouterr().out.splitlines() == [
        "path               tags             description",
        "projects/alpha.md  project, active  Alpha context",
        "notes/empty.md                      ",
    ]


def test_memory_search_json_emits_results_shape_and_preserves_nulls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results: list[dict[str, object]] = [
        {"path": "relative/note.md", "tags": None, "description": None},
        {"path": "relative/empty.md", "tags": [], "description": ""},
    ]
    _patch_post(
        monkeypatch,
        {"paths": [row["path"] for row in results], "results": results},
    )

    assert _memory.cmd_memory_search("edge cases", limit=5, json_output=True) == 0
    assert json.loads(capsys.readouterr().out) == results


def test_memory_search_human_output_renders_null_metadata_as_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_post(
        monkeypatch,
        {
            "paths": ["relative/note.md"],
            "results": [
                {"path": "relative/note.md", "tags": None, "description": None},
            ],
        },
    )

    assert _memory.cmd_memory_search("edge cases", limit=5, json_output=False) == 0
    assert capsys.readouterr().out.splitlines() == [
        "path              tags  description",
        "relative/note.md        ",
    ]


@pytest.mark.parametrize(
    ("status_code", "payload", "diagnostic"),
    [
        (401, {"detail": "invalid cluster secret"}, "invalid cluster secret"),
        (422, {"detail": "query is invalid"}, "query is invalid"),
        (503, {"reason": "memory index unavailable"}, "memory index unavailable"),
    ],
)
def test_memory_search_surfaces_gateway_error_detail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status_code: int,
    payload: object,
    diagnostic: str,
) -> None:
    _patch_post(monkeypatch, payload, status_code=status_code)

    with pytest.raises(httpx.HTTPStatusError):
        _memory.cmd_memory_search("alpha", limit=5, json_output=False)

    assert diagnostic in capsys.readouterr().err


def test_memory_search_parser_defaults_and_forwards_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int, bool]] = []

    def search(query: str, *, limit: int, json_output: bool) -> int:
        calls.append((query, limit, json_output))
        return 0

    monkeypatch.setattr(_memory, "cmd_memory_search", search)

    assert _main.main(["memory", "search", "alpha"]) == 0
    assert _main.main(["memory", "search", "beta", "--limit", "9", "--json"]) == 0
    assert calls == [("alpha", 5, False), ("beta", 9, True)]


@pytest.mark.parametrize("query", [""])
def test_memory_search_parser_rejects_empty_query(query: str) -> None:
    with pytest.raises(SystemExit) as exited:
        _main._build_parser().parse_args(["memory", "search", query])

    assert exited.value.code == 2


@pytest.mark.parametrize("limit", ["0", "101"])
def test_memory_search_parser_rejects_out_of_range_limit(limit: str) -> None:
    with pytest.raises(SystemExit) as exited:
        _main._build_parser().parse_args(["memory", "search", "alpha", "--limit", limit])

    assert exited.value.code == 2


@pytest.mark.parametrize("limit", ["1", "100"])
def test_memory_search_parser_accepts_limit_bounds(limit: str) -> None:
    args = _main._build_parser().parse_args(["memory", "search", "alpha", "--limit", limit])

    assert args.limit == int(limit)
