#!/usr/bin/env python3
"""Make the number of tests CI actually ran visible: per shard, per directory, and in total.

The backend shards run `pytest -q`: the log is dots without test ids, and the JUnit report
goes to Trunk only. So "a moved test did not fall out of CI" could only be proved by counting
around the move (the total changes by exactly the parametrized ids added), once per batch.
This script reads what pytest already wrote, the shard's JUnit report, so nothing is
collected a second time:

    shard_counts.py shard --group 3 --junit 'tmp/junit-backend-shard-3-a*.xml' --out tmp/c3.json \\
                          --summary "$GITHUB_STEP_SUMMARY" [--min-tests 1]
        counts the tests one shard executed, by directory, into its log and the GitHub job
        summary, and writes them as JSON (the workflow uploads it as an artifact). It fails
        when the shard left no JUnit report or executed fewer than `--min-tests` tests: the
        Trunk uploader lets both pass, and this is the check that does not;

    shard_counts.py total --dir counts/ --expected "1 2 ... 16 serial" [--baseline b.json] \\
                          --out test-count-baseline.json --summary "$GITHUB_STEP_SUMMARY" \\
                          --leak-report test-leak-report.json \\
                          --sha "$GITHUB_SHA" --run-id "$GITHUB_RUN_ID"
        adds the shards' counts up and prints how the total, and each directory, moved against
        a baseline (the previous main run's total). Informational: it never fails a run over a
        difference. `--out` is written only when every expected count file arrived, so a
        baseline is never partial.

The root leak guard (`tests/fixtures/leak_guard.py`) writes each finding as a JUnit property of the
test that leaked. `shard` carries them into its count file (`leaks`, `notes`, `faults`; a clean
shard's file has none of those keys) and `total` reports them for the whole run from this ONE job:
the job summary, ONE `::warning title=leak guard` annotation of at most `ANNOTATION_LINES`
lines (read it with `gh api repos/R/check-runs/<job id>/annotations`; the job summary is not in
that API), and the full list as `--leak-report`. A fault in this reporting is one annotation,
never the job's result.

A directory is a bucket: `tests/<area>` for the top-level tests, and the outermost `<pkg>/tests`
for a package's own tests, the same directories `scripts/codegen/gen_pyright_test_environments.py`
lists.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def bucket_of(test_file: str) -> str:
    """The directory a test file is counted under."""
    parts = test_file.split("/")
    if "tests" not in parts[:-1]:
        raise SystemExit(f"a test outside every tests/ directory: {test_file}")
    index = parts.index("tests")
    if index == 0:
        return "tests" if len(parts) == 2 else f"tests/{parts[1]}"
    return "/".join(parts[: index + 1])


# The leak guard's JUnit properties -> the key their findings are kept under.
GUARD_PROPERTIES = {"leak_guard": "leaks", "leak_guard_note": "notes", "leak_guard_fault": "faults"}
ANNOTATION_LINES = 40  # the annotation is the digest; the artifact has the full list


def count_junit(path: Path) -> dict[str, Any]:
    """Tests, skips and failures in one JUnit report, the tests per directory, and the guard's findings."""
    buckets: dict[str, int] = {}
    guard: dict[
        str, list[dict[str, str]]
    ] = {}  # only the kinds that occurred: a clean report adds no key
    tests = skipped = failed = 0
    for _, element in ET.iterparse(path):  # noqa: S314 - pytest's own report, written by this job
        if element.tag != "testcase":
            continue
        test_file = element.get("file")
        if test_file is None:
            raise SystemExit(
                f"{path}: testcase {element.get('classname')}.{element.get('name')} has no `file` "
                "attribute (a pytest internal error, or -o junit_family=xunit1 is missing)"
            )
        tests += 1
        for prop in element.findall("properties/property"):
            key = GUARD_PROPERTIES.get(prop.get("name") or "")
            if key is not None:
                kind, _, detail = (prop.get("value") or "").partition(": ")
                finding = {
                    "test": f"{test_file}::{element.get('name')}",
                    "kind": kind,
                    "detail": detail,
                }
                guard.setdefault(key, []).append(finding)
        skipped += element.find("skipped") is not None
        failed += element.find("failure") is not None or element.find("error") is not None
        bucket = bucket_of(test_file)
        buckets[bucket] = buckets.get(bucket, 0) + 1
        element.clear()
    return {"tests": tests, "skipped": skipped, "failed": failed, "buckets": buckets, **guard}


def latest_report(pattern: str) -> Path:
    """The JUnit report of the last attempt: a shard's retry reruns the whole shard."""
    reports = sorted(glob.glob(pattern))  # noqa: PTH207
    if not reports:
        raise SystemExit(
            f"no JUnit report matches {pattern}: pytest wrote none (a collection error?)"
        )
    return Path(reports[-1])


def _table(rows: list[tuple[str, int]], head: tuple[str, str]) -> str:
    return "\n".join(
        [f"| {head[0]} | {head[1]} |", "| --- | ---: |"] + [f"| {a} | {b} |" for a, b in rows]
    )


def _emit(text: str, summary: str, summary_path: Path | None) -> None:
    """Print `text`; append the markdown `summary` to the job summary file when there is one."""
    print(text)
    if summary_path is not None:
        with summary_path.open("a", encoding="utf-8") as handle:
            handle.write(summary + "\n")


def run_shard(group: str, junit: str, out: Path, summary_path: Path | None, min_tests: int) -> int:
    report = latest_report(junit)
    counts: dict[str, Any] = {"group": group, **count_junit(report)}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(counts, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    rows = sorted(counts["buckets"].items())
    headline = (
        f"shard {group}: {counts['tests']} tests executed "
        f"({counts['skipped']} skipped, {counts['failed']} failed), from {report.name}"
    )
    if "leaks" in counts or "faults" in counts:
        leaks = counts.get("leaks", [])
        headline += (
            f"; leak guard: {len(leaks)} leak finding(s) in {len({x['test'] for x in leaks})} test(s), "
            f"{len(counts.get('faults', []))} guard fault(s)"
        )
    text = "\n".join([headline, *(f"  {name:<50} {n:>6}" for name, n in rows)])
    _emit(
        text,
        f"### Backend shard {group}: {counts['tests']} tests\n\n"
        + _table(rows, ("directory", "tests")),
        summary_path,
    )
    if counts["tests"] < min_tests:
        raise SystemExit(
            f"shard {group} executed {counts['tests']} tests, fewer than the {min_tests} it must "
            f"(pytest ran nothing: an empty `testpaths`, a deselecting marker, a plugin that "
            f"removed the items); {report} is its report"
        )
    return 0


def _diff(new: dict[str, int], old: dict[str, int]) -> list[tuple[str, int]]:
    return sorted(
        (name, new.get(name, 0) - old.get(name, 0))
        for name in new.keys() | old.keys()
        if new.get(name, 0) != old.get(name, 0)
    )


def _baseline_lines(total: int, buckets: dict[str, int], baseline: Path | None) -> list[str]:
    """How the total, and each directory, moved against the baseline (the previous main run)."""
    if baseline is None or not baseline.is_file():
        return ["  no baseline: no earlier main run left a total; this run becomes the baseline"]
    base = json.loads(baseline.read_text(encoding="utf-8"))
    lines = [
        f"  against main {base.get('sha', '?')[:9]} (run {base.get('run_id', '?')}): "
        f"{base['tests']} -> {total} ({total - base['tests']:+d})"
    ]
    changed = _diff(buckets, base["buckets"])
    lines.extend(f"    {name:<50} {change:>+6}" for name, change in changed)
    return lines if changed else [*lines, "    no directory changed"]


def _workflow_command(command: str, title: str, message: str) -> str:
    """A GitHub Actions workflow command on one line (`%`, CR and LF are the escapes of its data)."""
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::{command} title={title}::{escaped}"


def _guard_findings(arrived: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    """Every leak-guard finding of the run, each tagged with the shard whose report it came from."""
    return {
        key: [
            dict(x, shard=group) for group, counts in arrived.items() for x in counts.get(key, [])
        ]
        for key in GUARD_PROPERTIES.values()
    }


def _leak_lines(
    found: dict[str, list[dict[str, str]]], arrived: int, expected: list[str], missing: list[str]
) -> list[str]:
    """The run's leak-guard digest: a head, the count by kind, the guard's faults, one line per leak group."""
    leaks = found["leaks"]
    by_kind: dict[str, int] = {}
    for leak in leaks:
        by_kind[leak["kind"]] = by_kind.get(leak["kind"], 0) + 1
    incomplete = f" (INCOMPLETE: no file for {', '.join(missing)})" if missing else ""
    lines = [
        f"leak guard: {len(leaks)} leak finding(s) in {len({x['test'] for x in leaks})} test(s), "
        f"{len(found['notes'])} sys.path note(s), {len(found['faults'])} guard fault(s), "
        f"from {arrived}/{len(expected)} count files{incomplete}",
        "  by kind: " + (", ".join(f"{kind}={n}" for kind, n in sorted(by_kind.items())) or "none"),
    ]
    for stage in sorted({x["kind"] for x in found["faults"]}):
        faulted = [x for x in found["faults"] if x["kind"] == stage]
        lines.append(
            f"  GUARD FAULT [{stage}] in {len(faulted)} test report(s): {faulted[0]['detail']}"
        )
    # One line per (file, kind, thing leaked): a file whose every test leaks the same key is one line,
    # not fifty.
    groups: dict[tuple[str, str, str], set[str]] = {}
    for leak in leaks:
        file, _, name = leak["test"].partition("::")
        thing = leak["detail"].split(" ", 1)[0].rstrip(":")
        groups.setdefault((file, leak["kind"], thing), set()).add(name)
    for (file, kind, thing), names in sorted(groups.items()):
        lines.append(f"  {file}  [{kind}] {thing}  x{len(names)} test(s), e.g. {sorted(names)[0]}")
    return lines


def _report_leaks(
    arrived: dict[str, dict[str, Any]],
    expected: list[str],
    missing: list[str],
    args: argparse.Namespace,
) -> None:
    """The cluster-wide leaker list, readable from this ONE job without pulling any shard log.

    Three channels, same data: the job summary (humans), one `::warning` annotation (readable with
    `gh api repos/R/check-runs/<job id>/annotations`; the Actions job summary itself is NOT in the
    check-run API), and the `--leak-report` JSON (the full list, one artifact). The annotation is
    only raised when there is a leak or a guard fault to say.
    """
    found = _guard_findings(arrived)
    lines = _leak_lines(found, len(arrived), expected, missing)
    text = "\n".join(lines)
    _emit(text, f"### Leak guard\n\n```\n{text}\n```", args.summary)
    if args.leak_report is not None:
        payload = {
            "sha": args.sha,
            "run_id": args.run_id,
            "complete": not missing,
            "digest": lines,
            **found,
        }
        args.leak_report.write_text(
            json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
    if found["leaks"] or found["faults"]:
        shown = lines[:ANNOTATION_LINES]
        if len(lines) > ANNOTATION_LINES:
            shown = [
                *lines[: ANNOTATION_LINES - 1],
                f"  (+{len(lines) - ANNOTATION_LINES + 1} more: artifact test-leak-report)",
            ]
        print(_workflow_command("warning", "leak guard", "\n".join(shown)))


def run_total(args: argparse.Namespace) -> int:
    directory: Path = args.dir
    expected: list[str] = args.expected.split()
    baseline: Path | None = args.baseline
    out: Path | None = args.out
    arrived: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        counts = json.loads(path.read_text(encoding="utf-8"))
        arrived[counts["group"]] = counts
    missing = [group for group in expected if group not in arrived]
    buckets: dict[str, int] = {}
    for counts in arrived.values():
        for name, n in counts["buckets"].items():
            buckets[name] = buckets.get(name, 0) + n
    total = sum(counts["tests"] for counts in arrived.values())
    skipped = sum(counts["skipped"] for counts in arrived.values())
    lines = [
        f"CI executed {total} backend tests ({skipped} skipped), from "
        f"{len(arrived)}/{len(expected)} count files",
        "  per shard: " + ", ".join(f"{g}={arrived[g]['tests']}" for g in expected if g in arrived),
    ]
    if missing:
        lines.append(
            f"  INCOMPLETE: no count file for {', '.join(missing)}; the total is not comparable and "
            "is not recorded as a baseline"
        )
    else:
        lines.extend(_baseline_lines(total, buckets, baseline))
    text = "\n".join(lines)
    rows = sorted(buckets.items())
    _emit(
        text,
        f"### Backend tests executed by CI\n\n```\n{text}\n```\n\n"
        + _table(rows, ("directory", "tests")),
        args.summary,
    )
    if out is not None and not missing:
        payload = {
            "tests": total,
            "skipped": skipped,
            "buckets": buckets,
            "sha": args.sha,
            "run_id": args.run_id,
        }
        out.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    try:
        _report_leaks(arrived, expected, missing, args)
    except (
        Exception
    ) as exc:  # informational and last: a fault here is one annotation, never the job's result
        print(
            _workflow_command(
                "warning", "leak guard (report fault)", f"{type(exc).__name__}: {exc}"
            )
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    shard = commands.add_parser("shard", help="count one shard's JUnit report")
    shard.add_argument("--group", required=True)
    shard.add_argument("--junit", required=True, help="glob; the highest attempt wins")
    shard.add_argument("--out", type=Path, required=True)
    shard.add_argument("--summary", type=Path, help="the GitHub job summary file to append to")
    shard.add_argument(
        "--min-tests", type=int, default=0, help="fail when fewer tests than this were executed"
    )
    total = commands.add_parser(
        "total", help="add the shards' counts up and compare with a baseline"
    )
    total.add_argument("--dir", type=Path, required=True)
    total.add_argument("--expected", required=True, help="space-separated group names")
    total.add_argument("--baseline", type=Path)
    total.add_argument("--out", type=Path, help="write the total here as the next baseline")
    total.add_argument("--summary", type=Path, help="the GitHub job summary file to append to")
    total.add_argument(
        "--leak-report",
        type=Path,
        help="write the full leak-guard list here (uploaded as an artifact)",
    )
    total.add_argument("--sha", default="", help="the commit the total describes")
    total.add_argument("--run-id", default="", help="the workflow run the total describes")
    args = parser.parse_args(argv)
    if args.command == "shard":
        return run_shard(args.group, args.junit, args.out, args.summary, args.min_tests)
    return run_total(args)


if __name__ == "__main__":
    sys.exit(main())
