"""The pytest plugins a `path_scopes.toml` mounts: strings the checker would not otherwise read.

A `path_scopes.toml` maps a dotted module name to the directory ("." ) or the test files it governs; the
test suite registers that module as a plugin for those paths when it collects them. The module is
loaded by name from a string, so no `import` records the dependency. Each mount is one fact edge, of the
same kind as a literal `pytest_plugins`, from the TOML file to the module's file; it is judged like an
import, so a directory that mounts a plugin from a layer above it shows up as the back-edge it is.
"""

from __future__ import annotations

import tomllib
from collections.abc import Collection

from meizel.graph.model import Code, Diagnostic, Fact, FactKind, MeizelError
from meizel.graph.universe import Universe

__all__ = ["SCOPE_FILE", "mount_facts"]

SCOPE_FILE = "path_scopes.toml"


def _line_of(lines: list[str], module: str) -> int:
    """The line that names `module` as a key, or 1 when the key is spelled in a way we do not find."""
    for number, line in enumerate(lines, start=1):
        if line.lstrip().startswith((f'"{module}"', f"'{module}'", f"{module} ")):
            return number
    return 1


def mount_facts(
    universe: Universe, only: Collection[str] | None = None
) -> tuple[list[Fact], list[Diagnostic]]:
    """One plugin fact per mounted module of every tracked `path_scopes.toml`, and the mounts that miss.

    `only` restricts the files read, for a run that looks at the changed files alone. A mount with no
    paths governs nothing and is no edge. A module that is not in this checkout is an
    `unresolved-import`, since the test suite would fail to import it.
    """
    facts: list[Fact] = []
    problems: list[Diagnostic] = []
    for path in sorted(universe.files):
        if path.rsplit("/", 1)[-1] != SCOPE_FILE or (only is not None and path not in only):
            continue
        text = (universe.root / path).read_text(encoding="utf-8")
        try:
            document = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise MeizelError(f"{path}: invalid TOML: {error}") from error
        lines = text.splitlines()
        for module, scope in document.items():
            if not scope:
                continue
            line = _line_of(lines, module)
            found = universe.resolve_module(module)
            if found is None or found.file is None:
                problems.append(
                    Diagnostic(
                        path,
                        line,
                        Code.UNRESOLVED_IMPORT,
                        f"{path} mounts {module!r}, which is not a module of this checkout",
                    )
                )
                continue
            facts.append(
                Fact(path, found.file, line, FactKind.PLUGIN, (), f"path_scopes mount {module}")
            )
    return facts, problems
