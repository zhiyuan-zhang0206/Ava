from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import pytest

from scripts.audit import branch_protection as audit

_ADMISSION_CHECKS = frozenset(
    {
        "backend (pytest + pyright)",
        "frontend (eslint + tsc + vitest)",
        "e2e (Playwright happy path)",
        "doc lints (pre-commit family)",
    }
)
_EXPECTED_CHECKS = _ADMISSION_CHECKS  # legacy alias for the drift tests below

_TRUNK_FIXTURE = """
version: 0.1
merge:
  required_statuses:
    - backend (pytest + pyright)
    - backend serial (flaky)
    - backend static (ruff + pyright + migration smoke)
    - backend structure (pre-commit lint + codegen freshness)
    - backend vendored pgvector smoke (Linux)
    - classify change
    - e2e (Playwright happy path)
    - e2e env guard (serial)
    - e2e hosted (agent-runner-as-server happy path)
    - frontend (eslint + tsc + vitest)
    - repo language (no raw CJK)
    - secret scan (Gitleaks)
"""

_TRUNK_BAD_FIXTURE = """
version: 0.1
merge:
  required_statuses:
    - backend (pytest + pyright)
    - backend shard (${{ matrix.group }}/16)
"""


def _protection(
    contexts: list[str] | None = None,
    *,
    strict: bool = False,
    enforce_admins: bool = True,
) -> dict[str, object]:
    return {
        "required_status_checks": {
            "contexts": list(_EXPECTED_CHECKS) if contexts is None else contexts,
            "strict": strict,
        },
        "enforce_admins": {"enabled": enforce_admins},
    }


def _workflows(*, ci_state: str = "active", rerun_state: str = "active") -> dict[str, object]:
    return {
        "workflows": [
            {"path": ".github/workflows/ci.yml", "state": ci_state},
            {"path": ".github/workflows/ci-rerun.yml", "state": rerun_state},
        ]
    }


def _fake_gh(protection: dict[str, object], workflows: dict[str, object]):
    def fake(args: list[str]) -> str:
        endpoint = args[-1]
        if endpoint.endswith("/protection"):
            return json.dumps(protection)
        if endpoint.endswith("/actions/workflows"):
            return json.dumps(workflows)
        raise AssertionError(f"unexpected gh API endpoint: {endpoint}")

    return fake


def test_expected_checks_is_the_trunk_admission_gate() -> None:
    assert audit.expected_checks() == _ADMISSION_CHECKS
    assert "doc lints (pre-commit family)" in _ADMISSION_CHECKS


def test_trunk_gate_findings_reject_matrix_template_names() -> None:
    findings = audit.trunk_gate_findings(_TRUNK_BAD_FIXTURE)
    assert (
        "trunk.yaml required_statuses contains a matrix template name: "
        "backend shard (${{ matrix.group }}/16)"
    ) in findings


def test_drift_report_accepts_the_exact_live_contract() -> None:
    assert audit.drift_report(_protection(), _EXPECTED_CHECKS, _workflows()) == []


def test_drift_report_names_missing_contexts() -> None:
    contexts = sorted(_EXPECTED_CHECKS - {"backend (pytest + pyright)"})
    assert audit.drift_report(_protection(contexts), _EXPECTED_CHECKS, _workflows()) == [
        "missing required status checks: backend (pytest + pyright)"
    ]


def test_drift_report_names_extra_contexts() -> None:
    contexts = [*_EXPECTED_CHECKS, "legacy"]
    assert audit.drift_report(_protection(contexts), _EXPECTED_CHECKS, _workflows()) == [
        "extra required status checks: legacy"
    ]


def test_drift_report_rejects_strict_status_checks() -> None:
    assert audit.drift_report(_protection(strict=True), _EXPECTED_CHECKS, _workflows()) == [
        "required status checks must have strict=false"
    ]


def test_drift_report_requires_admin_enforcement() -> None:
    assert audit.drift_report(
        _protection(enforce_admins=False), _EXPECTED_CHECKS, _workflows()
    ) == ["branch protection must enforce rules for administrators"]


def test_drift_report_detects_missing_required_status_checks() -> None:
    protection = {"enforce_admins": {"enabled": True}}
    assert audit.drift_report(protection, _EXPECTED_CHECKS, _workflows()) == [
        "branch protection has no required_status_checks"
    ]


def test_drift_report_detects_a_missing_workflow() -> None:
    workflows = {"workflows": [{"path": ".github/workflows/ci.yml", "state": "active"}]}
    assert audit.drift_report(_protection(), _EXPECTED_CHECKS, workflows) == [
        "workflow ci-rerun.yml is missing on GitHub"
    ]


def test_drift_report_detects_an_inactive_workflow() -> None:
    assert audit.drift_report(
        _protection(), _EXPECTED_CHECKS, _workflows(ci_state="disabled_manually")
    ) == ["workflow ci.yml is not active (state=disabled_manually)"]


def test_cli_reports_ok_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(audit, "run_gh", _fake_gh(_protection(), _workflows()))

    assert audit.main(["--repo", "owner/repo", "--json"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("AUDIT_RESULT=ok")


def test_cli_reports_drift_and_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    protection = _protection(sorted(_EXPECTED_CHECKS - {"e2e (Playwright happy path)"}))
    monkeypatch.setattr(audit, "run_gh", _fake_gh(protection, _workflows()))

    assert audit.main(["--repo", "owner/repo", "--json"]) == 1
    assert capsys.readouterr().out.rstrip().endswith("AUDIT_RESULT=drift")


def test_cli_reports_declaration_mismatch_as_drift_without_calling_github(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    trunk_file = tmp_path / "trunk.yaml"
    trunk_file.write_text(_TRUNK_BAD_FIXTURE)
    monkeypatch.setattr(audit, "TRUNK_FILE", trunk_file)

    def unexpected_gh(_args: list[str]) -> NoReturn:
        raise AssertionError("declaration drift must not depend on the GitHub API")

    monkeypatch.setattr(audit, "run_gh", unexpected_gh)

    assert audit.main(["--repo", "owner/repo", "--json"]) == 1
    assert capsys.readouterr().out.rstrip().endswith("AUDIT_RESULT=drift")


def test_cli_reports_verification_errors_and_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:

    def missing_gh(_args: list[str]) -> NoReturn:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(audit, "run_gh", missing_gh)

    assert audit.main(["--repo", "owner/repo", "--json"]) == 2
    assert capsys.readouterr().out.rstrip().endswith("AUDIT_RESULT=error")
