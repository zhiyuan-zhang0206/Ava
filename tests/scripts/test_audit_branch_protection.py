from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import NoReturn

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "audit_branch_protection.py"
_EXPECTED_CHECKS = frozenset(
    {
        "backend (pytest + pyright)",
        "frontend (eslint + tsc + vitest)",
        "e2e (Playwright happy path)",
        "secret scan (Gitleaks)",
    }
)

_MERGIFY_FIXTURE = """
queue_rules:
  - name: default
    queue_conditions:
      - or:
          - check-success=backend (pytest + pyright)
          - check-skipped=backend (pytest + pyright)
      - or:
          - check-success=frontend (eslint + tsc + vitest)
          - check-skipped=frontend (eslint + tsc + vitest)
      - or:
          - check-success=e2e (Playwright happy path)
          - check-skipped=e2e (Playwright happy path)
      - or:
          - check-success=secret scan (Gitleaks)
          - check-skipped=secret scan (Gitleaks)
    merge_conditions:
      - label=qa-approved
      - or:
          - check-success=backend (pytest + pyright)
          - check-skipped=backend (pytest + pyright)
      - or:
          - check-success=frontend (eslint + tsc + vitest)
          - check-skipped=frontend (eslint + tsc + vitest)
      - or:
          - check-success=e2e (Playwright happy path)
          - check-skipped=e2e (Playwright happy path)
      - or:
          - check-success=secret scan (Gitleaks)
          - check-skipped=secret scan (Gitleaks)
"""


def _audit() -> ModuleType:
    if not _SCRIPT.exists():
        pytest.fail("scripts/audit_branch_protection.py is not implemented")
    spec = importlib.util.spec_from_file_location("audit_branch_protection", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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


def test_expected_checks_collects_success_and_skipped_conditions() -> None:
    audit = _audit()
    assert audit.expected_checks(_MERGIFY_FIXTURE) == _EXPECTED_CHECKS


def test_queue_and_merge_condition_mismatch_is_drift() -> None:
    audit = _audit()
    mismatched = _MERGIFY_FIXTURE.replace(
        "    merge_conditions:\n",
        "    merge_conditions:\n      - check-success=legacy\n",
    )

    assert audit.expected_checks(mismatched) == _EXPECTED_CHECKS | {"legacy"}
    assert audit.declaration_drift(mismatched) == [
        "merge_conditions has checks absent from queue_conditions: legacy"
    ]


def test_real_mergify_declaration_has_the_required_checks() -> None:
    audit = _audit()
    text = (_REPO_ROOT / ".mergify.yml").read_text()
    assert audit.expected_checks(text) == _EXPECTED_CHECKS
    assert audit.declaration_drift(text) == []


def test_drift_report_accepts_the_exact_live_contract() -> None:
    audit = _audit()
    assert audit.drift_report(_protection(), _EXPECTED_CHECKS, _workflows()) == []


def test_drift_report_names_missing_contexts() -> None:
    audit = _audit()
    contexts = sorted(_EXPECTED_CHECKS - {"backend (pytest + pyright)"})
    assert audit.drift_report(_protection(contexts), _EXPECTED_CHECKS, _workflows()) == [
        "missing required status checks: backend (pytest + pyright)"
    ]


def test_drift_report_names_extra_contexts() -> None:
    audit = _audit()
    contexts = [*_EXPECTED_CHECKS, "legacy"]
    assert audit.drift_report(_protection(contexts), _EXPECTED_CHECKS, _workflows()) == [
        "extra required status checks: legacy"
    ]


def test_drift_report_rejects_strict_status_checks() -> None:
    audit = _audit()
    assert audit.drift_report(_protection(strict=True), _EXPECTED_CHECKS, _workflows()) == [
        "required status checks must have strict=false"
    ]


def test_drift_report_requires_admin_enforcement() -> None:
    audit = _audit()
    assert audit.drift_report(
        _protection(enforce_admins=False), _EXPECTED_CHECKS, _workflows()
    ) == ["branch protection must enforce rules for administrators"]


def test_drift_report_detects_missing_required_status_checks() -> None:
    audit = _audit()
    protection = {"enforce_admins": {"enabled": True}}
    assert audit.drift_report(protection, _EXPECTED_CHECKS, _workflows()) == [
        "branch protection has no required_status_checks"
    ]


def test_drift_report_detects_a_missing_workflow() -> None:
    audit = _audit()
    workflows = {"workflows": [{"path": ".github/workflows/ci.yml", "state": "active"}]}
    assert audit.drift_report(_protection(), _EXPECTED_CHECKS, workflows) == [
        "workflow ci-rerun.yml is missing on GitHub"
    ]


def test_drift_report_detects_an_inactive_workflow() -> None:
    audit = _audit()
    assert audit.drift_report(
        _protection(), _EXPECTED_CHECKS, _workflows(ci_state="disabled_manually")
    ) == ["workflow ci.yml is not active (state=disabled_manually)"]


def test_cli_reports_ok_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    audit = _audit()
    monkeypatch.setattr(audit, "run_gh", _fake_gh(_protection(), _workflows()))

    assert audit.main(["--repo", "owner/repo", "--json"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("AUDIT_RESULT=ok")


def test_cli_reports_drift_and_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    audit = _audit()
    protection = _protection(sorted(_EXPECTED_CHECKS - {"e2e (Playwright happy path)"}))
    monkeypatch.setattr(audit, "run_gh", _fake_gh(protection, _workflows()))

    assert audit.main(["--repo", "owner/repo", "--json"]) == 1
    assert capsys.readouterr().out.rstrip().endswith("AUDIT_RESULT=drift")


def test_cli_reports_declaration_mismatch_as_drift_without_calling_github(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    audit = _audit()
    mismatched = _MERGIFY_FIXTURE.replace(
        "    merge_conditions:\n",
        "    merge_conditions:\n      - check-success=legacy\n",
    )
    mergify_file = tmp_path / ".mergify.yml"
    mergify_file.write_text(mismatched)
    monkeypatch.setattr(audit, "_MERGIFY_FILE", mergify_file)

    def unexpected_gh(_args: list[str]) -> NoReturn:
        raise AssertionError("declaration drift must not depend on the GitHub API")

    monkeypatch.setattr(audit, "run_gh", unexpected_gh)

    assert audit.main(["--repo", "owner/repo", "--json"]) == 1
    assert capsys.readouterr().out.rstrip().endswith("AUDIT_RESULT=drift")


def test_cli_reports_verification_errors_and_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    audit = _audit()

    def missing_gh(_args: list[str]) -> NoReturn:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(audit, "run_gh", missing_gh)

    assert audit.main(["--repo", "owner/repo", "--json"]) == 2
    assert capsys.readouterr().out.rstrip().endswith("AUDIT_RESULT=error")
