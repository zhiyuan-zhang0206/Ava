"""Explicit executable identity for the single start lifecycle.

A start runs the checkout that loaded this code, with the interpreter that loaded
it. The identity is captured once and rechecked before each lifecycle phase; it
does not grant migration, writer closure, or any other operation right.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from base.deploy.release import runtime_interpreter
from base.deploy.release.runtime_interpreter import LoadedRuntimeIdentity


class StartRuntimeChangedError(ValueError):
    """The captured start runtime no longer matches the executing checkout."""


@dataclass(frozen=True)
class StartRuntime:
    """The checkout, working directory and interpreter one start executes."""

    code_root: Path
    cwd: Path
    interpreter: Path

    @classmethod
    def development(cls, checkout: Path) -> StartRuntime:
        return StartRuntime(checkout, checkout, Path(sys.executable).absolute())

    def identity(self) -> LoadedRuntimeIdentity:
        return runtime_interpreter.verify_loaded_source(self.code_root)

    def module_argv(self, module: str, *arguments: str) -> list[str]:
        return [str(self.interpreter), "-m", module, *arguments]

    def validate(self) -> None:
        """Recheck the captured runtime before a lifecycle phase consumes its paths."""
        if self != self.development(self.code_root):
            raise StartRuntimeChangedError("development start runtime changed")
