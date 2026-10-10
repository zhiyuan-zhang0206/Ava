"""Require Python launches whose source and module the test-impact analysis can read.

Run: `.venv/bin/python scripts/lint/diagnostics/analyzable_launches.py [path ...]`
(defaults to every tracked Python file under the analyzed top-level packages; the
commit hook passes `--only` with the changed files). Exit 1 on a violation.

## Why

PR test selection (`scripts/ci/test_impact.py`) reads the imports of code a test
runs in a child Python process: the literal `-c` source and the literal `-m`
module. Source assembled at runtime (`str.format`, f-strings, concatenation) is
opaque, so every test that reaches it joins every PR subset. Keep the source a
literal and pass the varying data through argv or the environment, for example:

    _PROBE = "import sys, json\\nroot = sys.argv[1]\\n..."
    subprocess.run([sys.executable, "-c", _PROBE, str(root), json.dumps(names)])

## The rule

A Python launch (`subprocess.run`/`Popen`/... or `asyncio.create_subprocess_exec`
starting Python) must give the shared fact collector a literal `-c` source or
`-m` module (see `scripts/structure/imports/README.md`). A launch whose dynamic
source is itself under test, or whose source its caller supplies, carries
`# launch-ok: <reason>` on the reported line or on a comment line directly above it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from scripts.ci.test_impact import analyzed_tops  # noqa: E402 - standalone script
from scripts.structure import lint_common  # noqa: E402 - standalone script
from scripts.structure.imports.facts import FactKind, collect  # noqa: E402 - standalone script
from scripts.structure.placement import ModuleIndex  # noqa: E402 - standalone script

_LAUNCH_KINDS = frozenset({FactKind.EMBEDDED_IMPORT, FactKind.PYTHON_MODULE})
_EXEMPTION = "# launch-ok:"


def violations(path: Path, rel: str, index: ModuleIndex) -> list[tuple[int, str]]:
    """``(line, reason)`` for each launch the collector cannot read, minus exemptions."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    evidence = collect(ast.parse(text, filename=rel), rel, index, tops=analyzed_tops())
    found: list[tuple[int, str]] = []
    for gap in evidence.unknown:
        if gap.kind not in _LAUNCH_KINDS:
            continue
        # The reason sits on the reported line or on a comment line directly above it.
        nearby = lines[max(gap.line - 2, 0) : gap.line] if 0 < gap.line <= len(lines) else []
        if not any(_EXEMPTION in line for line in nearby):
            found.append((gap.line, gap.reason))
    return sorted(set(found))


def _default_files() -> list[Path]:
    tops = analyzed_tops()
    return [
        _REPO_ROOT / rel
        for rel in lint_common.tracked_files(_REPO_ROOT)
        if rel.endswith(".py") and rel.split("/", 1)[0] in tops
    ]


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    argv, only = lint_common.split_only(argv)
    if argv:
        missing = [arg for arg in argv if not Path(arg).exists()]
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
        files = [Path(arg).resolve() for arg in argv]
    else:
        scope = lint_common.changed_scope(only, _REPO_ROOT)
        files = lint_common.restrict(_default_files(), scope, _REPO_ROOT)
    index = ModuleIndex(_REPO_ROOT)
    total = 0
    for path in sorted(files):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        for line, reason in violations(path, rel, index):
            total += 1
            print(f"{rel}:{line}: {reason}")
    if total:
        print(
            f"\n{total} unreadable Python launch(es): keep `-c` source / `-m` module literal and "
            "pass data through argv or the environment (see scripts/lint/diagnostics/analyzable_launches.py)."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
