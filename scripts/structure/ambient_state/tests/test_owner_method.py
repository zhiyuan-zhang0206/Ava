"""Shared executable syntax must not manufacture ownership from dead code."""

from __future__ import annotations

import ast

import pytest

from scripts.structure.ambient_state.owner_method import (
    assignment_pairs,
    call_nodes,
    instance_field,
    join_has_timeout,
)


def test_method_facts_exclude_nested_definitions_and_constant_dead_branches() -> None:
    source = """
def stop(self):
    worker = self.worker
    retained: object = worker
    missing: object
    if False:
        erased = other
        pretend.join(timeout=1)
    else:
        worker.join(timeout=budget)
    def unused():
        nested = other
        nested.join(timeout=1)
    class Unused:
        held = other
        held.join(timeout=1)
    callback = lambda: other.join(timeout=1)
"""
    method = ast.parse(source).body[0]
    pairs = list(assignment_pairs(method))
    assert {ast.unparse(target) for target, _ in pairs} == {"worker", "retained", "callback"}
    assert isinstance(method, ast.FunctionDef)
    original = method.body[0]
    assert isinstance(original, ast.Assign)
    assert any(target is original.targets[0] and value is original.value for target, value in pairs)
    assert [ast.unparse(call.func) for call in call_nodes(method)] == ["worker.join"]


def test_unknown_branch_and_root_call_remain_visible() -> None:
    branch = ast.parse("if condition:\n    left()\nelse:\n    right()").body[0]
    assert {ast.unparse(call.func) for call in call_nodes(branch)} == {"left", "right"}
    call = ast.parse("worker.join(timeout=budget)", mode="eval").body
    assert isinstance(call, ast.Call)
    assert list(call_nodes(call)) == [call]


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("self.worker", "self.worker"),
        ("self.worker.handle", None),
        ("other.worker", None),
        ("self.workers[key]", None),
        ("self.worker().handle", None),
    ],
)
def test_only_direct_instance_fields_are_identified(expression: str, expected: str | None) -> None:
    assert instance_field(ast.parse(expression, mode="eval").body) == expected


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ("", False),
        ("timeout=None", False),
        ("timeout=True", False),
        ("timeout='budget'", False),
        ("timeout=float('inf')", False),
        ("1", True),
        ("timeout=0.5", True),
        ("timeout=budget", True),
    ],
)
def test_timeout_shape_keeps_invalid_budgets_unproved(arguments: str, expected: bool) -> None:
    call = ast.parse(f"worker.join({arguments})", mode="eval").body
    assert isinstance(call, ast.Call)
    assert join_has_timeout(call) is expected
