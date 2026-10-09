"""Opt-in collection and runtime proof for whole-file shards.

Create a plan from one complete eligible collection, then collect each group's
files and compare node IDs and fixture closure with that same snapshot. Both
stages require --collect-only unless --file-shard-execute explicitly opts into
runtime proof. Required CI routing remains unchanged.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from collections.abc import Generator
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Literal, Self, cast

import pytest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator
from pytest_split.algorithms import LeastDurationAlgorithm

Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
type Phase = Literal["setup", "call", "teardown"]
DEFAULT_FILE_SHARDS = 16
MAX_FILE_SHARDS = 64
_DURATIONS = TypeAdapter(dict[str, Seconds])
_STARTED = time.perf_counter()


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    nodeid: str
    file: str
    fixtures: list[str]

    @model_validator(mode="after")
    def valid_path(self) -> Self:
        path = Path(self.file)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != self.file:
            raise ValueError("Test files must be normalized repository-relative paths")
        if path.suffix != ".py" or not self.nodeid.startswith(self.file + "::"):
            raise ValueError("Node IDs must belong to their declared Python file")
        return self


class Group(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    nodes: list[Node] = Field(min_length=1)
    estimated_seconds: Seconds

    @property
    def files(self) -> list[str]:
        return sorted({node.file for node in self.nodes})


class BaselineGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    node_count: int = Field(ge=0)
    file_count: int = Field(ge=0)
    estimated_seconds: Seconds


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    pytest_version: str
    pytest_split_version: str
    durations_sha256: str
    pytest_config_sha256: str
    unknown_nodes: int = Field(ge=0)
    collection_seconds: Seconds
    baseline: list[BaselineGroup]
    groups: list[Group] = Field(min_length=1, max_length=MAX_FILE_SHARDS)

    @model_validator(mode="after")
    def exclusive_ownership(self) -> Self:
        nodes: set[str] = set()
        files: set[str] = set()
        for group in self.groups:
            if files.intersection(group.files):
                raise ValueError("A file cannot belong to multiple groups")
            files.update(group.files)
            for node in group.nodes:
                if node.nodeid in nodes:
                    raise ValueError("A node cannot occur more than once")
                nodes.add(node.nodeid)
        if len(self.baseline) != len(self.groups):
            raise ValueError("Baseline and candidate must have the same group count")
        if sum(group.node_count for group in self.baseline) != len(nodes):
            raise ValueError("Baseline and candidate must have the same node population")
        return self


_PLAN = pytest.StashKey[Plan]()
_RUNTIME = pytest.StashKey[dict[str, "RuntimeNode"]]()
_COLLECTION_SECONDS = pytest.StashKey[float]()
_DURATION_DIGEST = pytest.StashKey[str]()


class RuntimeNode(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    fixtures: dict[str, str] = Field(default_factory=dict)
    outcomes: dict[Phase, Literal["passed", "failed", "skipped"]] = Field(
        default_factory=dict[Phase, Literal["passed", "failed", "skipped"]]
    )
    seconds: dict[Phase, Seconds] = Field(default_factory=dict[Phase, Seconds])


class RuntimeReport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    group: int = Field(ge=1)
    worker: str
    exitstatus: int
    pytest_version: str
    pytest_split_version: str
    durations_sha256: str
    pytest_config_sha256: str
    collection_seconds: Seconds
    nodes: dict[str, RuntimeNode]


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("Whole-file collection shadow")
    group.addoption("--file-shard-plan", help="Write a plan from a complete eligible collection.")
    group.addoption("--file-shard-count", type=int, default=DEFAULT_FILE_SHARDS)
    group.addoption("--file-shard-check", help="Read a plan and collect one group's files.")
    group.addoption("--file-shard-group", type=int)
    group.addoption("--file-shard-report", help="Write the collection comparison JSON.")
    group.addoption(
        "--file-shard-execute",
        action="store_true",
        help="Execute a checked group for runtime proof.",
    )
    group.addoption(
        "--file-shard-runtime-report", help="Write per-worker runtime evidence at this prefix."
    )


def _worker(config: pytest.Config) -> str:
    worker = getattr(config, "workerinput", None)
    return cast(str, worker["workerid"]) if worker is not None else "controller"


def _report_path(config: pytest.Config, option: str) -> Path:
    path = Path(cast(str, config.getoption(option)))
    return path.with_name(f"{path.stem}-{_worker(config)}{path.suffix}")


def _configure_runtime(config: pytest.Config) -> None:
    if config.getoption("file_shard_execute") and (
        not config.getoption("file_shard_check")
        or config.option.collectonly
        or not config.getoption("file_shard_runtime_report")
    ):
        raise pytest.UsageError(
            "Execution proof requires a checked group, runtime report and test bodies"
        )
    if not config.getoption("file_shard_runtime_report"):
        return
    if config.option.collectonly:
        raise pytest.UsageError("Runtime evidence requires test execution")
    group = config.getoption("file_shard_group") or config.getoption("group")
    if not isinstance(group, int) or group < 1:
        raise pytest.UsageError("Runtime evidence requires a shard group")
    _report_path(config, "file_shard_runtime_report").unlink(missing_ok=True)
    config.stash[_RUNTIME] = {}
    # pytest-split writes new measurements at session finish; evidence identifies
    # the input that selected this generation, before that output replaces it.
    config.stash[_DURATION_DIGEST] = _durations(config)[1]


def _durations(config: pytest.Config) -> tuple[dict[str, float], str]:
    try:
        raw = Path(cast(str, config.getoption("durations_path"))).read_bytes()
        return _DURATIONS.validate_json(raw, strict=True), hashlib.sha256(raw).hexdigest()
    except (OSError, ValidationError) as error:
        raise pytest.UsageError(f"Invalid shadow duration input: {error}") from error


def _configuration_digest(config: pytest.Config) -> str:
    if config.inipath is None:
        raise pytest.UsageError("Shadow requires a pytest configuration file; pin it with -c")
    context = {
        "overrides": config.getoption("override_ini", default=[]),
        "markers": config.option.markexpr,
        "keywords": config.option.keyword,
        "omit_static": config.getoption("omit_static_tests", default=False),
    }
    raw = config.inipath.read_bytes() + json.dumps(context, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def _load_group(config: pytest.Config) -> None:
    source = Path(cast(str, config.getoption("file_shard_check")))
    output = config.getoption("file_shard_report")
    if not output:
        raise pytest.UsageError("--file-shard-check requires --file-shard-report")
    report = (
        _report_path(config, "file_shard_report")
        if config.getoption("file_shard_execute")
        else Path(output)
    )
    if source.resolve() == Path(output).resolve():
        raise pytest.UsageError("Shadow plan and report must use different paths")
    report.unlink(missing_ok=True)
    try:
        raw = source.read_bytes()
        plan = Plan.model_validate_json(raw)
    except (OSError, ValidationError) as error:
        raise pytest.UsageError(f"Invalid shadow plan: {error}") from error
    index = config.getoption("file_shard_group")
    if not isinstance(index, int) or not 1 <= index <= len(plan.groups):
        raise pytest.UsageError("--file-shard-group must name a group in the plan")
    if (plan.pytest_version, plan.pytest_split_version) != (
        pytest.__version__,
        version("pytest-split"),
    ):
        raise pytest.UsageError(
            "Shadow plan and check require the same pytest/pytest-split versions"
        )
    if plan.durations_sha256 != _durations(config)[1]:
        raise pytest.UsageError("Shadow plan and check require the same duration input")
    if plan.pytest_config_sha256 != _configuration_digest(config):
        raise pytest.UsageError(
            "Shadow plan and check require the same pytest configuration and filters"
        )
    files = plan.groups[index - 1].files
    for file in files:
        path = (config.rootpath / file).resolve()
        if not path.is_relative_to(config.rootpath.resolve()) or not path.is_file():
            raise pytest.UsageError(f"Planned file is missing or outside the checkout: {file}")
    config.args[:] = [str(config.rootpath / file) for file in files]
    config.stash[_PLAN] = plan


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    _configure_runtime(config)
    output = config.getoption("file_shard_plan")
    check = config.getoption("file_shard_check")
    execute = config.getoption("file_shard_execute")
    if not output and not check:
        if config.getoption("file_shard_group") or config.getoption("file_shard_report"):
            raise pytest.UsageError("Shadow group/report options require --file-shard-check")
        return
    if output and check:
        raise pytest.UsageError("Choose plan creation or group checking, not both")
    if not config.option.collectonly and not execute:
        raise pytest.UsageError(
            "File shard shadow requires --collect-only; it cannot run a test gate"
        )
    if config.getoption("splits") is not None:
        raise pytest.UsageError("Shadow needs the unsplit collection; do not supply --splits")
    count = cast(int, config.getoption("file_shard_count"))
    if not 1 <= count <= MAX_FILE_SHARDS:
        raise pytest.UsageError(f"--file-shard-count must be between 1 and {MAX_FILE_SHARDS}")
    if check:
        _load_group(config)
    else:
        Path(cast(str, output)).unlink(missing_ok=True)


def _record(item: pytest.Item, root: Path) -> Node:
    if not isinstance(item, pytest.Function):
        raise pytest.UsageError(f"Unsupported shadow collector: {type(item).__name__}")
    return Node(
        nodeid=item.nodeid,
        file=item.path.relative_to(root).as_posix(),
        fixtures=sorted(item.fixturenames),
    )


def _plan(session: pytest.Session) -> Plan:
    durations, digest = _durations(session.config)
    items = session.items
    by_file: dict[Path, list[pytest.Item]] = defaultdict(list)
    for item in items:
        by_file[item.path].append(item)
    count = cast(int, session.config.getoption("file_shard_count"))
    if len(by_file) < count:
        raise pytest.UsageError("Shadow requires at least one eligible file per group")
    known = [durations[item.nodeid] for item in items if item.nodeid in durations]
    # Match pytest-split's relevant-node mean before aggregating whole files.
    average = sum(known) / len(known) if known else 1.0
    weights = {
        nodes[0].nodeid: sum(durations.get(item.nodeid, average) for item in nodes)
        for nodes in by_file.values()
    }
    algorithm = LeastDurationAlgorithm()
    baseline = algorithm(count, items, durations)
    candidate = algorithm(count, [nodes[0] for nodes in by_file.values()], weights)
    return Plan(
        pytest_version=pytest.__version__,
        pytest_split_version=version("pytest-split"),
        durations_sha256=digest,
        pytest_config_sha256=_configuration_digest(session.config),
        unknown_nodes=len(items) - len(known),
        collection_seconds=time.perf_counter() - _STARTED,
        baseline=[
            BaselineGroup(
                node_count=len(group.selected),
                file_count=len({item.path for item in group.selected}),
                estimated_seconds=group.duration,
            )
            for group in baseline
        ],
        groups=[
            Group(
                nodes=[
                    _record(item, session.config.rootpath)
                    for file in group.selected
                    for item in by_file[file.path]
                ],
                estimated_seconds=group.duration,
            )
            for group in candidate
        ],
    )


def _check(session: pytest.Session) -> None:
    config = session.config
    index = cast(int, config.getoption("file_shard_group"))
    expected = {node.nodeid: node for node in config.stash[_PLAN].groups[index - 1].nodes}
    records = [_record(item, config.rootpath) for item in session.items]
    actual = {node.nodeid: node for node in records}
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    changed = sorted(
        nodeid for nodeid in actual.keys() & expected.keys() if actual[nodeid] != expected[nodeid]
    )
    matched = not (missing or extra or changed or len(actual) != len(session.items))
    report = {
        "group": index,
        "matched": matched,
        "collection_seconds": time.perf_counter() - _STARTED,
        "node_count": len(session.items),
        "file_count": len({item.path for item in session.items}),
        "missing": missing,
        "extra": extra,
        "fixture_changes": changed,
    }
    report_path = (
        _report_path(config, "file_shard_report")
        if config.getoption("file_shard_execute")
        else Path(cast(str, config.getoption("file_shard_report")))
    )
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    if not matched:
        raise pytest.UsageError(
            "File shard shadow differs from complete collection; see the report"
        )


def pytest_collection_finish(session: pytest.Session) -> None:
    config = session.config
    config.stash[_COLLECTION_SECONDS] = time.perf_counter() - _STARTED
    output = config.getoption("file_shard_plan")
    check = config.getoption("file_shard_check")
    if not output and not check:
        return
    if session.testsfailed:
        raise pytest.UsageError("Collection errors cannot produce a valid shadow result")
    if output:
        Path(output).write_text(_plan(session).model_dump_json(indent=2) + "\n")
    else:
        _check(session)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item,
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if _RUNTIME not in item.config.stash:
        return report
    node = item.config.stash[_RUNTIME].setdefault(item.nodeid, RuntimeNode())
    phase = report.when
    if phase in node.outcomes:
        raise pytest.UsageError(f"Repeated runtime phase for {item.nodeid}: {phase}")
    node.outcomes[phase] = report.outcome
    node.seconds[phase] = report.duration
    if phase == "call" and isinstance(item, pytest.Function):
        # Pytest's resolved definitions include getfixturevalue() bindings, unlike
        # item.fixturenames. The proof pins pytest's version in every report.
        node.fixtures = {
            name: f"{definition.func.__module__}:{definition.func.__qualname__}:{definition.scope}"
            for name, definition in item._request._fixture_defs.items()
        }
    return report


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    config = session.config
    if _RUNTIME not in config.stash:
        return
    report = RuntimeReport(
        group=cast(int, config.getoption("file_shard_group") or config.getoption("group")),
        worker=_worker(config),
        exitstatus=exitstatus,
        pytest_version=pytest.__version__,
        pytest_split_version=version("pytest-split"),
        durations_sha256=config.stash[_DURATION_DIGEST],
        pytest_config_sha256=_configuration_digest(config),
        collection_seconds=config.stash.get(_COLLECTION_SECONDS, 0.0),
        nodes=config.stash[_RUNTIME],
    )
    _report_path(config, "file_shard_runtime_report").write_text(
        report.model_dump_json(indent=2) + "\n"
    )
