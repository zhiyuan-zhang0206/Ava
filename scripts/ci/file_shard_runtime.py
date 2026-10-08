"""Compare complete paired runtime evidence; missing or lossy evidence fails."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from scripts.ci.file_shard_shadow import Plan, RuntimeNode, RuntimeReport


class CoverageFile(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    executed_lines: list[int]
    missing_lines: list[int]


class CoverageReport(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    files: dict[str, CoverageFile]


def _same_generation(report: RuntimeReport, plan: Plan, group: int) -> None:
    actual = (
        report.group,
        report.pytest_version,
        report.pytest_split_version,
        report.durations_sha256,
        report.pytest_config_sha256,
    )
    expected = (
        group,
        plan.pytest_version,
        plan.pytest_split_version,
        plan.durations_sha256,
        plan.pytest_config_sha256,
    )
    if actual != expected or report.exitstatus != 0:
        raise ValueError(
            f"Failed or foreign runtime generation: group {group}, worker {report.worker}"
        )


def _group_nodes(
    directory: Path, plan: Plan, group: int, workers: int
) -> tuple[dict[str, RuntimeNode], list[float]]:
    paths = sorted(directory.glob("runtime-*.json"))
    reports = [RuntimeReport.model_validate_json(path.read_bytes()) for path in paths]
    expected_workers = {"controller", *(f"gw{index}" for index in range(workers))}
    if (
        len(reports) != len(expected_workers)
        or {report.worker for report in reports} != expected_workers
    ):
        raise ValueError(f"Missing or duplicate worker evidence in {directory}")
    nodes: dict[str, RuntimeNode] = {}
    collection: list[float] = []
    for report in reports:
        _same_generation(report, plan, group)
        if nodes.keys() & report.nodes.keys():
            raise ValueError(f"Duplicate execution in {directory}")
        nodes.update(report.nodes)
        if report.worker != "controller":
            collection.append(report.collection_seconds)
    return nodes, collection


def _population(
    directory: Path, plan: Plan, workers: int, *, candidate: bool
) -> tuple[dict[str, RuntimeNode], list[float]]:
    nodes: dict[str, RuntimeNode] = {}
    times: list[float] = []
    for index, group in enumerate(plan.groups, 1):
        actual, collection = _group_nodes(directory / str(index), plan, index, workers)
        expected_count = len(group.nodes) if candidate else plan.baseline[index - 1].node_count
        if len(actual) != expected_count or nodes.keys() & actual.keys():
            raise ValueError(f"Wrong or repeated group population: {directory / str(index)}")
        if candidate and actual.keys() != {node.nodeid for node in group.nodes}:
            raise ValueError(f"Executed candidate differs from its plan: group {index}")
        nodes.update(actual)
        times.extend(collection)
    expected = {node.nodeid for group in plan.groups for node in group.nodes}
    if nodes.keys() != expected:
        raise ValueError(f"Missing or extra executed nodes in {directory}")
    return nodes, times


def _coverage_changes(baseline: Path, candidate: Path) -> dict[str, list[int]]:
    before = CoverageReport.model_validate_json(baseline.read_bytes()).files
    after = CoverageReport.model_validate_json(candidate.read_bytes()).files
    if not before or before.keys() != after.keys():
        raise ValueError("Coverage source population changed or is empty")
    extra: dict[str, list[int]] = {}
    for name, original in before.items():
        updated = after[name]
        statements = set(original.executed_lines + original.missing_lines)
        if statements != set(updated.executed_lines + updated.missing_lines):
            raise ValueError(f"Coverage statement population changed: {name}")
        lost = set(original.executed_lines) - set(updated.executed_lines)
        if lost:
            raise ValueError(f"Candidate lost covered lines in {name}: {sorted(lost)}")
        added = sorted(set(updated.executed_lines) - set(original.executed_lines))
        if added:
            extra[name] = added
    return extra


def compare(plan: Plan, directory: Path, workers: int) -> dict[str, object]:
    if workers < 1:
        raise ValueError("Worker count must be positive")
    baseline, baseline_times = _population(directory / "baseline", plan, workers, candidate=False)
    candidate, candidate_times = _population(directory / "candidate", plan, workers, candidate=True)
    changed = sorted(
        nodeid
        for nodeid, original in baseline.items()
        if original.fixtures != candidate[nodeid].fixtures
        or original.outcomes != candidate[nodeid].outcomes
    )
    if changed:
        raise ValueError(f"Runtime fixture bindings or outcomes changed: {changed}")
    incomplete = [
        nodeid
        for nodeid, node in candidate.items()
        if node.outcomes.get("setup") != "skipped"
        and set(node.outcomes) != {"setup", "call", "teardown"}
    ]
    if incomplete or any("failed" in node.outcomes.values() for node in candidate.values()):
        raise ValueError(f"Failed or incomplete test execution: {incomplete}")
    added_coverage = _coverage_changes(
        directory / "baseline/coverage.json", directory / "candidate/coverage.json"
    )
    return {
        "matched": True,
        "node_count": len(candidate),
        "groups": len(plan.groups),
        "workers_per_group": workers,
        "baseline_collection_seconds": baseline_times,
        "candidate_collection_seconds": candidate_times,
        "additional_covered_lines": added_coverage,
        "baseline_test_seconds": sum(sum(node.seconds.values()) for node in baseline.values()),
        "candidate_test_seconds": sum(sum(node.seconds.values()) for node in candidate.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.unlink(missing_ok=True)
    result = compare(Plan.model_validate_json(args.plan.read_bytes()), args.reports, args.workers)
    args.out.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
