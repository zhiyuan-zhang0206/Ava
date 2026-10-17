from pathlib import Path

from base.host.proc import run_bounded
from scripts.content_lint.code_comments.check import Repository

DOC = "pkg/docs/core.md"


def write_files(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def repository(root: Path, files: dict[str, str]) -> Repository:
    write_files(root, files)
    return Repository(root, frozenset(files))


def note(lines: tuple[str, ...] = (), *, links: tuple[str, ...] = (), indent: int = 0) -> str:
    margin = " " * indent
    block = [f"{margin}# meiva:note"]
    block.extend(f"{margin}# {line}" if line else f"{margin}#" for line in lines)
    block.extend(f"{margin}# meiva:link {link}" for link in links)
    return "\n".join(block) + "\n"


def files() -> dict[str, str]:
    return {
        "pkg/core.py": note(("The budget stays positive.",), links=(DOC,))
        + "def budget() -> int:\n    return 1\n",
        DOC: "# Budget\n",
        "pkg/tests/test_core.py": "def test_budget() -> None:\n    assert 1 > 0\n",
    }


def git_repository(root: Path, files: dict[str, str]) -> None:
    write_files(root, files)
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "comment-check@example.invalid"),
        ("config", "user.name", "comment-check"),
        ("add", "."),
        ("commit", "-qm", "base"),
    ):
        result = run_bounded(["git", *args], cwd=root, timeout=20, text=True, capture_output=True)
        result.check_returncode()
