"""Guard each pull_request/push event, including tag pushes, against unfiltered runs.

Scan .github/workflows/*.yml and *.yaml. Each of those two events needs a
nonempty paths/paths-ignore list or an explicit (workflow, event) exemption.
Branch, tag and activity-type filters do not exempt an event. Other triggers
(workflow_run, schedule, issues, pull_request_target, etc.) are outside this
predicate, which is deliberately limited to the two named events.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"
EXEMPTIONS = {
    ("ci.yml", "pull_request"): "Required checks report on every PR; jobs classify paths.",
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


# ── a filter entry must name something that exists ──────────────────────────
#
# GitHub does not validate `paths:` entries: a filter naming a moved or renamed
# file never fails, the proof workflow just stops triggering. Test files move into
# their packages' `tests/` directories, so every positive entry must still match a
# tracked file.


def glob_regex(pattern: str) -> re.Pattern[str]:
    """GitHub's filter glob as a regex: `*` stays inside a path segment, `**` crosses them."""
    out = ""
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**/", index):
            out += "(?:.*/)?"
            index += 3
        elif pattern.startswith("**", index):
            out += ".*"
            index += 2
        elif char == "*":
            out += "[^/]*"
            index += 1
        elif char == "?":
            out += "[^/]"
            index += 1
        else:
            out += re.escape(char)
            index += 1
    return re.compile(out + "$")


def filter_entries(workflow: Path) -> list[str]:
    """The positive `paths:` entries of a workflow's push and pull_request events."""
    document = yaml.safe_load(workflow.read_text())
    triggers = document["on" if "on" in document else True]
    if not isinstance(triggers, dict):
        return []
    entries: list[str] = []
    for event in ("push", "pull_request"):
        options = triggers.get(event)
        if isinstance(options, dict):
            entries.extend(entry for entry in options.get("paths", []) if not entry.startswith("!"))
    return entries


def tracked_files() -> list[str]:
    result = subprocess.run(  # noqa: S603 - fixed git query in this repository
        ["git", "-C", str(WORKFLOWS.parents[1]), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [path for path in result.stdout.split("\0") if path]


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("tests/ui/**", "tests/ui/a/b.py", True),
        ("tests/ui/**", "tests/uix/b.py", False),
        ("tests/agent/test_resources*.py", "tests/agent/test_resources_x.py", True),
        ("tests/agent/test_resources*.py", "tests/agent/sub/test_resources.py", False),
        ("**/tests/**", "base/packages/tests/test_x.py", True),
        ("tests/_*.py", "tests/_containers.py", True),
        ("tests/_*.py", "tests/x/_containers.py", False),
        ("base/paths/__init__.py", "base/paths/__init__.py", True),
    ],
)
def test_glob_regex_follows_the_filter_syntax(pattern: str, path: str, matches: bool) -> None:
    assert (glob_regex(pattern).match(path) is not None) is matches


def test_every_paths_filter_entry_matches_a_tracked_file() -> None:
    tracked = tracked_files()
    dead = {
        f"{workflow.name}: {entry}"
        for workflow in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
        for entry in filter_entries(workflow)
        if not any(glob_regex(entry).match(path) for path in tracked)
    }
    assert not dead, (
        "workflow `paths:` entries that match no tracked file (the workflow would stop "
        f"triggering on them; a moved test needs its entry updated): {sorted(dead)}"
    )


def test_a_dead_filter_entry_is_detected(tmp_path: Path) -> None:
    workflow = tmp_path / "proof.yml"
    workflow.write_text(
        "on:\n  push:\n    paths:\n      - 'tests/base/test_gone.py'\n      - '!docs/**'\n"
    )
    assert filter_entries(workflow) == ["tests/base/test_gone.py"]
    assert not any(glob_regex("tests/base/test_gone.py").match(path) for path in tracked_files())
