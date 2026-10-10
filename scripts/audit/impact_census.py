"""Measure how far changes reach through the PR test-selection impact graph.

Run: `python -m scripts.audit.impact_census [--top N] [--json]` from the repository
root (stdlib-only, read-only, no environment). It reports the numbers the locality
and selector workstreams track (issue #5128):

- source files every collectable test reaches, overall and per top-level package
  (locality drives this down; genuine foundation modules are its floor);
- inputs the analysis cannot bound, how many every test reaches, and the tests that
  reach any of them, which join every SELECTED subset (the selector side drives
  these down);
- the heaviest unbounded sites by the estimated time of the tests they pull in.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from scripts.ci.test_impact import Impact, build_impact
from scripts.ci.test_selector import (
    estimate_seconds,
    load_checkout,
    load_durations,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Site:
    """One unbounded input and the tests that reach it."""

    path: str
    line: int
    reason: str
    tests: int
    minutes: float


@dataclass(frozen=True)
class Census:
    """Reach and unbounded-input numbers for one checkout."""

    tests: int
    full_minutes: float
    source_files: int
    sources_reached_by_every_test: int
    by_package: dict[str, tuple[int, int]]  # package -> (files, reached by every test)
    unbounded_sites: int
    unbounded_sites_reached_by_every_test: int
    tests_reaching_unbounded: int
    tests_reaching_unbounded_minutes: float
    heaviest_unbounded: tuple[Site, ...]


def census(repo_root: Path, *, top: int = 10) -> Census:
    """Measure the impact graph of the checkout at ``repo_root``."""
    checkout = load_checkout(repo_root)
    tests = checkout.collectable
    impact = build_impact(checkout.repo_root, tests)
    durations = load_durations(checkout.repo_root / ".test_durations")
    reference = set(tests)

    def minutes(selected: set[str]) -> float:
        return round(estimate_seconds(selected, durations, reference_paths=reference) / 60, 1)

    total = len(tests)
    packages, everywhere = _package_reach(impact.tests_by_input, tests)
    by_site = _unbounded_sites(impact)
    tainted: set[str] = set()
    for consumers in by_site.values():
        tainted |= consumers
    heaviest = sorted(by_site.items(), key=lambda entry: (-minutes(entry[1]), entry[0]))[:top]
    return Census(
        tests=total,
        full_minutes=minutes(reference),
        source_files=sum(packages.values()),
        sources_reached_by_every_test=sum(everywhere.values()),
        by_package={name: (count, everywhere[name]) for name, count in sorted(packages.items())},
        unbounded_sites=len(by_site),
        unbounded_sites_reached_by_every_test=sum(1 for t in by_site.values() if len(t) == total),
        tests_reaching_unbounded=len(tainted),
        tests_reaching_unbounded_minutes=minutes(tainted),
        heaviest_unbounded=tuple(
            Site(path, line, reason, len(t), minutes(t)) for (path, line, reason), t in heaviest
        ),
    )


def _package_reach(
    tests_by_input: dict[str, set[str]], tests: frozenset[str]
) -> tuple[Counter[str], Counter[str]]:
    """Per top-level package: non-test source files, and those every test reaches."""
    packages: Counter[str] = Counter()
    everywhere: Counter[str] = Counter()
    for path, consumers in tests_by_input.items():
        if not path.endswith(".py") or "/" not in path or path in tests:
            continue
        if "/tests/" in path or path.startswith("tests/"):
            continue
        package = path.split("/", 1)[0]
        packages[package] += 1
        if len(consumers) == len(tests):
            everywhere[package] += 1
    return packages, everywhere


def _unbounded_sites(impact: Impact) -> dict[tuple[str, int, str], set[str]]:
    """Each unbounded input with the tests that reach the file holding it."""
    by_site: defaultdict[tuple[str, int, str], set[str]] = defaultdict(set)
    for item in impact.unknown:
        by_site[(item.path, item.line, item.reason)] |= impact.tests_by_input.get(item.path, set())
    return by_site


def _print(report: Census) -> None:
    print(
        f"sources reached by every test: {report.sources_reached_by_every_test}"
        f" / {report.source_files}"
    )
    for package, (files, everywhere) in report.by_package.items():
        print(f"  {package}: {everywhere} / {files}")
    print(
        f"unbounded sites: {report.unbounded_sites}"
        f" (reached by every test: {report.unbounded_sites_reached_by_every_test})"
    )
    print(
        f"tests reaching an unbounded site: {report.tests_reaching_unbounded} / {report.tests}"
        f" ({report.tests_reaching_unbounded_minutes} of {report.full_minutes} estimated minutes)"
    )
    for site in report.heaviest_unbounded:
        print(
            f"  {site.minutes:5.1f} min {site.tests:4d} tests  {site.path}:{site.line}: {site.reason}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure how far changes reach through the PR test-selection impact graph."
    )
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    parser.add_argument("--top", type=int, default=10, help="heaviest unbounded sites to list")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = census(args.repo_root.resolve(), top=args.top)
    if args.json:
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
    else:
        _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
