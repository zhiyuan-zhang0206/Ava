#!/usr/bin/env python3
"""Lint: every `meiva.<name>` reference in the procedural docs must resolve to a live member.

When an SDK module is deleted (the recent `meiva.monitor` / `meiva.schedule` removal),
nothing keeps the docs from carrying a now-dangling `meiva.monitor.*` reference for
nobody to catch. This lint closes that gap: it scans `docs/conventions/**/*.md` and
`.agents/skills/**/*.md`, pulls every `meiva.<name>` reference, and asserts the FIRST
segment after `meiva.` is a real member of the live `meiva` namespace. A deletion that
forgets to scrub a doc fails the commit, at the source, in the same PR.

## Valid-name source of truth (built from the live SDK, never hardcoded)

`valid_meiva_names()` imports `meiva` and unions four sets, so it tracks the SDK
automatically — add a submodule and it's instantly valid; delete one and its docs
references go stale here:
  1. `agent_visible_names(meiva)` — the agent-facing top-level surface (`agents`, `shell`,
     `self`, ...), read from `meiva.__all_for_meiva__` plus any plugin declarations;
  2. real submodule names — every `meiva/<x>.py` file and `meiva/<x>/` package (this also
     covers the private `_settings` module the design docs legitimately cite);
  3. public attributes on the `meiva` module object — module-level names like `state`,
     `state_update`, `const`, `help`;
  4. plugin-declared namespace names — the strings in `SdkNamespace("X", ...)`
     declarations across `plugins/*/plugin.py` (e.g. `cwd` from `meiva_code`). These are real
     `meiva.*` surface at agent runtime but invisible to a bare `import meiva`, so they are
     discovered by the same AST scan `agent_docstrings.py` uses — the two lints stay
     coupled to one definition of "what a plugin declares".

Only the FIRST segment is validated: `meiva.shell.list` -> check `shell`;
`meiva.watcher.cron` -> check `watcher`; `meiva.AGENT_ID` -> check `AGENT_ID`. Anything
deeper (method names, attributes) is out of scope — too dynamic to verify offline
without false positives.

## Zero-false-positive design

Prose is where false positives live (a sentence that happens to say "meiva." mid-flow,
a hostname like `meiva.example.com`, a metasyntactic `meiva.X`). Two guards keep this lint
a wall, not a nuisance:

  - **Code-span restriction.** Only `meiva.<name>` references that sit inside an
    inline-code span (`` `...` ``) or a fenced code block (``` ``` ```) are checked. A
    bare prose mention is never flagged — it is exactly the ambiguous case the
    graduation test (`docs/conventions/engineering/lint-vs-sweeper.md`) says belongs in the sweeper, not
    a commit-blocking lint.
  - **Non-symbol forms excluded.** Even inside code, three forms are not symbols and
    are skipped: a name segment followed by a domain-style suffix (`meiva.host.com`) is a
    hostname; the single-uppercase-letter metasyntactic placeholder (`meiva.X` / `meiva.Y`,
    the docstring stand-in for "any submodule") is a teaching device; and a dunder
    (`meiva.__all_for_meiva__`, `meiva.__file__`) is Python protocol surface, not a deletable SDK
    member. None of them is the "deleted SDK symbol" drift class this lint targets.

What remains after those guards is an unambiguous `meiva.<identifier>` written as code
whose first segment is not a live member — a genuine dangling reference. `_EXEMPT`
carries the rare legitimate reference the heuristics can't model.

Scope is the *procedural* docs — `docs/conventions/` plus the repo's own dev skills under
`.agents/skills/` (`.meiva/skills` and `.claude/skills` are links back to it). Both tell an agent what to do right now, so a stale `meiva.*` in either one
is a live instruction to call a symbol that no longer exists. `docs/decisions/` is a
point-in-time archive that is never rewritten (its `meiva.monitor` snapshots are correct
history, not drift) and `future/` is design space that may name not-yet-built
surface — both are out of scope to stay zero-FP.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterator
from pathlib import Path

from base.host.env.dotenv_boot import enter_scratch_home

if __name__ == "__main__":
    enter_scratch_home()

import meiva

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOCS_CONVENTIONS = _REPO_ROOT / "docs" / "conventions"
DEV_SKILLS = _REPO_ROOT / ".agents" / "skills"

# `meiva.<name>` — first segment only. `\b` anchors so `lava.x` doesn't match; the
# name segment is a Python-identifier-shaped token.
_MEIVA_REF = re.compile(r"\bmeiva\.([A-Za-z_][A-Za-z0-9_]*)")

# Inline-code span on a prose line: text between single backticks (no embedded
# backtick / newline). Fenced blocks are handled separately by line state.
_INLINE_CODE = re.compile(r"`([^`\n]+)`")

# A name segment immediately followed by a domain-style suffix is a hostname
# (`meiva.example.com`), not an SDK symbol — skip it.
_DOMAIN_SUFFIX = re.compile(r"\.(?:com|net|org|io|dev|app|local)\b")

# A name segment immediately followed by `.okf.md` is an OKF document filename
# (`meiva.meiva.okf.md`), not an SDK symbol — skip it.
_OKF_FILENAME = re.compile(r"\.okf\.md\b")

# Single uppercase letter (optionally with a trailing digit) = the docstring
# metasyntactic stand-in for "any submodule" (`help(meiva.X)`, `@wrap(meiva.X.y)`),
# never a real member.
_METASYNTACTIC = re.compile(r"^[A-Z][0-9]?$")

# Dunder attribute (`meiva.__all_for_meiva__`, `meiva.__file__`) = Python protocol
# surface the design docs cite, not deletable SDK members — out of scope, skip
# it. `[a-z_]` so multi-word dunders like `__all_for_meiva__` are covered too.
_DUNDER = re.compile(r"^__[a-z_]+__$")

# Escape valve for a legitimate reference the heuristics can't model. Each entry
# is a first-segment name; normally empty, same allowlist convention as the
# sibling local lints.
_EXEMPT: set[str] = {
    "git",  # the clone URL's `.../meiva.git` (install / dev-setup)
    "fleet",  # localStorage key prefix "meiva.fleet.*", not an SDK member
    "active",  # localStorage key prefix "meiva.active.*", not an SDK member
    # The OTel root-span attribute keys "meiva.checkpoint_id" / "meiva.turn"
    # (base/telemetry/tracing.py) — span attribute names, not SDK members.
    "checkpoint_id",
    "turn",
}


def _plugin_namespace_names() -> set[str]:
    """Namespace names declared by `SdkNamespace("X", ...)` in plugins.

    These are real `meiva.X` surface at agent runtime (e.g. `cwd` from `meiva_code`) but
    are not present on a bare `import meiva`, so the docs may legitimately cite them.
    Discovered by the same AST scan `agent_docstrings.py` uses, keeping the two
    lints coupled to one notion of "what a plugin registers".
    """
    names: set[str] = set()
    for plugin_py in _REPO_ROOT.glob("meiva_builtins/plugins/*/plugin.py"):
        tree = ast.parse(plugin_py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "SdkNamespace"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                names.add(node.args[0].value)
    return names


def valid_meiva_names() -> set[str]:
    """The live set of valid first-segments after `meiva.`.

    Union of: `agent_visible_names(meiva)` (the agent surface), real `meiva/<x>.py` /
    `meiva/<x>/` submodule names, public attributes on the `meiva` module object, and
    plugin-registered namespace names. Built fresh from the imported `meiva` package so a
    deletion or addition to the SDK moves the valid set with it — no hardcoded list to
    drift.
    """
    names: set[str] = set(meiva.agent_visible_names(meiva))

    meiva_dir = Path(meiva.__file__).resolve().parent
    for entry in meiva_dir.iterdir():
        if entry.suffix == ".py" and entry.stem != "__init__":
            names.add(entry.stem)
        elif entry.is_dir() and (entry / "__init__.py").exists():
            names.add(entry.name)

    names.update(n for n in dir(meiva) if not n.startswith("__"))
    names.update(_plugin_namespace_names())
    names.update(_EXEMPT)
    return names


def referenced_symbols(md_text: str) -> set[str]:
    """First-segments of every checkable `meiva.<name>` reference in `md_text`.

    "Checkable" = sits inside an inline-code span or a fenced code block, and is not a
    hostname suffix or a single-letter metasyntactic placeholder. Bare prose mentions
    are deliberately NOT returned — that is the zero-false-positive guard.
    """
    found: set[str] = set()
    in_fence = False
    fence_marker = ""
    for line in md_text.splitlines():
        stripped = line.lstrip()
        if not in_fence and (stripped.startswith(("```", "~~~"))):
            in_fence = True
            fence_marker = stripped[:3]
            continue
        if in_fence:
            if stripped.startswith(fence_marker):
                in_fence = False
            else:
                found.update(_scan_fragment(line))
            continue
        for span in _INLINE_CODE.finditer(line):
            found.update(_scan_fragment(span.group(1)))
    return found


def _scan_fragment(fragment: str) -> set[str]:
    """Pull checkable `meiva.<name>` first-segments out of one code fragment."""
    names: set[str] = set()
    for m in _MEIVA_REF.finditer(fragment):
        name = m.group(1)
        if _METASYNTACTIC.match(name) or _DUNDER.match(name):
            continue
        if m.start() > 0 and fragment[m.start() - 1] == ".":
            # Filename fragment like `index.meiva.okf.md` — the `meiva` segment is
            # part of a dotted filename, not an SDK reference.
            continue
        rest = fragment[m.end() :]
        if _OKF_FILENAME.match(rest):
            # `meiva.meiva.okf.md` — an OKF filename whose stem is `meiva`, not a
            # reference to an `meiva.meiva` symbol.
            continue
        if _DOMAIN_SUFFIX.match(rest):
            continue
        names.add(name)
    return names


def _fenced_fragments(text: str) -> Iterator[tuple[int, str]]:
    """`(line number, code fragment)` of every checkable fragment: fenced-block lines, inline code."""
    in_fence = False
    fence_marker = ""
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if not in_fence and (stripped.startswith(("```", "~~~"))):
            in_fence = True
            fence_marker = stripped[:3]
            continue
        if in_fence:
            if stripped.startswith(fence_marker):
                in_fence = False
                continue
            yield lineno, line
        else:
            for m in _INLINE_CODE.finditer(line):
                yield lineno, m.group(1)


def _dangling_refs(root: Path, valid: set[str]) -> list[str]:
    """One message per `meiva.<name>` reference under `root` that names no SDK member."""
    # Display paths relative to the repo root when the scanned tree lives under
    # it (the real run); fall back to the root's parent when it is monkeypatched
    # to a tmp tree under test.
    display_root = _REPO_ROOT if root.is_relative_to(_REPO_ROOT) else root.parent
    violations: list[str] = []
    for md in sorted(root.rglob("*.md")):
        for lineno, fragment in _fenced_fragments(md.read_text(encoding="utf-8")):
            for name in _scan_fragment(fragment):
                if name not in valid:
                    rel = md.relative_to(display_root)
                    violations.append(
                        f"{rel}:{lineno}: meiva.{name} is not a member of the meiva "
                        f"namespace (valid: {', '.join(sorted(valid))})"
                    )
    return violations


def check() -> int:
    """Validate every procedural-doc `meiva.<name>` ref; return 0 (clean) or 1 (violations)."""
    valid = valid_meiva_names()
    violations: list[str] = []
    # Read the roots through the module globals so a test can monkeypatch either
    # one at a throwaway tree.
    for root in (DOCS_CONVENTIONS, DEV_SKILLS):
        if root.is_dir():
            violations.extend(_dangling_refs(root, valid))
    if violations:
        for v in violations:
            print(v, file=sys.stderr)
        print(
            f"\n{len(violations)} dangling meiva.* reference(s) in conventions or "
            ".agents/skills. A deleted/renamed SDK member left a stale doc reference — fix "
            "the doc (or add the name to _EXEMPT if it is a legitimate non-member form).",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    return check()


if __name__ == "__main__":
    sys.exit(main())
