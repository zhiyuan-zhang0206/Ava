"""Build small throwaway repositories and read the checker's verdict on them."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from textwrap import dedent

from scripts.meizel.analysis import Analysis, analyze
from scripts.meizel.model import Diagnostic

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
