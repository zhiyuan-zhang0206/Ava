"""Original Task-valued map snapshots and one local bounded join."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.structure import ambient_state

SCHEDULER = """
import asyncio

class Owner:
    def __init__(self, report):
        self.children = {}
        self.errors = []
        self.report = report

    def start(self, key, operation):
        task = asyncio.create_task(operation())
        self.children[key] = task
        task.add_done_callback(self.finished)
        return task

    def finished(self, task):
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self.errors.append(error)
                self.report(error)

    def child(self, key):
        return self.children.get(key)

    async def stop(self, budget=1):
        snapshot = dict(self.children)
        for task in snapshot.values():
            task.cancel()
        await self.join(snapshot, budget)
        if self.errors:
            raise self.errors[0]

    async def join(self, snapshot, budget):
        await asyncio.wait(snapshot.values(), timeout=budget)
"""


def _sites(source: str, root: Path) -> dict[str, list[int]]:
    return ambient_state.measure(ast.parse(source), "base/operations.py", root)


def test_original_snapshot_and_local_join_are_supported(tmp_path: Path) -> None:
    assert _sites(SCHEDULER, tmp_path) == {}


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("self.children[key] = task", "self.children[key] = other"),
        ("self.children[key] = task", "self.current = task"),
        ("task.add_done_callback(self.finished)", "other.add_done_callback(self.finished)"),
        ("snapshot = dict(self.children)", "snapshot = dict(self.unowned)"),
        (
            "snapshot = dict(self.children)",
            "snapshot = dict(self.children)\n        snapshot.clear()",
        ),
        (
            "snapshot = dict(self.children)",
            "snapshot = dict(self.children)\n        snapshot[0] = other",
        ),
        (
            "snapshot = dict(self.children)",
            "snapshot = dict(self.children)\n        snapshot |= other",
        ),
        (
            "task.add_done_callback(self.finished)",
            "pause()\n        task.add_done_callback(self.finished)",
        ),
        ("async def join(self, snapshot, budget):", "def join(self, snapshot, budget):"),
        (
            "async def join(self, snapshot, budget):",
            "async def join(self, snapshot, budget):\n        return",
        ),
        ("for task in snapshot.values():", "for task in snapshot:"),
        ("task.cancel()", "other.cancel()"),
        ("await self.join(snapshot, budget)", "self.join(snapshot, budget)"),
        ("await self.join(snapshot, budget)", "await self.join(other, budget)"),
        (
            "await asyncio.wait(snapshot.values(), timeout=budget)",
            "await asyncio.wait(snapshot, timeout=budget)",
        ),
        (
            "await asyncio.wait(snapshot.values(), timeout=budget)",
            "await asyncio.wait(other, timeout=budget)",
        ),
        (
            "await asyncio.wait(snapshot.values(), timeout=budget)",
            "asyncio.wait(snapshot.values(), timeout=budget)",
        ),
        ("timeout=budget", "timeout=None"),
        ("timeout=budget", "timeout=float('inf')"),
        ("return self.children.get(key)", "return self.unowned.get(key)"),
        ("return self.children.get(key)", "return self.children.get()"),
        ("return self.children.get(key)", "return self.children.get(other)"),
        ("def child(self, key):", "async def child(self, key):"),
        (
            "await self.join(snapshot, budget)",
            "await self.join(snapshot, budget)\n        self.children.clear()",
        ),
        ("error = task.exception()", "error = other.exception()"),
        ("self.errors.append(error)", "self.errors.append(RuntimeError('replacement'))"),
        ("raise self.errors[0]", "raise RuntimeError('replacement')"),
    ],
)
def test_task_valued_map_must_keep_and_join_original_handles(
    before: str, after: str, tmp_path: Path
) -> None:
    assert _sites(SCHEDULER.replace(before, after), tmp_path)


def test_unrelated_rejecting_gate_is_not_request_stop(tmp_path: Path) -> None:
    source = SCHEDULER.replace(
        "        self.children = {}", "        self.closed = False\n        self.children = {}"
    )
    source = source.replace(
        "        for task in snapshot.values():\n            task.cancel()",
        "        self.closed = True",
    )
    source += """

    async def unrelated_admission(self):
        if self.closed:
            raise RuntimeError('closed')
        return object()
"""
    assert _sites(source, tmp_path)


@pytest.mark.parametrize("method", ["stop", "join"])
def test_original_map_cannot_be_discarded_through_an_alias(method: str, tmp_path: Path) -> None:
    entry = f"    async def {method}(self, "
    start = SCHEDULER.index(entry)
    body = SCHEDULER.index("\n", start) + 1
    source = (
        SCHEDULER[:body]
        + "        original = self.children\n        original.clear()\n"
        + SCHEDULER[body:]
    )
    assert _sites(source, tmp_path)


def test_local_evidence_does_not_depend_on_owner_names(tmp_path: Path) -> None:
    renamed = (
        SCHEDULER.replace("Owner", "Renamed")
        .replace("children", "retained")
        .replace("finished", "received")
    )
    renamed = renamed.replace("import asyncio", "import asyncio as scheduling").replace(
        "asyncio.", "scheduling."
    )
    assert _sites(renamed, tmp_path) == {}


def test_current_dispatcher_uses_the_same_general_map_evidence() -> None:
    root = Path(__file__).resolve().parents[4]
    relative = "services/agent_runner/agent_host/dispatcher.py"
    findings = ambient_state.measure(ast.parse((root / relative).read_text()), relative, root)
    assert f"{relative}::asyncio-task:TurnScheduler._start" not in findings


def test_stop_snapshot_cannot_be_cleared_through_a_later_alias(tmp_path: Path) -> None:
    source = SCHEDULER.replace(
        "        snapshot = dict(self.children)",
        "        snapshot = dict(self.children)\n        alias = snapshot\n        alias.clear()",
    )
    assert _sites(source, tmp_path)


@pytest.mark.parametrize("method", ["stop", "join"])
@pytest.mark.parametrize("through_second_alias", [False, True])
@pytest.mark.parametrize(
    "mutation", ["receiver.clear()", "receiver[0] = other", "receiver |= other"]
)
def test_snapshot_alias_chains_cannot_discard_or_replace_original_handles(
    method: str, through_second_alias: bool, mutation: str, tmp_path: Path
) -> None:
    declarations = "        alias = snapshot\n"
    receiver = "alias"
    if through_second_alias:
        declarations += "        second = alias\n"
        receiver = "second"
    declarations += "        " + mutation.replace("receiver", receiver) + "\n"
    if method == "stop":
        anchor = "        snapshot = dict(self.children)\n"
        source = SCHEDULER.replace(anchor, anchor + declarations)
    else:
        anchor = "        await asyncio.wait(snapshot.values(), timeout=budget)"
        source = SCHEDULER.replace(anchor, declarations + anchor)
    assert _sites(source, tmp_path)


@pytest.mark.parametrize("method", ["stop", "join"])
def test_unknown_rebound_alias_is_not_the_joined_original_snapshot(
    method: str, tmp_path: Path
) -> None:
    if method == "stop":
        source = SCHEDULER.replace(
            "        snapshot = dict(self.children)",
            "        snapshot = dict(self.children)\n        alias = snapshot\n        alias = other",
        ).replace("await self.join(snapshot, budget)", "await self.join(alias, budget)")
    else:
        source = SCHEDULER.replace(
            "        await asyncio.wait(snapshot.values(), timeout=budget)",
            "        alias = snapshot\n        alias = other\n"
            "        await asyncio.wait(alias.values(), timeout=budget)",
        )
    assert _sites(source, tmp_path)
