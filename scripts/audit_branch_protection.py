#!/usr/bin/env python3
"""Compare GitHub's live branch protections with Ava's Trunk merge-queue declaration.

The repository declares its merge gate in ``.trunk/trunk.yaml`` (queue testing
gate) plus the admission gate ruled in P2 (three suite aggregators +
``qa-approved-gate``); GitHub stores required checks and workflow activation
outside git. This read-only audit makes that external state observable and
distinguishes drift from an API/tool failure.
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
_TRUNK_FILE = _REPO_ROOT / ".trunk" / "trunk.yaml"
# P2 ruling (2026-09-02): the branch-protection admission gate is the three
# suite aggregators (the names Mergify also keyed on) plus the qa-approved
# label gate. The queue TESTING gate lives in trunk.yaml (12 statuses) and is
# audited separately by trunk_gate_findings().
_ADMISSION_CHECKS = frozenset(
    {
        "backend (pytest + pyright)",
        "frontend (eslint + tsc + vitest)",
        "e2e (Playwright happy path)",
        "qa-approved-gate",
    }
)
_MATRIX_TEMPLATE = re.compile(r"\$\{\{ matrix")
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


def expected_checks() -> frozenset[str]:
    """Return the branch-protection admission gate (P2 ruling)."""
    return _ADMISSION_CHECKS


def trunk_gate_findings(text: str) -> list[str]:
    """Audit the queue TESTING gate declared in .trunk/trunk.yaml.

    Invariants: required_statuses must be present and non-empty, carry the
    qa-approved-gate and the three suite aggregators, and must not contain raw
    matrix template names (a fully-skipped matrix reports only the template
    name, so such an entry could stall the queue forever on docs-only PRs).
    """
    document: object = yaml.safe_load(text)
    root = _mapping(document, ".trunk/trunk.yaml")
    if "merge" not in root:
        return ["trunk.yaml is missing merge.required_statuses"]
    merge = _mapping(root["merge"], "merge")
    if "required_statuses" not in merge:
        return ["trunk.yaml is missing merge.required_statuses"]
    statuses = _string_set(merge["required_statuses"], "merge.required_statuses")
    if not statuses:
        return ["trunk.yaml merge.required_statuses must not be empty"]

    findings: list[str] = []
    if "qa-approved-gate" not in statuses:
        findings.append("trunk.yaml required_statuses missing qa-approved-gate")
    for aggregator in (
        "backend (pytest + pyright)",
        "frontend (eslint + tsc + vitest)",
        "e2e (Playwright happy path)",
    ):
        if aggregator not in statuses:
            findings.append(f"trunk.yaml required_statuses missing {aggregator}")
    for name in sorted(statuses):
        if _MATRIX_TEMPLATE.search(name):
            findings.append(f"trunk.yaml required_statuses contains a matrix template name: {name}")
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

    trunk_text = _TRUNK_FILE.read_text()
    declaration_findings = trunk_gate_findings(trunk_text)
    if declaration_findings:
        return declaration_findings
    expected = expected_checks()
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
