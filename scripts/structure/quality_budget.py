"""Function quality metrics and frozen-baseline helpers for the structure gate."""

from __future__ import annotations

import ast
import sys
from collections import Counter, defaultdict
from pathlib import Path

from radon.complexity import ComplexityVisitor
from radon.visitors import Class, Function

COMPLEXITY_HARD = 15
COMPLEXITY_WARN = 10
NESTING_CEILING = 5
QUALITY_SECTIONS = ("complexity", "nesting")
CONTROL = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.Match)
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def function_nodes(tree: ast.AST, path: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Index functions by Python qualname, numbering redefinitions by source order."""
    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        if isinstance(node, FUNCTIONS):
            name = prefix + node.name
            found.append((name, node))
            prefix = name + ".<locals>."
        elif isinstance(node, ast.ClassDef):
            prefix += node.name + "."
        for child in ast.iter_child_nodes(node):
            visit(child, prefix)

    visit(tree, "")
    counts: Counter[str] = Counter()
    result: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for name, node in sorted(found, key=lambda item: item[1].lineno):
        counts[name] += 1
        suffix = f"#{counts[name]}" if counts[name] > 1 else ""
        result[f"{path}::{name}{suffix}"] = node
    return result


def _block_scores(blocks: list[Class | Function]) -> dict[tuple[int, str], int]:
    """Include methods, closures and inner classes without double-counting methods."""
    result: dict[tuple[int, str], int] = {}
    for block in blocks:
        if isinstance(block, Class):
            result.update(_block_scores([*block.methods, *block.inner_classes]))
        else:
            result[block.lineno, block.name] = block.complexity
            result.update(_block_scores(block.closures))
    return result


def complexity_scores(
    tree: ast.AST, nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
) -> dict[str, int]:
    """Use Radon 6.0.1 on the shared AST, including its local-class collection gap."""
    scores = _block_scores(ComplexityVisitor.from_ast(tree).blocks)
    result: dict[str, int] = {}
    for key, node in nodes.items():
        identity = (node.lineno, node.name)
        if identity not in scores:
            # Radon discards classes inside functions (and their methods/closures).
            # Reuse the existing node and the same visitor, never reparse source.
            scores.update(_block_scores(ComplexityVisitor.from_ast(node).blocks))
        result[key] = scores[identity]
    return result


def _if_depth(node: ast.If, depth: int) -> int:
    current = depth + 1
    deepest = max((_node_depth(child, current) for child in node.body), default=current)
    # Match the audit's AST shape: elif keeps its parent's depth; else: if deepens.
    if (
        len(node.orelse) == 1
        and isinstance(node.orelse[0], ast.If)
        and node.orelse[0].col_offset == node.col_offset
    ):
        return max(deepest, _node_depth(node.orelse[0], depth))
    return max(deepest, *(_node_depth(child, current) for child in node.orelse), current)


def _node_depth(node: ast.AST, depth: int) -> int:
    if isinstance(node, (*FUNCTIONS, ast.ClassDef, ast.Lambda)):
        return depth
    if isinstance(node, ast.If):
        return _if_depth(node, depth)
    if isinstance(node, CONTROL):
        depth += 1
    if isinstance(node, ast.Match):
        children = [child for case in node.cases for child in case.body]
    else:
        # Includes Try handlers/else/finally at the control node's incremented depth.
        children = ast.iter_child_nodes(node)
    return max((_node_depth(child, depth) for child in children), default=depth)


def max_depth(body: list[ast.stmt]) -> int:
    """Port of audit 6562: deepest control-flow path, with nested definitions separate."""
    return max((_node_depth(node, 0) for node in body), default=0)


def measure_quality(tree: ast.AST, path: str) -> dict[str, dict[str, int]]:
    nodes = function_nodes(tree, path)
    return {
        "complexity": complexity_scores(tree, nodes),
        "nesting": {key: max_depth(node.body) for key, node in nodes.items()},
    }


def validate_quality_entries(kind: str, entries: object, scope: tuple[str, ...]) -> None:
    if not isinstance(entries, dict):
        raise ValueError(f"'{kind}' must be an object")  # noqa: TRY004 — invalid JSON schema
    ceiling = COMPLEXITY_HARD - 1 if kind == "complexity" else NESTING_CEILING
    for key, count in entries.items():
        path_text, separator, qualname = key.partition("::")
        path = Path(path_text)
        valid_path = (
            path.parts
            and not path.is_absolute()
            and path.as_posix() == path_text
            and ".." not in path.parts
            and path.parts[0] in scope
            and path.suffix == ".py"
        )
        if (
            not valid_path
            or not separator
            or not qualname
            or type(count) is not int
            or count <= ceiling
        ):
            raise ValueError(
                f"invalid {kind} entry {key!r}: expected a scoped .py path::qualname "
                f"and integer > {ceiling}"
            )


def quality_errors(
    measurements: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]]
) -> list[str]:
    errors: list[str] = []
    for kind, ceiling in (("complexity", COMPLEXITY_HARD - 1), ("nesting", NESTING_CEILING)):
        for key, value in measurements[kind].items():
            if value <= ceiling:
                continue
            if key not in baseline[kind]:
                reason = "new violation, not in the baseline — refactor it"
            elif value > baseline[kind][key]:
                reason = (
                    f"grew above its frozen baseline value ({baseline[kind][key]}) — refactor it"
                )
            else:
                continue
            errors.append(f"{key}: {kind} {value}: {reason}")
    return errors


def render_warnings(complexity: dict[str, int], *, full: bool = False) -> None:
    counts = Counter(
        key.partition("::")[0]
        for key, value in complexity.items()
        if COMPLEXITY_WARN <= value < COMPLEXITY_HARD
    )
    if not counts:
        return
    print(
        f"complexity warnings (cc 10-14, non-blocking): {sum(counts.values())} "
        f"functions in {len(counts)} files",
        file=sys.stderr,
    )
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    visible = ordered if full else ordered[:30]
    for path, count in visible:
        print(f"{path}: {count}", file=sys.stderr)
    remaining = ordered[len(visible) :]
    if remaining:
        print(
            f"rest: {len(remaining)} files / {sum(count for _, count in remaining)} functions",
            file=sys.stderr,
        )


def unpaired_additions(current: dict[str, int], previous: dict[str, int]) -> list[str]:
    """Maximum matching in each file's threshold bipartite graph of added/removed keys.

    Candidate neighborhoods are nested by value. Pairing the smallest addition
    with the smallest sufficient removal preserves every larger candidate, so
    this greedy matching is maximum without an augmenting-path search.
    """
    removals: dict[str, list[int]] = defaultdict(list)
    for key in previous.keys() - current.keys():
        removals[key.partition("::")[0]].append(previous[key])
    for candidates in removals.values():
        candidates.sort()
    unmatched: list[str] = []
    for key in sorted(current.keys() - previous.keys(), key=lambda key: (current[key], key)):
        candidates = removals[key.partition("::")[0]]
        match = next((i for i, value in enumerate(candidates) if value >= current[key]), None)
        if match is None:
            unmatched.append(key)
        else:
            candidates.pop(match)
    return unmatched
