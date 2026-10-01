"""A map from dotted module name to a value built on first access, for lints that index the repo.

A lint that resolves calls and imports across modules needs "does this module exist" for every
name it meets but a parsed value for only a few. Building the value on demand keeps a run that
judges a few files proportional to what those files reach, instead of to the repository.
"""

from __future__ import annotations

from collections.abc import Callable


class ModuleMap[T]:
    """dotted module -> a value built on first access; `in` asks whether the module exists."""

    def __init__(self, exists: Callable[[str], bool], build: Callable[[str], T | None]) -> None:
        self._exists = exists
        self._build = build
        self._built: dict[str, T | None] = {}

    def get(self, name: str) -> T | None:
        """The built value; None for an unknown module or one `build` returned None for."""
        if not self._exists(name):
            return None
        if name not in self._built:
            self._built[name] = self._build(name)
        return self._built[name]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self._exists(name)

    def __getitem__(self, name: str) -> T:
        built = self.get(name)
        if built is None:
            raise KeyError(name)
        return built
