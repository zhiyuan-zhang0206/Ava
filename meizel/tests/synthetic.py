"""Build small throwaway repositories and read the checker's verdict on them."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from textwrap import dedent

from meizel.contracts.analysis import Analysis, analyze
from meizel.contracts.cli import main
from meizel.graph.model import Diagnostic

type RepoFactory = Callable[[Mapping[str, str]], Path]


def build_repo(base: Path, files: Mapping[str, str]) -> Path:
    root = base / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main", "-q", str(root)], check=True)  # noqa: S603 - fixed argv
    for rel, text in files.items():
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
