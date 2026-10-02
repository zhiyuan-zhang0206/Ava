"""A synthetic repository for the placement and patch-target tests.

Three units (`cli` > `ava` > `base` by an import-linter layers contract) and a handful of
modules with public and private names. `services/` is absent on purpose: a repository
without it is legal, and `services.<x>` units only appear when a test adds one.
"""

from __future__ import annotations

import pathlib

PYPROJECT = """\
[tool.importlinter]
root_packages = ["base", "ava", "cli"]

[[tool.importlinter.contracts]]
name = "Layers"
type = "layers"
layers = ["cli", "ava", "base"]
"""

SOURCES = {
    "base/__init__.py": "",
    "base/net/__init__.py": "",
    "base/net/retry.py": (
        'import time\n\nREGISTRY = {"retries": 3}\n_TABLE = {"delay": 1}\n\n\n'
        "def backoff():\n    return 1\n\n\n"
        "def _sleep(seconds):\n    time.sleep(seconds)\n"
    ),
    "base/db/__init__.py": "def connect():\n    return None\n",
    "base/db/pool.py": "_pool = None\n\n\ndef acquire():\n    return _pool\n",
    "base/config/__init__.py": "settings = None\n",
    "ava/__init__.py": "",
    "ava/agents/__init__.py": "",
    "ava/agents/_client.py": "def send():\n    return None\n",
    "ava/agents/api.py": "def spawn():\n    return None\n",
    "cli/__init__.py": "",
    "cli/commands/__init__.py": "",
    "cli/commands/run.py": "def main():\n    return 0\n",
    "cli/commands/_util.py": "def _helper():\n    return 0\n",
}


def write(root: pathlib.Path, rel: str, text: str) -> pathlib.Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_repo(root: pathlib.Path, extra: dict[str, str] | None = None) -> pathlib.Path:
    """Write the synthetic repository into `root` (and `extra` files on top)."""
    write(root, "pyproject.toml", PYPROJECT)
    write(root, "scripts/structure/baseline/README.md", "Structure baseline shards.\n")
    for rel, text in {**SOURCES, **(extra or {})}.items():
        write(root, rel, text)
    return root
