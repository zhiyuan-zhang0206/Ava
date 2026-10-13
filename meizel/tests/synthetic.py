"""Build small throwaway repositories and read the checker's verdict on them."""

from __future__ import annotations

import subprocess
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from textwrap import dedent
from typing import cast

from meizel.contracts.analysis import Analysis, analyze
from meizel.contracts.cli import main
from meizel.contracts.policy import POLICY, render_policy
from meizel.graph.model import Diagnostic

type RepoFactory = Callable[[Mapping[str, str]], Path]


def listed_allowances(files: Mapping[str, str]) -> str:
    """A policy listing every `[loads]` and `[external]` site the given manifests declare."""
    external: dict[str, str] = {}
    loads: dict[str, tuple[str, ...]] = {}
    for rel, text in files.items():
        if rel.rsplit("/", 1)[-1] != "meizel.toml":
            continue
        directory = rel.rpartition("/")[0]
        try:
            document = tomllib.loads(dedent(text).lstrip("\n"))
        except tomllib.TOMLDecodeError:
            continue  # a malformed manifest is the test's subject, not an allowance
        external_table: object = document.get("external", {})
        loads_table: object = document.get("loads", {})
        if not isinstance(external_table, dict) or not isinstance(loads_table, dict):
            continue
        for name, reason in cast("dict[str, object]", external_table).items():
            external[f"{directory}/{name}" if directory else name] = str(reason)
        for name, targets in cast("dict[str, list[str]]", loads_table).items():
            loads[f"{directory}/{name}" if directory else name] = tuple(targets)
    return render_policy(external, loads)


def build_repo(base: Path, files: Mapping[str, str]) -> Path:
    """A throwaway repository; unless it brings its own policy, one lists its allowances."""
    root = base / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main", "-q", str(root)], check=True)  # noqa: S603 - fixed argv
    given = dict(files)
    if POLICY not in given:
        given[POLICY] = listed_allowances(files)
    for rel, text in given.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dedent(text).lstrip("\n"), encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)  # noqa: S603 - fixed argv
    return root


def analysis_of(root: Path) -> Analysis:
    return analyze(root, jobs=1)


def verdict(root: Path) -> set[tuple[str, str]]:
    """(code, path) of every diagnostic."""
    return {(d.code.value, d.path) for d in analysis_of(root).diagnostics}


def diagnostics(root: Path, code: str) -> list[Diagnostic]:
    return [d for d in analysis_of(root).diagnostics if d.code.value == code]


def edges(root: Path) -> set[tuple[str, str, str]]:
    """(src, dst, kind) of every fact edge."""
    return {(f.src, f.dst, f.kind.value) for f in analysis_of(root).facts}


def failures(root: Path) -> set[tuple[str, str]]:
    """(code, path) of every diagnostic that fails `check`, recorded debt taken into account."""
    return {(d.code.value, d.path) for d in analysis_of(root).verdict}


def run(root: Path, *args: str) -> int:
    """One `python -m meizel` invocation against `root`."""
    return main(["--repo", str(root), "--jobs", "1", *args])


def write(root: Path, files: Mapping[str, str]) -> None:
    """Add or replace files and stage them, so the universe sees them."""
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dedent(text).lstrip("\n"), encoding="utf-8")
    git(root, "add", "-A")


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)  # noqa: S603 - fixed argv


def commit(root: Path, message: str = "snapshot") -> None:
    git(
        root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        message,
    )
