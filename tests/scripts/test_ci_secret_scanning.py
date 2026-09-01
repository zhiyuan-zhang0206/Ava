"""Contract test for the CI secret and dependency audit policy."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_PRE_COMMIT = _REPO_ROOT / ".pre-commit-config.yaml"
_POLICY = _REPO_ROOT / ".gitleaks.toml"


def _load_yaml(path: Path) -> dict[object, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return cast("dict[object, Any]", document)


def test_ci_secret_and_dependency_audits_are_wired_to_one_policy() -> None:
    workflow = _load_yaml(_WORKFLOW)
    secret_scan = workflow["jobs"]["secret-scan"]
    assert secret_scan.get("continue-on-error") is not True
    secret_steps = secret_scan["steps"]
    assert secret_steps[0]["with"]["fetch-depth"] == 0
    assert "github.com/zricethezav/gitleaks/v8@v8.30.1" in secret_steps[1]["run"]
    assert "gitleaks detect --source . --config .gitleaks.toml --redact" in secret_steps[2]["run"]

    dependency_audit = workflow["jobs"]["dependency-audit"]
    assert dependency_audit.get("continue-on-error") is not True
    audit_steps = dependency_audit["steps"]
    assert audit_steps[2]["run"] == "uvx --from 'uv==0.11.16' uv audit --frozen"
    assert audit_steps[2]["continue-on-error"] is True
    assert audit_steps[4]["working-directory"] == "ui/web"
    assert audit_steps[4]["run"] == "npm audit"
    assert audit_steps[4]["continue-on-error"] is True

    pre_commit = _load_yaml(_PRE_COMMIT)
    hooks = [hook for repo in pre_commit["repos"] for hook in repo["hooks"]]
    assert "detect-private-key" not in {hook["id"] for hook in hooks}
    gitleaks_hook = next(hook for hook in hooks if hook["id"] == "gitleaks")
    assert gitleaks_hook["args"] == ["--config=.gitleaks.toml"]

    with _POLICY.open("rb") as policy_file:
        policy = tomllib.load(policy_file)
    assert policy["extend"]["useDefault"] is True
    allowlisted_regexes = "\n".join(policy["allowlist"]["regexes"])
    for fixture in ("ava_dev_only", "pw123", "s3cr3t", "new-db", "new-runner-db"):
        assert fixture in allowlisted_regexes
    assert set(policy["allowlist"]["commits"]) == {
        "e0270b05d28524401b6caf5b0a300ab91a4d1c6a",
        "14e008cd0f9ab4b791330f16d098d8155726d392",
        "40a3e60c3cea5994c402634bde54a5898debe62e",
        "71ebd7d4e70b4f0d7506d58282c79a36fc098d0c",
        "cb9e783af600b2b5ac53b776f68b33514885e896",
        "c0bb257d84d3a7d6212be23339cbf6ca921ac5e3",
        "c15f00344d4805b82ed2e0cda824a7527cc0b943",
    }

    assert not (_REPO_ROOT / ".gitguardian.yml").exists()
