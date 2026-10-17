"""Forbid bare os.environ / os.getenv — runtime config must go through base.config.Settings.

Run: `.venv/bin/python scripts/lint/no_os_environ.py [path ...]` (that launcher binds the
config registry and calls `main` here; defaults to scanning the whole repo;
an explicit path that does not exist is an error (stderr + exit 1) rather than a
silent no-op). Also run automatically via pre-commit hook before commit.

## Why

`base.config.settings` (the `Settings` aggregate in `base/config/`) is the single
source of truth for runtime config. Scattered `os.environ.get("MEIVA_X")` causes:
- defaults drift from Settings
- types (bool/int/float) are unvalidated; ValueError surfaces only at use, not at startup
- the frontend Control page does not see newly-added vars
- review cannot trace "where does this variable come from"

## Two rules

### Rule 1: Non-test code

Scan all non-test .py files under `agent/`, `base/`,
`gateway/`, `services/`, `meiva/`, `scripts/` (test_*.py / *_test.py /
any `tests/` directory excluded). Any reference to `os.environ` or `os.getenv` is an error,
unless the file is in _ALLOWED_FILES (Settings itself + .env loader +
bootstrap that cannot depend on Settings).

Adding a new _ALLOWED_FILES entry requires demonstrating "cannot go through
Settings" — bootstrap ordering constraints; provider-key reads are centralized
behind ``provider_api.require_key``, and other SDK-internal raw env is owned by
Settings fields. No inline exemption mechanism — avoids scattered hard-to-audit
`# noqa`-style escape hatches.

### Rule 2: Test code

Scan all `monkeypatch.setenv("X", ...)` / `monkeypatch.delenv("X")` /
`os.environ["X"] = ...` calls in `tests/`, `test_*.py`, `*_test.py`; if
X is in Settings's **model_fields alias set**, error — the `Settings`
singleton is built once and its fields hold the environment as it stood then
(the pytest conftest builds it eagerly at import, `MEIVA_CONFIG_BOOT=eager`; a
production process builds it at the first config read beyond the boot-lite
surface, see `base/config/__init__.py`), so later setenv/delenv cannot reach
`settings.x`, and the test silently no-ops.
Must switch to `monkeypatch.setattr(settings, "<field_name>", value)` —
except provider API-key env vars declared in `_PROVIDER_KEY_ENV_VARS` and the
live-read knobs in `_LIVE_READ_ENV_VARS` (see below): those are read from the
environment live by design, so setenv is the real seam.

The alias set is dynamically read from `the config field registry` — adding a
new field to Settings auto-syncs the ban list; no manual maintenance.
Historical bugs: PR #327 hit this pattern twice (test_loop_main's
MEIVA_MCP_SOCKET, base/daemon/tests/test_health.py's MEIVA_SCHEDULER_HEALTH_PORT).

Error format `file:line: <line content>` + non-zero exit.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path

from meizel import allowances, lint_common
from meizel.roles import packages

__all__ = ["main"]

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Framework dirs Rule 1 does not scan. A test file under them (a package's own
# `<pkg>/**/tests/`) is still read for Rule 2, so the default scan visits their
# test files only.
_TEST_ONLY_DIRS = ("meiva_builtins", "cli", "ops")

# Scan directories — only OUR code, do not scan .venv / vendor / node_modules.
_SCAN_DIRS = (
    *(d for d in packages("application") if d not in _TEST_ONLY_DIRS),
    *packages("tooling"),
)

# Everything below is listed in meizel.policy.toml under [no-os-environ], each entry with its reason.
_POLICY = allowances.load(_REPO_ROOT)

# Provider API-key env vars are read from os.environ at build time
# (base/lm/provider_api.require_key) once the provider is a plugin — the Settings field is retired
# from that provider's key path (task #2505). In tests, monkeypatch.setenv on these is a REAL seam
# (require_key reads env live), not a Settings-singleton no-op. Each key must match a
# ProviderBinding.key_env.
_PROVIDER_KEY_ENV_VARS = frozenset(_POLICY.reasons("no-os-environ", "provider-key-vars"))

# Vars read from the live environment BY DESIGN (not via the Settings singleton, which holds the
# environment as it stood when it was built): in tests, monkeypatch.setenv on them is a REAL seam,
# the same class as the provider keys above. Every entry must have its reader file in _ALLOWED_FILES.
_LIVE_READ_ENV_VARS = frozenset(_POLICY.reasons("no-os-environ", "live-read-vars"))

# File-level exemption — must demonstrate "cannot go through Settings": bootstrap ordering /
# dynamic env enumeration. A new entry is a policy change with a one-line reason.
_ALLOWED_FILES = frozenset(_POLICY.reasons("no-os-environ", "files"))

# Test files — pytest fixtures mocking env is a legitimate use
_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
)

# Match os.environ. / os.environ[ / os.getenv(  (word boundary on both sides).
_OS_ENV_PATTERN = re.compile(r"\bos\.(environ|getenv)\b")

# Match only `monkeypatch.setenv("X"...)` / `monkeypatch.delenv("X"...)` —
# pytest fixture calls, which always happen after Settings import (inside
# the test function). Deliberately does **not** match `os.environ["X"] = ...`
# — some env in conftest is bootstrap before Settings import, or used for
# subprocesses (gateway / agent fork); neither is the settings-singleton bug.
_TEST_SETENV_PATTERN = re.compile(
    r"\bmonkeypatch\.(?:setenv|delenv)\s*\(\s*['\"]([A-Z_][A-Z0-9_]*)['\"]"
)

# Triple-quoted docstring literals like `monkeypatch.setenv("MEIVA_X"...)`
# (e.g. a fixture's docstring describing the call) are not violations —
# track triple-quote state line-by-line and skip inside docstring ranges.
_TRIPLE_QUOTE_PATTERN = re.compile(r'"""|\'\'\'')


def _is_test_file(rel_path: str) -> bool:
    return any(p.search(rel_path) for p in _TEST_PATTERNS)


def _scan_file(
    path: Path, rel_path: str, managed_envs: frozenset[str]
) -> list[tuple[int, str, str]]:
    """Return error list [(lineno, line_stripped, error_kind), ...].

    error_kind = "naked-env" (Rule 1) | "setenv-managed" (Rule 2).
    """
    if rel_path in _ALLOWED_FILES:
        return []
    is_test = _is_test_file(rel_path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []  # unreadable entry (e.g. a dangling symlink) or binary content
    violations: list[tuple[int, str, str]] = []
    inside_triple = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        # Triple-quote state machine: count occurrences of """ / ''' per
        # line; odd count flips the inside state. We do not parse nested
        # string semantics, but this suffices (docstrings not written on a
        # single line `"""..."""` have all middle lines inside the triple range).
        quote_count = len(_TRIPLE_QUOTE_PATTERN.findall(line))
        line_starts_inside = inside_triple
        if quote_count % 2:
            inside_triple = not inside_triple
        # Entire line in docstring range (both start and end inside) -> skip
        if line_starts_inside and inside_triple:
            continue
        # `#` splits the line into code and comment; only check the code part.
        code, _, _ = line.partition("#")
        if is_test:
            # Rule 2: monkeypatch.setenv/delenv in tests changing a Settings-managed env (silent no-op).
            for m in _TEST_SETENV_PATTERN.finditer(code):
                env_name = m.group(1)
                if (
                    env_name in managed_envs
                    and env_name not in _PROVIDER_KEY_ENV_VARS
                    and env_name not in _LIVE_READ_ENV_VARS
                ):
                    violations.append((lineno, line.strip(), f"setenv-managed:{env_name}"))
        elif _OS_ENV_PATTERN.search(code):
            # Rule 1: bare os.environ in non-test code.
            violations.append((lineno, line.strip(), "naked-env"))
    return violations


def _iter_py_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            files.extend(root.rglob("*.py"))
    return files


def _files_to_scan(argv: list[str], scope: frozenset[str] | None, root: Path) -> list[Path]:
    """Explicit paths as given; otherwise every file the default scan reads, narrowed to the
    `--only` changed files when `scope` names them."""
    if argv:
        return _iter_py_files([Path(a).resolve() for a in argv])
    targets = [root / d for d in _SCAN_DIRS] + [root / "tests"]
    targets += [root / d for d in _TEST_ONLY_DIRS if (root / d).is_dir()]
    default = [p for p in _iter_py_files(targets) if _in_default_scope(p, root)]
    return lint_common.restrict(default, scope, root)


def _rel_label(path: Path, root: Path) -> str:
    """Repo-relative posix path, or the absolute path of a file outside the repo."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


# The config registry decides which env names Rule 2 treats as Settings-managed, for every test.
_RULE_INPUTS = ("base/config/", "base/host/env/")


def _in_default_scope(path: Path, root: Path) -> bool:
    """Under a `_TEST_ONLY_DIRS` root only test files are scanned (Rule 2 only)."""
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return True
    if rel.split("/", 1)[0] in _TEST_ONLY_DIRS:
        return _is_test_file(rel)
    return True


def main(
    argv: list[str] | None = None,
    *,
    managed_aliases: Callable[[], frozenset[str]],
    repo_root: Path | None = None,
) -> int:
    """Run the lint; `managed_aliases` returns the env names the config registry owns."""
    root = repo_root if repo_root is not None else _REPO_ROOT
    argv = argv if argv is not None else sys.argv[1:]
    argv, only = lint_common.split_only(argv)
    # argv non-empty = explicit paths; empty = default scan of all _SCAN_DIRS + tests/, or
    # the `--only` changed files (the commit hook) under that same scope.
    if argv:
        missing = [arg for arg in argv if not Path(arg).exists()]
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
    scope = lint_common.changed_scope(only, root, inputs=_RULE_INPUTS)
    py_files = _files_to_scan(argv, scope, root)
    # Only Rule 2 (tests) reads the config registry, and importing it is the slow part of a
    # run that judges a few non-test files.
    managed_envs: frozenset[str] = (
        managed_aliases()
        if any(_is_test_file(_rel_label(path, root)) for path in py_files)
        else frozenset[str]()
    )

    total_violations = 0
    for path in sorted(py_files):
        rel = _rel_label(path, root)
        violations = _scan_file(path, rel, managed_envs)
        for lineno, content, kind in violations:
            total_violations += 1
            if kind.startswith("setenv-managed:"):
                env = kind.split(":", 1)[1]
                print(
                    f"{rel}:{lineno}: monkeypatch.setenv/delenv on `{env}` — Settings is a singleton "
                    f"built once; env changes do not reach the settings instance field. Use "
                    f'`monkeypatch.setattr(settings, "<field_name>", value)` instead.'
                )
            else:
                print(
                    f"{rel}:{lineno}: bare os.environ/getenv usage -> route through base.config.settings"
                )
            print(f"    {content}")

    if total_violations:
        print(
            f"\n{total_violations} violations total. See the docstring at the top of "
            "meizel/state/no_os_environ.py for the exemption procedure and the rationale "
            "for using setattr in tests.",
            file=sys.stderr,
        )
        return 1
    return 0
