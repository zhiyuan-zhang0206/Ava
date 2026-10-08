#!/usr/bin/env python3
"""Plan duration refreshes from measured provenance, never successful no-op runs."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from base.deploy.git import gitenv
from base.host import proc

_SOURCE_FILE = ".test_durations.source.json"
_CHANGE_INTERVAL = 20
_BACKSTOP_WINDOW = timedelta(hours=5)
_SHA = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


class RefreshEvent(StrEnum):
    """Supported workflow triggers."""

    SCHEDULE = "schedule"
    MANUAL = "workflow_dispatch"
    CI = "workflow_run"


class RefreshMode(StrEnum):
    """The workflow's measurement routing contract."""

    SKIP = "skip"
    MEASURE = "measure"
    REUSE = "reuse"


class MeasurementSource(BaseModel):
    """Committed only with a complete, successfully merged timing snapshot."""

    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    source_sha: _SHA
    run_id: Annotated[int, Field(gt=0)]
    measured_at: AwareDatetime


class _Repository(BaseModel):
    full_name: str


class _SuccessfulMainCI(BaseModel):
    """Accept only trusted main-push CI artifacts at the receiving boundary."""

    model_config = ConfigDict(strict=True)
    name: Literal["CI"]
    path: Literal[".github/workflows/ci.yml"]
    event: Literal["push"]
    status: Literal["completed"]
    conclusion: Literal["success"]
    head_branch: Literal["main"]
    head_repository: _Repository
    head_sha: _SHA
    id: Annotated[int, Field(gt=0)]
    updated_at: AwareDatetime


class _CompletedCIEvent(BaseModel):
    action: Literal["completed"]
    workflow_run: _SuccessfulMainCI


def _git(*args: str) -> str:
    result = proc.run_bounded(
        ["git", *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=gitenv.git_env(),
    )
    result.check_returncode()
    return result.stdout.strip()


def _ancestor(older: str, newer: str) -> bool:
    result = proc.run_bounded(
        ["git", "merge-base", "--is-ancestor", older, newer],
        capture_output=True,
        text=True,
        timeout=30,
        env=gitenv.git_env(),
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr)
    return result.returncode == 0


def _source_at(ref: str) -> MeasurementSource | None:
    if not _git("ls-tree", "--name-only", ref, "--", _SOURCE_FILE):
        return None
    return MeasurementSource.model_validate_json(_git("show", f"{ref}:{_SOURCE_FILE}"))


def _input_source(args: argparse.Namespace) -> MeasurementSource:
    if args.event == RefreshEvent.CI:
        event = _CompletedCIEvent.model_validate_json(args.event_path.read_text())
        run = event.workflow_run
        if run.head_repository.full_name != args.repository:
            raise ValueError("duration artifacts must come from this repository's main CI")
        source = MeasurementSource(
            schema_version=1,
            source_sha=run.head_sha,
            run_id=run.id,
            measured_at=run.updated_at,
        )
    else:
        source = MeasurementSource(
            schema_version=1,
            source_sha=_git("rev-parse", "HEAD"),
            run_id=args.run_id,
            measured_at=datetime.now(UTC),
        )
    if not _ancestor(source.source_sha, "HEAD"):
        raise ValueError("measurement source is outside the checked-out main history")
    return source


def _decide(
    event: RefreshEvent,
    source: MeasurementSource,
    baseline: MeasurementSource | None,
) -> tuple[RefreshMode, str]:
    if event == RefreshEvent.MANUAL:
        return RefreshMode.MEASURE, "manual measurement requested"
    if baseline is None:
        mode = RefreshMode.REUSE if event == RefreshEvent.CI else RefreshMode.MEASURE
        return mode, "bootstrap: no published measurement provenance"
    if event == RefreshEvent.SCHEDULE:
        if datetime.now(UTC) - baseline.measured_at < _BACKSTOP_WINDOW:
            return RefreshMode.SKIP, "a complete measurement was published within five hours"
        return RefreshMode.MEASURE, "daily fallback measurement"
    if _ancestor(source.source_sha, baseline.source_sha):
        return RefreshMode.SKIP, "this CI source has already been measured or superseded"
    if not _ancestor(baseline.source_sha, source.source_sha):
        raise ValueError("published measurement and CI source have diverged")
    changes = int(
        _git("rev-list", "--first-parent", "--count", f"{baseline.source_sha}..{source.source_sha}")
    )
    if changes < _CHANGE_INTERVAL:
        return (
            RefreshMode.SKIP,
            f"{changes}/{_CHANGE_INTERVAL} main changes since the last measurement",
        )
    return RefreshMode.REUSE, f"{changes}/{_CHANGE_INTERVAL} main changes: reuse completed CI"


def _latest_source(
    applied: MeasurementSource | None,
    published: MeasurementSource | None,
) -> MeasurementSource | None:
    if applied is None:
        return published
    if published is None:
        return applied
    if applied.source_sha == published.source_sha:
        return max((applied, published), key=lambda item: item.measured_at)
    if _ancestor(applied.source_sha, published.source_sha):
        return published
    if _ancestor(published.source_sha, applied.source_sha):
        return applied
    raise ValueError("applied and published measurement histories have diverged")


def _plan(args: argparse.Namespace) -> None:
    event = RefreshEvent(args.event)
    source = _input_source(args)
    applied = _source_at("HEAD")
    published = _source_at(args.published_ref) if args.published_ref else None
    baseline = _latest_source(applied, published)
    if baseline is not None and not _ancestor(baseline.source_sha, "HEAD"):
        raise ValueError("published measurement is outside main history")
    mode, reason = _decide(event, source, baseline)
    with args.output.open("a") as output:
        output.write(
            f"mode={mode}\nsource-sha={source.source_sha}\nsource-run-id={source.run_id}\n"
            f"measured-at={source.measured_at.isoformat()}\n"
        )
    summary = (
        f"Duration refresh: **{mode}** — {reason}.\n\n"
        f"Source: `{source.source_sha}` "
        f"([run {source.run_id}](https://github.com/{args.repository}/actions/runs/{source.run_id})).\n\n"
        f"Applied on main: `{applied.source_sha if applied else 'unknown'}`.\n\n"
        f"Applied measurement time: {applied.measured_at.isoformat() if applied else 'unknown'}.\n\n"
        f"Published for review: `{published.source_sha if published else 'none'}`.\n"
        f"Published measurement time: {published.measured_at.isoformat() if published else 'unknown'}.\n"
    )
    print(summary)
    with args.summary.open("a") as output:
        output.write(summary)


def main(argv: list[str] | None = None) -> int:
    """Plan a workflow or stamp an already validated complete measurement."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--event", choices=list(RefreshEvent), required=True)
    plan.add_argument("--event-path", type=Path, required=True)
    plan.add_argument("--repository", required=True)
    plan.add_argument("--run-id", type=int, required=True)
    plan.add_argument("--published-ref")
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--summary", type=Path, required=True)
    stamp = commands.add_parser("stamp")
    stamp.add_argument("--source-sha", required=True)
    stamp.add_argument("--run-id", type=int, required=True)
    stamp.add_argument("--measured-at", required=True)
    args = parser.parse_args(argv)
    if args.command == "plan":
        _plan(args)
    else:
        source = MeasurementSource.model_validate_json(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_sha": args.source_sha,
                    "run_id": args.run_id,
                    "measured_at": args.measured_at,
                }
            )
        )
        Path(_SOURCE_FILE).write_text(source.model_dump_json(indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
