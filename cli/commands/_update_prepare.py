"""Prepare-gate details and operator-facing verdicts for cluster rollouts."""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from cli.commands._update_dryrun import PrepareResult


@dataclass(frozen=True)
class PrepareGate:
    """Prepared evidence plus the maintenance-window context shown to operators."""

    prepared: PrepareResult
    breakdown_line: str
    estimate_note: str | None


def build_prepare_gate(
    prepare_runner: Callable[..., PrepareResult],
    repo: Path,
    target_sha: str | None,
    *,
    pull: bool,
    snapshotter: Callable[..., object],
    check_runner: Callable[..., list[str]],
    estimate_runner: Callable[[], float],
    breakdown_runner: Callable[..., Mapping[str, float]],
    note_runner: Callable[..., str | None],
    persist_seed: bool,
) -> PrepareGate:
    """Prepare a commit and calculate the exact estimate context from the same baseline."""
    prepared = prepare_runner(
        repo,
        target_sha,
        pull=pull,
        snapshotter=snapshotter,
        check_runner=check_runner,
        estimate_runner=estimate_runner,
    )
    breakdown = breakdown_runner(persist_seed=persist_seed)
    return PrepareGate(
        prepared=prepared,
        breakdown_line=", ".join(f"{name}={duration:.1f}s" for name, duration in breakdown.items()),
        estimate_note=note_runner(persist_seed=persist_seed),
    )


def print_dry_run_verdict(gate: PrepareGate) -> int:
    """Print the dry-run verdict and return its shell exit status."""
    verdict = "PASS" if not gate.prepared.failures and gate.prepared.estimate_s < 120.0 else "FAIL"
    estimate_context = f"; {gate.estimate_note}" if gate.estimate_note is not None else ""
    print(
        f"\n→ prepare dry-run: {verdict} (estimate {gate.prepared.estimate_s:.1f}s; "
        f"{gate.breakdown_line}{estimate_context})"
    )
    for failure in gate.prepared.failures:
        print(f"  ✗ {failure}")
    return 0 if verdict == "PASS" else 1


def refuse_normal_prepare(gate: PrepareGate) -> int | None:
    """Print a normal-run prepare refusal, if any; otherwise allow commit."""
    if gate.prepared.failures:
        print("\n✗ prepare checks failed; refusing maintenance:", file=sys.stderr)
        for failure in gate.prepared.failures:
            print(f"  · {failure}", file=sys.stderr)
        return 1
    if gate.prepared.estimate_s < 120.0:
        return None
    estimate_context = f"; {gate.estimate_note}" if gate.estimate_note is not None else ""
    print(
        f"\n✗ estimated maintenance window {gate.prepared.estimate_s:.1f}s is at least 120.0s; "
        f"refusing commit before Phase A ({gate.breakdown_line}{estimate_context})",
        file=sys.stderr,
    )
    return 1
