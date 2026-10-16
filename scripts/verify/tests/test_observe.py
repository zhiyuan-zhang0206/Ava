from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts.verify import observe

REPLY = "the reply"


def _timeline(
    *,
    code: str = "print(1 + 2)",
    output: str = "exit 0\n\n3\n",
    exec_ms: int | None = 12,
    reply: str = REPLY,
) -> list[dict[str, Any]]:
    return [
        {"kind": "agent_code", "payload": code, "exec_ms": None},
        {"kind": "code_output", "payload": output, "exec_ms": exec_ms},
        {"kind": "agent_chat", "payload": reply, "exec_ms": None},
    ]


def test_a_completed_execution_is_recognized() -> None:
    assert observe.execution_completed(_timeline(), REPLY)


@pytest.mark.parametrize(
    "items",
    [
        _timeline(output="exit 0\n\n13\n"),
        _timeline(output="exit 0 at 2026-10-01T03:00:00\n\nnothing printed\n"),
        _timeline(output="3"),
        _timeline(output="cancelled\n\n3\n"),
        _timeline(output="timeout after 30s\n\n3\n"),
        _timeline(exec_ms=None),
        _timeline(code="print(2 + 1)"),
        _timeline(reply="another reply"),
        _timeline()[:2],
        [*_timeline(), {"kind": "code_output", "payload": "exit 0\n\n3\n", "exec_ms": 1}],
    ],
)
def test_anything_short_of_the_executed_output_three_is_not_completion(
    items: list[dict[str, Any]],
) -> None:
    assert not observe.execution_completed(items, REPLY)


def _chain(*pids: int) -> list[dict[str, Any]]:
    names = {1: "launchd", 10: "AvaPermissionsHelper", 20: "ava-root", 30: "python3.12"}
    return [{"pid": pid, "name": names[pid]} for pid in pids]


def test_the_chain_launchd_helper_root_unit_is_accepted() -> None:
    assert observe.ancestry_problem(_chain(30, 20, 10, 1), [20, 10]) is None
    assert observe.ancestry_problem(_chain(20, 10, 1), [20, 10]) is None
    assert observe.ancestry_problem(_chain(10, 1), [10]) is None


@pytest.mark.parametrize(
    "chain",
    [
        _chain(30, 20, 1),
        _chain(30, 10, 20, 1),
        _chain(30, 20, 10),
        [*_chain(30, 20), {"pid": 5, "name": "zsh"}, *_chain(10, 1)],
    ],
)
def test_any_other_process_chain_is_a_problem(chain: list[dict[str, Any]]) -> None:
    assert observe.ancestry_problem(chain, [20, 10]) is not None


def test_a_check_that_exists_on_one_platform_is_listed_not_failed_on_the_other() -> None:
    assert set(observe.ONLY_ON_MACOS) <= set(observe.CHECKS)

    runnable, skipped = observe.applicable_checks(macos=False)
    assert "helper_chain" not in runnable and set(skipped) == set(observe.ONLY_ON_MACOS)
    assert set(runnable) | set(skipped) == set(observe.CHECKS)

    runnable, skipped = observe.applicable_checks(macos=True)
    assert set(runnable) == set(observe.CHECKS) and skipped == {}


def test_the_human_secret_is_read_from_the_home_env(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("AVA_CLUSTER_SECRET=dev-cluster-secret\n")
    assert observe.human_secret(tmp_path) == "dev-cluster-secret"


def test_a_home_without_a_human_secret_is_refused(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("AVA_DB_URL=postgresql://ava@127.0.0.1:5432/ava\n")
    with pytest.raises(RuntimeError, match="AVA_CLUSTER_SECRET"):
        observe.human_secret(tmp_path)


def test_the_login_presents_the_human_secret_and_keeps_the_session(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("AVA_CLUSTER_SECRET=dev-cluster-secret\n")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"ok": True}, headers={"Set-Cookie": "ava_session=s1; Path=/"}
        )

    with httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://gateway.test"
    ) as client:
        observe.login(client, tmp_path)
        assert client.cookies.get("ava_session") == "s1"
    assert [request.url.path for request in seen] == ["/api/auth/login"]
    assert json.loads(seen[0].content) == {"password": "dev-cluster-secret"}


def test_a_refused_login_is_an_error(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("AVA_CLUSTER_SECRET=dev-cluster-secret\n")

    def refuse(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid password"})

    with (
        httpx.Client(
            transport=httpx.MockTransport(refuse), base_url="http://gateway.test"
        ) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        observe.login(client, tmp_path)
