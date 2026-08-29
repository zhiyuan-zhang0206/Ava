#!/usr/bin/env python3
"""Backend coverage gates — core-domain line rate + per-risk-domain floors.

Reads coverage.json (written by `uv run coverage json -o coverage.json` from
the combined shard data in the backend CI job) and enforces two tiers:

- the combined line-rate gate over the six core domains
  (agent/ava/cli/gateway/shared/ui) — the legacy 85% gate, unchanged;
- per-risk-domain minimum line floors for ops/services/ava_builtins — the
  high-incident operational domains (deploy/backup/watchdog/ops) the
  combined gate never scored (tech audit 2026-08-24 finding #10).

Floors are set just below each domain's measured baseline at gate
introduction, so a regression below current coverage fails while
shard/flake variance passes. A floored domain with zero measured lines
fails regardless of its floor value: an empty domain (renamed package,
broken source glob) must be loud, never a vacuous pass. The measured
per-domain table is always printed (calibration + failure diagnosis);
exits 1 when any gate fails.

Usage: uv run python scripts/coverage_gates.py [coverage.json]
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

# The 85% gate denominator — must match [tool.coverage.run].source's core
# set and the comment in ci.yml's gate step. ops/services/ava_builtins are
# measured into the same data but gated by FLOORS, not by this number.
CORE_DOMAINS = ("agent", "ava", "cli", "gateway", "shared", "ui")
CORE_THRESHOLD_DEFAULT = 85.0

# Per-risk-domain minimum line floors (percent). Each key is a domain
# prefix: top-level packages ("ops") or second-level subdomains
# ("services/pitr"). A prefix matches every reported file whose path is the
# prefix itself or starts with "prefix/".
#
# Calibrated 2026-08-29 from the first measuring CI run (PR #965):
# ops 92.6% / services 73.1% / ava_builtins 81.6%. Floors sit 2-4 points
# below the measured baseline — enough buffer for shard-split and flake
# variance, tight enough that a real regression (a critical module losing
# its tests) trips the gate.
#
# ops is the deploy/rollout/cluster-lifecycle surface (highest incident
# exposure — a broken upgrade takes the fleet down), so it keeps the
# tightest buffer; services carries the data-durability daemons
# (backup/pitr/watchdog) and ava_builtins the plugins.
FLOORS: dict[str, float] = {
    "ops": 90.0,
    "services": 70.0,
    "ava_builtins": 78.0,
}


def _domain_of(path: str) -> str:
    return path.split("/", 1)[0]


def _subdomain_of(path: str) -> str:
    parts = path.split("/")
    return "/".join(parts[:2]) if len(parts) > 1 else parts[0]


def _matches_prefix(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _aggregate(files: dict[str, dict]) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """Aggregate covered/valid line counts from the coverage.json file map.

    Returns (per_domain, per_subdomain): per_domain keys are top-level
    packages ("ops"); per_subdomain keys are the first two path segments
    ("services/pitr") — both maps hold [covered, valid] pairs.
    """
    per_domain: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    per_subdomain: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for path, entry in files.items():
        summary = entry["summary"]
        covered = int(summary["covered_lines"])
        valid = int(summary["num_statements"])
        per_domain[_domain_of(path)][0] += covered
        per_domain[_domain_of(path)][1] += valid
        per_subdomain[_subdomain_of(path)][0] += covered
        per_subdomain[_subdomain_of(path)][1] += valid
    return per_domain, per_subdomain


def _rate(covered: int, valid: int) -> float:
    return round(covered * 100.0 / valid, 1) if valid else 0.0


def _prefix_rate(
    per_domain: dict[str, list[int]],
    per_subdomain: dict[str, list[int]],
    prefix: str,
) -> tuple[float, int, int]:
    """Aggregated (rate, covered, valid) for a domain prefix."""
    covered = valid = 0
    table = per_subdomain if "/" in prefix else per_domain
    for name, (c, v) in table.items():
        if _matches_prefix(name, prefix):
            covered += c
            valid += v
    return _rate(covered, valid), covered, valid


def _core_rate(per_domain: dict[str, list[int]]) -> tuple[float, int, int]:
    covered = sum(per_domain[d][0] for d in CORE_DOMAINS)
    valid = sum(per_domain[d][1] for d in CORE_DOMAINS)
    return _rate(covered, valid), covered, valid


def check(files: dict[str, dict], threshold: float) -> int:
    """Enforce the gates against a coverage.json `files` map.

    Returns 0 when every gate passes, 1 otherwise. Always prints the
    per-domain table.
    """
    per_domain, per_subdomain = _aggregate(files)
    failures: list[str] = []

    core_rate, core_covered, core_valid = _core_rate(per_domain)
    print(
        f"core domains {'+'.join(CORE_DOMAINS)}: {core_rate}% "
        f"({core_covered}/{core_valid} lines) — threshold {threshold}%"
    )
    if core_rate < threshold:
        failures.append(f"core combined coverage {core_rate}% below {threshold}%")

    print("all measured domains (covered/total lines):")
    for name, (covered, valid) in sorted(per_domain.items(), key=lambda kv: kv[1][1], reverse=True):
        note = ""
        if name in CORE_DOMAINS:
            note = " — core gate"
        elif name in FLOORS:
            rate = _rate(covered, valid)
            verdict = "ok" if rate >= FLOORS[name] else f"FAIL (floor {FLOORS[name]}%)"
            note = f" — floor {FLOORS[name]}% {verdict}"
        print(f"  {name:>20} {_rate(covered, valid):>6}% ({covered}/{valid}){note}")

    floored_subdomains = [p for p in FLOORS if "/" in p]
    if floored_subdomains:
        print("floored subdomains:")
        for prefix in floored_subdomains:
            rate, covered, valid = _prefix_rate(per_domain, per_subdomain, prefix)
            verdict = "ok" if rate >= FLOORS[prefix] else f"FAIL (floor {FLOORS[prefix]}%)"
            print(
                f"  {prefix:>20} {rate:>6}% ({covered}/{valid}) — floor {FLOORS[prefix]}% {verdict}"
            )

    for prefix, floor in FLOORS.items():
        rate, covered, valid = _prefix_rate(per_domain, per_subdomain, prefix)
        if valid == 0:
            # Existence check, independent of the floor value: a floored
            # domain with no measured lines means the package vanished or
            # the source glob broke — fail even at floor 0.0, never a
            # vacuous pass.
            failures.append(
                f"{prefix} has no measured lines — package missing or source glob broken"
            )
            continue
        if rate < floor:
            failures.append(f"{prefix} coverage {rate}% below floor {floor}%")

    if failures:
        print("coverage gates FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("coverage gates passed")
    return 0


def main() -> int:
    json_path = sys.argv[1] if len(sys.argv) > 1 else "coverage.json"
    threshold = float(os.environ.get("BACKEND_COVERAGE_THRESHOLD", CORE_THRESHOLD_DEFAULT))
    with Path(json_path).open() as f:
        data = json.load(f)
    return check(data["files"], threshold)


if __name__ == "__main__":
    sys.exit(main())
