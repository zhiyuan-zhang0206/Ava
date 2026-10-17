from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[3]


def load_skill_script(*parts: str, name: str | None = None) -> ModuleType:
    path = _REPO_ROOT.joinpath("meiva_builtins", "skills", *parts)
    module_name = name or f"{path.stem}_under_test"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
