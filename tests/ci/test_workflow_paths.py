"""Guard each pull_request/push event, including tag pushes, against unfiltered runs.

Scan .github/workflows/*.yml and *.yaml. Each of those two events needs a
nonempty paths/paths-ignore list or an explicit (workflow, event) exemption.
Branch, tag and activity-type filters do not exempt an event. Other triggers
(workflow_run, schedule, issues, pull_request_target, etc.) are outside this
predicate, which is deliberately limited to the two named events.
"""

import shutil
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"
EXEMPTIONS = {
    ("ci.yml", "pull_request"): "Required checks report on every PR; jobs classify paths.",
    ("qa-approved-gate.yml", "pull_request"): "Required QA gate follows every PR head and label.",
    ("release-app.yml", "push"): "Version-tag releases; GitHub ignores paths for tags.",
    ("release.yml", "push"): "Version-tag releases; GitHub ignores paths for tags.",
}


def unfiltered_events(workflow: Path) -> set[str]:
    # YAML 1.1 reads bare `on` as True; quoted `on` remains a string.
    document = yaml.safe_load(workflow.read_text())
    triggers = document["on" if "on" in document else True]
    if isinstance(triggers, str):
        triggers = {triggers: {}}
    elif isinstance(triggers, list):
        triggers = dict.fromkeys(triggers)
    assert isinstance(triggers, dict), f"Invalid on declaration in {workflow.name}"
    missing = set()
    for event in {"pull_request", "push"}.intersection(triggers):
        options = triggers[event]
        if not isinstance(options, dict) or not any(
            key in options and isinstance(options[key], list) and options[key]
            for key in ("paths", "paths-ignore")
        ):
            missing.add(event)
    return missing


def assert_workflow_paths(directory: Path) -> None:
    workflows = sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")])
    assert workflows, f"No workflows found in {directory}"
    unfiltered = {(path.name, event) for path in workflows for event in unfiltered_events(path)}
    unexpected = unfiltered - EXEMPTIONS.keys()
    assert not unexpected, (
        f"Unfiltered push/PR events: {sorted(unexpected)}. "
        "Add paths/paths-ignore or a justified event-specific EXEMPTIONS entry."
    )
    stale = EXEMPTIONS.keys() - unfiltered
    assert not stale, f"Remove stale workflow exemptions: {sorted(stale)}"
    assert all(reason.strip() for reason in EXEMPTIONS.values())


def test_repository_workflows_have_paths_or_justified_exemptions() -> None:
    assert_workflow_paths(WORKFLOWS)


@pytest.mark.parametrize(
    "trigger",
    [
        "pull_request",
        "push",
        "[pull_request, workflow_dispatch]",
        "\n  pull_request:",
        "\n  push:\n    branches: [main]",
        "\n  push:\n    tags: ['v*']",
        "\n  pull_request:\n    paths: []",
    ],
)
def test_unfiltered_event_forms_are_detected(tmp_path: Path, trigger: str) -> None:
    workflow = tmp_path / "example.yml"
    workflow.write_text(f"on: {trigger}\n")
    assert unfiltered_events(workflow)


@pytest.mark.parametrize(
    "trigger",
    [
        "\n  pull_request:\n    paths: ['src/**']",
        "\n  push:\n    paths-ignore: ['docs/**']",
        "workflow_run",
        "\n  schedule:\n    - cron: '0 0 * * *'",
        "[issues, workflow_dispatch]",
        "\n  pull_request_target:\n    types: [labeled]",
    ],
)
def test_filtered_and_out_of_scope_events_are_accepted(tmp_path: Path, trigger: str) -> None:
    workflow = tmp_path / "example.yml"
    workflow.write_text(f"on: {trigger}\n")
    assert not unfiltered_events(workflow)


def test_one_event_filter_does_not_cover_another(tmp_path: Path) -> None:
    workflow = tmp_path / "example.yml"
    workflow.write_text("on:\n  push:\n    paths: ['src/**']\n  pull_request:\n")
    assert unfiltered_events(workflow) == {"pull_request"}


def test_quoted_on_key_is_detected(tmp_path: Path) -> None:
    workflow = tmp_path / "example.yml"
    workflow.write_text("'on': push\n")
    assert unfiltered_events(workflow) == {"push"}


@pytest.mark.parametrize("event, extension", [("pull_request", "yml"), ("push", "yaml")])
def test_new_unfiltered_workflow_fails_in_copy(tmp_path: Path, event: str, extension: str) -> None:
    workflows = tmp_path / ".github/workflows"
    shutil.copytree(WORKFLOWS, workflows)
    assert_workflow_paths(workflows)
    (workflows / f"unregistered.{extension}").write_text(
        f"on: [{event}]\njobs:\n  proof:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: echo proof\n"
    )
    with pytest.raises(AssertionError, match=rf"unregistered\.{extension}.*{event}"):
        assert_workflow_paths(workflows)


def test_exemptions_are_event_specific(tmp_path: Path) -> None:
    workflows = tmp_path / ".github/workflows"
    shutil.copytree(WORKFLOWS, workflows)
    (workflows / "ci.yml").write_text("on: [pull_request, push]\n")
    with pytest.raises(AssertionError, match=r"ci.yml.*push"):
        assert_workflow_paths(workflows)


def test_removed_exemption_is_reported(tmp_path: Path) -> None:
    workflows = tmp_path / ".github/workflows"
    shutil.copytree(WORKFLOWS, workflows)
    (workflows / "release.yml").unlink()
    with pytest.raises(AssertionError, match=r"stale workflow exemptions.*release.yml"):
        assert_workflow_paths(workflows)
