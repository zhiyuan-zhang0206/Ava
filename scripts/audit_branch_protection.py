#!/usr/bin/env python3
"""Compare GitHub's live branch protections with Ava's Mergify declaration.

The repository declares its merge gate in ``.mergify.yml``, but GitHub stores
required checks and workflow activation outside git. This read-only audit makes
that external state observable and distinguishes drift from an API/tool failure.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from urllib.parse import quote

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MERGIFY_FILE = _REPO_ROOT / ".mergify.yml"
_CHECK_CONDITION = re.compile(r"check-(?:success|skipped)=(.+)")
_GITHUB_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_REMOTE_REPO = re.compile(r"(?:github\.com[:/])([^/]+)/([^/]+?)(?:\.git)?$")
_REQUIRED_WORKFLOWS = (
    ".github/workflows/ci.yml",
    ".github/workflows/ci-rerun.yml",
)


def _mapping(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{location} must be a mapping")
    mapping = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise TypeError(f"{location} keys must be strings")
    return cast(dict[str, object], mapping)


def _list(value: object, location: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{location} must be a list")
    return cast(list[object], value)


def _condition_strings(value: object, location: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        items = cast(list[object], value)
        conditions: list[str] = []
        for index, item in enumerate(items):
            conditions.extend(_condition_strings(item, f"{location}[{index}]"))
        return conditions
    if isinstance(value, dict):
        condition_group = _mapping(cast(object, value), location)
        if set(condition_group) != {"or"}:
            raise ValueError(f"{location} must contain exactly one 'or' group")
        return _condition_strings(condition_group["or"], f"{location}.or")
    raise ValueError(f"{location} must be a condition string, list, or 'or' group")


def _checks_in_conditions(value: object, location: str) -> frozenset[str]:
    checks: set[str] = set()
    for condition in _condition_strings(value, location):
        match = _CHECK_CONDITION.fullmatch(condition)
        if match is not None:
            checks.add(match.group(1))
        elif condition.startswith("check-"):
            raise ValueError(f"unsupported check condition in {location}: {condition}")
    return frozenset(checks)


def _declared_checks(text: str) -> tuple[frozenset[str], frozenset[str]]:
    document: object = yaml.safe_load(text)
    root = _mapping(document, ".mergify.yml")
    if "queue_rules" not in root:
        raise ValueError(".mergify.yml is missing queue_rules")
    rules = _list(root["queue_rules"], "queue_rules")
    if not rules:
        raise ValueError("queue_rules must not be empty")

    queue_checks: set[str] = set()
    merge_checks: set[str] = set()
    for index, value in enumerate(rules):
        rule = _mapping(value, f"queue_rules[{index}]")
        for key, destination in (
            ("queue_conditions", queue_checks),
            ("merge_conditions", merge_checks),
        ):
            if key not in rule:
                raise ValueError(f"queue_rules[{index}] is missing {key}")
            destination.update(_checks_in_conditions(rule[key], f"queue_rules[{index}].{key}"))

    if not queue_checks and not merge_checks:
        raise ValueError("queue_rules declare no required checks")
    return frozenset(queue_checks), frozenset(merge_checks)


def expected_checks(text: str) -> frozenset[str]:
    """Return the union of checks declared for queueing and merging."""
    queue_checks, merge_checks = _declared_checks(text)
    return queue_checks | merge_checks


def declaration_drift(text: str) -> list[str]:
    """Report any mismatch between queue-time and merge-time check names."""
    queue_checks, merge_checks = _declared_checks(text)
    findings: list[str] = []
    queue_only = queue_checks - merge_checks
    merge_only = merge_checks - queue_checks
    if queue_only:
        findings.append(
            "queue_conditions has checks absent from merge_conditions: "
            + ", ".join(sorted(queue_only))
        )
    if merge_only:
        findings.append(
            "merge_conditions has checks absent from queue_conditions: "
            + ", ".join(sorted(merge_only))
        )
    return findings


def _string_set(value: object, location: str) -> frozenset[str]:
    items = _list(value, location)
    if not all(isinstance(item, str) for item in items):
        raise TypeError(f"{location} entries must be strings")
    return frozenset(cast(list[str], items))


def _boolean(value: object, location: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{location} must be a boolean")
    return value


def drift_report(
    protection_payload: Mapping[str, object],
    expected: frozenset[str],
    workflows_payload: Mapping[str, object],
) -> list[str]:
    """Return human-readable differences from the branch-protection contract."""
    findings: list[str] = []

    if (
        "required_status_checks" not in protection_payload
        or protection_payload["required_status_checks"] is None
    ):
        findings.append("branch protection has no required_status_checks")
    else:
        status_checks = _mapping(
            protection_payload["required_status_checks"], "required_status_checks"
        )
        contexts = _string_set(status_checks["contexts"], "required_status_checks.contexts")
        missing = expected - contexts
        extra = contexts - expected
        if missing:
            findings.append("missing required status checks: " + ", ".join(sorted(missing)))
        if extra:
            findings.append("extra required status checks: " + ", ".join(sorted(extra)))
        if _boolean(status_checks["strict"], "required_status_checks.strict"):
            findings.append("required status checks must have strict=false")

    enforce_admins = _mapping(protection_payload["enforce_admins"], "enforce_admins")
    if not _boolean(enforce_admins["enabled"], "enforce_admins.enabled"):
        findings.append("branch protection must enforce rules for administrators")

    workflow_entries = _list(workflows_payload["workflows"], "workflows")
    live_workflows: dict[str, str] = {}
    for index, value in enumerate(workflow_entries):
        workflow = _mapping(value, f"workflows[{index}]")
        path = workflow["path"]
        state = workflow["state"]
        if not isinstance(path, str) or not isinstance(state, str):
            raise TypeError(f"workflows[{index}].path and state must be strings")
        if path in live_workflows:
            raise ValueError(f"duplicate workflow path in GitHub response: {path}")
        live_workflows[path] = state

    for path in _REQUIRED_WORKFLOWS:
        name = Path(path).name
        if path not in live_workflows:
            findings.append(f"workflow {name} is missing on GitHub")
        elif live_workflows[path] != "active":
            findings.append(f"workflow {name} is not active (state={live_workflows[path]})")

    return findings


def derive_repo() -> str:
    """Derive ``owner/repo`` from this checkout's origin remote."""
    completed = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    match = _REMOTE_REPO.search(completed.stdout.strip())
    if match is None:
        raise ValueError("origin is not a GitHub repository URL")
    return f"{match.group(1)}/{match.group(2)}"


def run_gh(args: list[str]) -> str:
    """Run one read-only ``gh`` invocation and return its stdout."""
    completed = subprocess.run(  # noqa: S603
        ["gh", *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _json_mapping(text: str, location: str) -> dict[str, object]:
    payload: object = json.loads(text)
    return _mapping(payload, location)


def _audit(repo: str, branch: str) -> list[str]:
    if _GITHUB_REPO.fullmatch(repo) is None:
        raise ValueError("--repo must have the form owner/repo")
    if not branch:
        raise ValueError("--branch must not be empty")

    declaration = _MERGIFY_FILE.read_text()
    expected = expected_checks(declaration)
    declaration_findings = declaration_drift(declaration)
    if declaration_findings:
        return declaration_findings
    protection = _json_mapping(
        run_gh(
            [
                "api",
                "--method",
                "GET",
                f"repos/{repo}/branches/{quote(branch, safe='')}/protection",
            ]
        ),
        "branch protection response",
    )
    workflows = _json_mapping(
        run_gh(
            [
                "api",
                "--method",
                "GET",
                f"repos/{repo}/actions/workflows",
            ]
        ),
        "workflows response",
    )
    return drift_report(protection, expected, workflows)


def _print_report(result: str, findings: list[str], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"result": result, "findings": findings}, sort_keys=True))
    elif result == "ok":
        print("Branch protection audit: no drift detected.")
    else:
        print(f"Branch protection audit: {result}.")
        for finding in findings:
            print(f"- {finding}")
    print(f"AUDIT_RESULT={result}")


def _error_detail(error: BaseException) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        stderr = error.stderr.strip() if isinstance(error.stderr, str) else ""
        return stderr or str(error)
    return str(error)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="GitHub repository as owner/repo")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    try:
        repo = args.repo if args.repo is not None else derive_repo()
        findings = _audit(repo, args.branch)
    except (
        FileNotFoundError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        yaml.YAMLError,
        subprocess.CalledProcessError,
    ) as error:
        _print_report("error", [_error_detail(error)], as_json=args.as_json)
        return 2

    if findings:
        _print_report("drift", findings, as_json=args.as_json)
        return 1
    _print_report("ok", [], as_json=args.as_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
