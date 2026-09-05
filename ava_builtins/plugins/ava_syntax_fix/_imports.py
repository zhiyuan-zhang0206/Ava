"""
Step 2 of the syntax-fix pipeline: missing-import detection.

ruff F821 (undefined name) finds names used but never bound; curated
tables map each to the one unambiguous import statement. Split out
of plugin.py (2026-08-07, Task #1011).
"""

from __future__ import annotations

import functools
import importlib.util
import json
import pathlib
import re
import shutil
import subprocess
import sys

from loguru import logger

# ---------------------------------------------------------------------------
# 2. Missing import detection (ruff F821 + curated name -> import mapping)
# ---------------------------------------------------------------------------
#
# ruff's F821 (undefined name) does the detection: it is scope-aware (a name
# the code assigns, imports, or binds as a function/comprehension target is
# never flagged) and module-level use-before-import IS flagged. Each undefined
# name is then mapped to an import statement via the tables below. Names with
# no mapping (genuinely undefined variables, typos) are left alone: they raise
# NameError at runtime and the agent fixes its own code -- auto-fix covers only
# the cases with one unambiguous resolution.

# Provided by the execution namespace -- never auto-imported.
_PREINJECTED_NAMES: frozenset[str] = frozenset()

# Bare names -> from-import. Curated from runtime NameErrors observed in agent
# logs (`Path` dominates by a wide margin) plus names models habitually use
# unqualified. Only names with one unambiguous resolution belong here; anything
# plausibly meant as a local variable (`run`, `choice`, `match`) stays out.
_BARE_NAME_IMPORTS: dict[str, str] = {
    "ava": "import ava",
    "Path": "from pathlib import Path",
    "PurePath": "from pathlib import PurePath",
    "Counter": "from collections import Counter",
    "defaultdict": "from collections import defaultdict",
    "deque": "from collections import deque",
    "OrderedDict": "from collections import OrderedDict",
    "namedtuple": "from collections import namedtuple",
    "date": "from datetime import date",
    "timedelta": "from datetime import timedelta",
    "timezone": "from datetime import timezone",
    "dataclass": "from dataclasses import dataclass",
    "field": "from dataclasses import field",
    "asdict": "from dataclasses import asdict",
    "Any": "from typing import Any",
    "Optional": "from typing import Optional",
    "Union": "from typing import Union",
    "Literal": "from typing import Literal",
    "Callable": "from typing import Callable",
    "Iterable": "from typing import Iterable",
    "Iterator": "from typing import Iterator",
    "Sequence": "from typing import Sequence",
    "Mapping": "from typing import Mapping",
    "TypedDict": "from typing import TypedDict",
    "NamedTuple": "from typing import NamedTuple",
    "TypeVar": "from typing import TypeVar",
    "cast": "from typing import cast",
    "partial": "from functools import partial",
    "reduce": "from functools import reduce",
    "lru_cache": "from functools import lru_cache",
    "cache": "from functools import cache",
    "wraps": "from functools import wraps",
    "Decimal": "from decimal import Decimal",
    "Fraction": "from fractions import Fraction",
    "Enum": "from enum import Enum",
    "IntEnum": "from enum import IntEnum",
    "StrEnum": "from enum import StrEnum",
    "auto": "from enum import auto",
    "ABC": "from abc import ABC",
    "abstractmethod": "from abc import abstractmethod",
    "sleep": "from time import sleep",
    "chain": "from itertools import chain",
    "islice": "from itertools import islice",
    "product": "from itertools import product",
    "combinations": "from itertools import combinations",
    "permutations": "from itertools import permutations",
    "groupby": "from itertools import groupby",
    "ThreadPoolExecutor": "from concurrent.futures import ThreadPoolExecutor",
    "ProcessPoolExecutor": "from concurrent.futures import ProcessPoolExecutor",
    "as_completed": "from concurrent.futures import as_completed",
    "StringIO": "from io import StringIO",
    "BytesIO": "from io import BytesIO",
    "dedent": "from textwrap import dedent",
    "glob": "from glob import glob",
    "Popen": "from subprocess import Popen",
    "PIPE": "from subprocess import PIPE",
    "check_output": "from subprocess import check_output",
    "urlparse": "from urllib.parse import urlparse",
    "urljoin": "from urllib.parse import urljoin",
    "urlencode": "from urllib.parse import urlencode",
    "quote": "from urllib.parse import quote",
    "unquote": "from urllib.parse import unquote",
    "contextmanager": "from contextlib import contextmanager",
    "suppress": "from contextlib import suppress",
    "deepcopy": "from copy import deepcopy",
    "uuid4": "from uuid import uuid4",
    "randint": "from random import randint",
    "shuffle": "from random import shuffle",
    "NamedTemporaryFile": "from tempfile import NamedTemporaryFile",
    "TemporaryDirectory": "from tempfile import TemporaryDirectory",
    "rmtree": "from shutil import rmtree",
    "ET": "import xml.etree.ElementTree as ET",
}

# Third-party aliases / modules -> (package to probe, import statement).
# Mapped only when the package is importable here -- agent code runs in this
# same environment, so find_spec is the exact truth. An uninstalled package is
# a different problem (the agent must see it and decide to install), not a
# missing import line.
_THIRD_PARTY_IMPORTS: dict[str, tuple[str, str]] = {
    "np": ("numpy", "import numpy as np"),
    "numpy": ("numpy", "import numpy"),
    "pd": ("pandas", "import pandas as pd"),
    "pandas": ("pandas", "import pandas"),
    "plt": ("matplotlib", "import matplotlib.pyplot as plt"),
    "sns": ("seaborn", "import seaborn as sns"),
    "requests": ("requests", "import requests"),
    "httpx": ("httpx", "import httpx"),
    "aiohttp": ("aiohttp", "import aiohttp"),
    "yaml": ("yaml", "import yaml"),
    "bs4": ("bs4", "import bs4"),
    "BeautifulSoup": ("bs4", "from bs4 import BeautifulSoup"),
    "tqdm": ("tqdm", "from tqdm import tqdm"),
}

# stdlib submodules that need an explicit `import parent.child` -- importing
# the bare parent does not bind them. Deeper-nested packages (xml.etree.*,
# email.mime.*) are deliberately absent: a two-level import would not bind the
# leaf either, so they stay on the runtime-error path.
_STDLIB_SUBMODULES = frozenset(
    {
        "urllib.parse",
        "urllib.request",
        "urllib.error",
        "concurrent.futures",
        "collections.abc",
        "importlib.util",
        "importlib.metadata",
        "importlib.resources",
        "logging.handlers",
        "logging.config",
        "multiprocessing.pool",
    }
)

# Attributes that live on the datetime *class*, not the datetime module --
# used to tell `datetime.now()` (the model meant the class) apart from
# `datetime.datetime.now()` (the model meant the module).
_DATETIME_CLASS_ATTRS = frozenset(
    {
        "now",
        "today",
        "utcnow",
        "strptime",
        "fromisoformat",
        "fromtimestamp",
        "utcfromtimestamp",
        "fromordinal",
        "combine",
        "min",
        "max",
    }
)

_F821_MSG_RE = re.compile(r"Undefined name `([^`]+)`")


# Every ruff `subprocess.run` in this module carries a `noqa: S603` marker: the argv is
# a fixed list built in-process (this resolved path + literal flags), and the
# only thing that varies is the agent's source, which is piped to stdin and
# never interpolated into the command. S603 fires solely because the executable
# is a resolved path rather than a string literal.
@functools.cache
def _ruff_executable() -> str:
    """Absolute path to the ``ruff`` that ships in this interpreter's env.

    The three ruff-backed fixers below each swallow ``FileNotFoundError`` and
    return their input unchanged, so an unresolvable binary degrades them to a
    silent no-op instead of failing loudly. A bare ``"ruff"`` makes that hinge
    on ambient ``PATH``: agent processes only work because their launcher
    prepends the venv's ``bin`` (`ops/agent_launch.py`), an implicit contract
    owned by a different module, and any caller that skips it (a direct
    ``.venv/bin/python`` run, an eval harness) silently loses the whole stage.
    Anchoring on ``sys.executable`` binds ruff to the environment of the running
    interpreter, which is where the dependency is installed. Falls back to a
    ``PATH`` lookup so an interpreter outside a ruff-carrying env still finds a
    system-wide install.
    """
    return shutil.which("ruff", path=str(pathlib.Path(sys.executable).parent)) or "ruff"


# Wall-clock timeout for every ruff subprocess pass in this package. A timeout
# used to collapse into silent pass-through (issue #159): the repair stage
# reported success-by-omission, indistinguishable from "ruff ran and found
# nothing to fix". Every give-up below now logs — never silent.
_RUFF_TIMEOUT_SECONDS = 5


@functools.lru_cache(maxsize=1)
def _warn_ruff_missing_once() -> None:
    """Log a missing ruff executable once per process.

    ruff is an optional stage — a host without it skips the fix — but the skip
    must be visible. One line per process, not one per agent turn (issue #159).
    """
    logger.warning(
        f"ruff executable {_ruff_executable()!r} not found — syntax-fix "
        "ruff stage skipped (source passed through unchanged)"
    )


def _log_ruff_give_up(step: str, code: str, exc: BaseException) -> None:
    """Log why a ruff subprocess pass gave up, then let the caller pass through.

    FileNotFoundError = the optional stage is skipped on a host without ruff —
    one line per process via _warn_ruff_missing_once. TimeoutExpired and
    OSError and UnicodeError are silent-failure smells (issue #159): the first
    logs the elapsed budget and the input size (a timeout correlated with large
    inputs points at the budget, not the host), the second logs the errno
    (usually a symptom of something larger on the host), and the third logs the
    text codec failure.
    """
    if isinstance(exc, subprocess.TimeoutExpired):
        logger.warning(
            f"ruff {step} did not finish within {exc.timeout}s on "
            f"{len(code)}-char source — passing through unchanged"
        )
        return
    if isinstance(exc, FileNotFoundError):  # OSError subclass — check first
        _warn_ruff_missing_once()
        return
    if isinstance(exc, UnicodeError):
        logger.warning(
            f"ruff {step} failed with {type(exc).__name__} ({exc}) — passing through unchanged"
        )
        return
    if isinstance(exc, OSError):
        logger.warning(
            f"ruff {step} failed with OSError errno={exc.errno} "
            f"({exc.strerror}) — passing through unchanged"
        )
        return


def _f821_names(diagnostics: list[dict]) -> set[str]:
    """Extract the undefined names from a ruff F821 diagnostic list."""
    names: set[str] = set()
    for diag in diagnostics:
        # Non-F821 entries (syntax errors carry code=None) are not name hits.
        if diag["code"] != "F821":
            continue
        m = _F821_MSG_RE.search(diag["message"])
        if m:
            names.add(m.group(1))
    return names


def _ruff_undefined_names(code: str) -> set[str]:
    """Run `ruff check --select F821` via stdin; return the undefined names.

    Returns an empty set when ruff is unavailable, crashes, or the source does
    not parse (syntax errors surface downstream via compile()).
    """
    try:
        proc = subprocess.run(  # noqa: S603
            [
                _ruff_executable(),
                "check",
                "--select",
                "F821",
                "--output-format",
                "json",
                "--stdin-filename",
                "script.py",
                "-",
            ],
            input=code,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_RUFF_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, UnicodeError) as exc:
        # Same contract as _ruff_fix / _ruff_format (issue #159): a missing
        # ruff is logged once per process, a timeout / OS error at warning —
        # a detection stage that silently returns "no undefined names" would
        # leave missing imports un-repaired with zero signal.
        _log_ruff_give_up("check --select F821", code, exc)
        return set()
    # ruff exits 1 when diagnostics were found; >1 means it crashed.
    if proc.returncode > 1 or not proc.stdout.strip():
        return set()
    try:
        diagnostics = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return set()
    return _f821_names(diagnostics)


def _module_attrs(code: str, name: str) -> set[str]:
    """Attributes accessed as ``name.attr`` anywhere in the source. String
    literals are not excluded -- this only feeds intent inference (module vs
    class vs from-import), where the occasional in-string hit is harmless."""
    return {m.group(1) for m in re.finditer(rf"(?<![\w.]){name}\.([A-Za-z_]\w*)", code)}


def _datetime_import(attrs: set[str]) -> list[str]:
    """Resolve the datetime module/class ambiguity: `datetime.now()` means the
    class, `datetime.datetime` / mixed attr use means the module. Bare
    `datetime` with no attr -> the class."""
    if attrs and not attrs <= _DATETIME_CLASS_ATTRS:
        return ["import datetime"]
    return ["from datetime import datetime"]


def _stdlib_module_import(name: str, attrs: set[str]) -> list[str]:
    """Import statement(s) binding a stdlib module, submodule-aware:
    `urllib.parse.urlparse` yields `import urllib.parse`."""
    subs = sorted(f"{name}.{a}" for a in attrs if f"{name}.{a}" in _STDLIB_SUBMODULES)
    return [f"import {s}" for s in subs] if subs else [f"import {name}"]


def _is_bare_name_usage(name: str, attrs: set[str]) -> bool:
    """True when `name` should resolve to the curated bare-name import: it is
    in the table and, when it is also a stdlib module (`glob`), used as a bare
    function rather than as a module (`glob.glob(...)`)."""
    return name in _BARE_NAME_IMPORTS and not (attrs and _is_stdlib_module(name))


def _imports_for_name(name: str, code: str) -> list[str]:
    """Map one undefined name to the import statement(s) that bind it.

    Resolution order: pre-injected names (never imported) -> the datetime
    module/class disambiguation -> curated bare names -> stdlib modules
    (submodule-aware: `urllib.parse.urlparse` yields `import urllib.parse`) ->
    installed third-party aliases. Unmapped names return [] and stay on the
    runtime NameError path.
    """
    if name in _PREINJECTED_NAMES:
        return []
    attrs = _module_attrs(code, name)
    if name == "datetime":
        return _datetime_import(attrs)
    # A name in the bare table that is ALSO a stdlib module (`glob`) follows
    # its usage: `glob.glob(...)` wants the module, bare `glob(...)` the function.
    if _is_bare_name_usage(name, attrs):
        return [_BARE_NAME_IMPORTS[name]]
    if _is_stdlib_module(name):
        return _stdlib_module_import(name, attrs)
    if name in _THIRD_PARTY_IMPORTS:
        package, stmt = _THIRD_PARTY_IMPORTS[name]
        if importlib.util.find_spec(package) is not None:
            return [stmt]
    return []


def _detect_missing_imports(code: str) -> list[str]:
    """Find names used but never bound; return import statements to prepend.

    ruff F821 supplies the undefined names -- scope-aware, so existing imports,
    assignments, and local bindings are respected, while module-level
    use-before-import is still flagged. Each name is then mapped through
    `_imports_for_name`; names without a mapping are left for the runtime
    NameError (fail fast: only unambiguous resolutions are auto-fixed).
    """
    if not code.strip():
        return []
    stmts: set[str] = set()
    for name in _ruff_undefined_names(code):
        stmts.update(_imports_for_name(name, code))
    return sorted(stmts)


def _is_stdlib_module(name: str) -> bool:
    """Check whether *name* is a Python stdlib module.

    Uses ``sys.stdlib_module_names`` (Python 3.10+) for the canonical set.
    Returns False for names that are not stdlib modules or when the set is
    unavailable (Python < 3.10).
    """
    if not hasattr(sys, "stdlib_module_names"):
        return False
    return name in sys.stdlib_module_names


# Pre-computed triple-quote strings for docstring detection
_DQ3 = '"' * 3
_SQ3 = "'" * 3


def _skip_docstring(lines: list[str], i: int) -> int:
    """Advance past a module docstring starting at line i (which must begin
    with a triple quote). Returns the index just past the docstring."""
    stripped = lines[i].strip()
    quote = _DQ3 if stripped.startswith(_DQ3) else _SQ3
    if stripped.count(quote) >= 2:  # single-line docstring
        return i + 1
    for j in range(i + 1, len(lines)):
        if quote in lines[j]:
            return j + 1
    return i + 1  # unterminated -- treat as one line


def _import_insert_index(lines: list[str]) -> int:
    """Index at which import statements belong: after the module docstring,
    shebang, and any leading comments / blank lines."""
    insert_at = 0
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith(("#!", "#")):
            i += 1
            insert_at = i
            continue
        if stripped.startswith((_DQ3, _SQ3)):
            i = _skip_docstring(lines, i)
            insert_at = i
            continue
        break
    return insert_at


def _insert_imports(code: str, import_stmts: list[str]) -> str:
    """Insert import statements after module docstring / shebang / comments."""
    lines = code.split("\n")
    insert_at = _import_insert_index(lines)

    import_block = "\n".join(import_stmts)
    prefix = "\n" if (insert_at > 0 and lines[insert_at - 1].strip()) else ""
    suffix = "\n"
    import_block = prefix + import_block + suffix

    return "\n".join(lines[:insert_at]) + import_block + "\n".join(lines[insert_at:])
