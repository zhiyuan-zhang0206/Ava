"""The node universe: tracked files and their directories, from `git ls-files`."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from meizel.graph.model import MeizelError

__all__ = [
    "MANIFEST",
    "Module",
    "Universe",
    "is_private_module",
    "load_universe",
    "parent_of",
    "toplevel",
]

MANIFEST = "meizel.toml"


@dataclass(frozen=True, slots=True)
class Module:
    """A first-party module: its source file, or None for a namespace directory."""

    file: str | None
    path: str


_DUNDER_MODULE = re.compile(r"__\w+__(\.py)?")


def is_private_module(child: str) -> bool:
    """A module or package whose name starts with an underscore is private to its directory.

    A dunder name (`__init__.py`, `__main__.py`) is the language's own protocol, not a private one.
    """
    return child.startswith("_") and _DUNDER_MODULE.fullmatch(child) is None


def parent_of(path: str) -> str:
    return path.rpartition("/")[0]


def toplevel(cwd: Path) -> Path:
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise MeizelError(f"not inside a git checkout: {result.stderr.strip()}")
    return Path(result.stdout.strip())


class Universe:
    """Tracked regular files, every directory above them, and module resolution."""

    def __init__(self, root: Path, tracked: list[str]) -> None:
        self.root = root
        self.files: frozenset[str] = frozenset(tracked)
        children: dict[str, set[str]] = {}
        for path in tracked:
            self._add_chain(children, path)
        self.dirs: frozenset[str] = frozenset(children)
        self.children: dict[str, tuple[str, ...]] = {
            directory: tuple(sorted(names)) for directory, names in children.items()
        }
        self.manifest_dirs: frozenset[str] = frozenset(
            parent_of(p) for p in tracked if p.rsplit("/", 1)[-1] == MANIFEST
        )
        self.python_files: tuple[str, ...] = tuple(sorted(p for p in tracked if p.endswith(".py")))
        self.roots: frozenset[str] = frozenset(
            name.removesuffix(".py") for name in self.children.get("", ())
        )
        self._modules: dict[str, Module | None] = {}

    @staticmethod
    def _add_chain(children: dict[str, set[str]], path: str) -> None:
        children.setdefault("", set())
        parts = path.split("/")
        for depth in range(len(parts)):
            directory = "/".join(parts[:depth])
            name = parts[depth]
            if depth == len(parts) - 1 and name == MANIFEST:
                continue
            children.setdefault(directory, set()).add(name)
            if depth < len(parts) - 1:
                children.setdefault("/".join(parts[: depth + 1]), set())

    def resolve_module(self, dotted: str) -> Module | None:
        """Resolve a dotted name against the checkout, as `pythonpath = ["."]` would."""
        if dotted in self._modules:
            return self._modules[dotted]
        path = dotted.replace(".", "/")
        found: Module | None = None
        if f"{path}.py" in self.files:
            found = Module(f"{path}.py", f"{path}.py")
        elif f"{path}/__init__.py" in self.files:
            found = Module(f"{path}/__init__.py", path)
        elif path in self.dirs and path:
            found = Module(None, path)
        self._modules[dotted] = found
        return found

    def is_first_party_name(self, dotted: str) -> bool:
        return dotted.split(".", 1)[0] in self.roots


def _is_regular(root: Path, rel: str) -> bool:
    full = root / rel
    return full.is_file() and not full.is_symlink()


def load_universe(root: Path) -> Universe:
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise MeizelError(f"git ls-files failed: {result.stderr.decode(errors='replace').strip()}")
    listed = [p.decode() for p in result.stdout.split(b"\0") if p]
    return Universe(root, sorted({p for p in listed if _is_regular(root, p)}))
