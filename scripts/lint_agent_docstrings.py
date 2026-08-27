"""Lint agent-visible docstrings — block Chinese characters + framework impl details.

Runs in pre-commit. Scope: `ava/*.py` (excluding private `_*.py` modules / `_`-prefixed packages and
`hooks.py`) + `plugins/*/*.py`. These docstrings are concatenated into the
LLM system prompt via `ava.help()` and `register_system_prompt_section`;
violations land in the agent's context window verbatim.

## What gets banned

### 1. Chinese characters

CJK Unified Ideographs + CJK punctuation + fullwidth ASCII. The prompt
must be English-only — LLMs perform better with mono-lingual instructions,
and "dev notes in Chinese, contract in English" mixing causes both to
degrade.

### 2. Framework implementation keywords

System components, history pointers, and design internals that leak the
"how" of Ava's internals to the agent. Agent should see the contract
("read returns utf-8 text") not the mechanism ("state_handle.update
mutates the working copy").

Keyword list grows as new violations are observed — add to `_IMPL_KEYWORDS`
when reviewing this lint's misses.

### 3. Module docstrings restating their own children (2026-06-10)

A module docstring naming one of its own public children inside a backtick
code span — the children render directly below it, each carrying its own
contract. Prose use of a child's name as a plain English word is never
flagged (the zero-false-positive core); function docstrings referencing
siblings are out of scope (legitimate cross-refs like cron -> launch).

### 4. SDK<->skill coupling (2026-06-10)

An agent-visible docstring referencing skills — skill discovery belongs to
the skills index section, not the SDK layer. `ava/skills.py` (whose subject
IS skills) is exempt.

### 5. Markdown emphasis (2026-07-24)

`**bold**` in a docstring — docstrings are plain Python prose, not Markdown;
emphasis markers render as literal asterisks in the stub the agent reads.
Signature-style `**kwargs` (no closing pair) is not matched.

The judgement-residue siblings of these rules (rare-error Raises sections,
cross-stage lifecycle narration, zero-based budget calls) stay manual /
sweeper territory — see conventions/lint-vs-sweeper.md.

## Exemption

For a legitimate occurrence (e.g. a function whose contract genuinely needs
to mention a process detail like `pg_notify`), append a comment
`# lint-docstring: ok <reason>` on
the same line as the offending text.

## Why not check imports / introspection-driven?

Static AST scan is faster, deterministic, and doesn't require resolving
plugin load order. False positives are fixable by either rewording the
docstring or adding the inline exemption.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# Match CJK characters + CJK punctuation + fullwidth ASCII range.
# Includes Han ideographs, CJK punctuation, and fullwidth forms.
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")

# Markdown bold emphasis (`**text**`). Both delimiters must hug non-space
# content, so `**kwargs` / `**args` (no closing pair on the same line) and a
# stray `f(**a) and g(**b)` (space before the would-be closer) don't match.
_MD_EMPHASIS_RE = re.compile(r"\*\*\S(?:[^*]*?\S)?\*\*")

# Implementation-detail keywords. Match case-insensitive whole-word where it
# makes sense; some patterns embed punctuation that disambiguates them.
# When you observe a new leaked pattern in the system prompt, add it here +
# fix the offending file.
_IMPL_KEYWORDS: list[tuple[str, str]] = [
    # System component names — agent shouldn't know these exist.
    (r"\bstate_handle\b", "state_handle (framework state plumbing)"),
    (r"\bLangGraph\b", "LangGraph (framework name)"),
    # Deployment topology / infra roles. The agent's world is just "machines";
    # gateway / agent-runner / gateway / runner / cluster are how the fleet
    # is deployed, not something the agent reasons over. Say "machine(s)".
    (r"\bcontrol[- ]plane\b", "gateway (deployment topology)"),
    (r"\bagent[- ]hosts?\b", "agent-runner (deployment role)"),
    (r"\bgateway\b", "gateway (infrastructure component)"),
    (r"\bcluster\b", "cluster (deployment topology — say 'machines')"),
    (r"\brunner\b", "runner (deployment component)"),
    (r"\bhosts?\b", "host (say 'machine')"),
    (r"\bpg_notify\b", "pg_notify (postgres detail)"),
    (r"\bworker thread\b", "worker thread (concurrency detail)"),
    (r"\bINSERT inbound\b", "INSERT inbound (db plumbing)"),
    (r"\benvelope wrap\b", "envelope wrap (message plumbing)"),
    (r"\bcheckpoint\b", "checkpoint (LangGraph internals)"),
    (r"\bava\.state(?!_)\b", "ava.state (framework-internal slot)"),
    (r"\bava\.state_update\b", "ava.state_update (framework-internal slot)"),
    # History pointers — never include in agent text.
    (r"\bPR #\d+", "PR # reference (history pointer)"),
    (r"\bdecisions/\b", "decision-record reference (history pointer)"),
    (r"legacy:", "legacy: marker (history pointer)"),
    # Design internals.
    (r"\bread-modify-write\b", "read-modify-write (implementation detail)"),
    (r"\bwrap of\b", "wrap of (implementation detail)"),
    # Platform / protocol detail.
    (r"\bPOSIX\b", "POSIX (platform detail)"),
    (r"\bCPython\b", "CPython (platform detail)"),
    (r"\bO_APPEND\b", "O_APPEND (syscall flag)"),
    # Dev slogans / setup reminders that don't belong in agent prompt.
    (r"\bfail loud, not silent\b", "dev slogan"),
    (r"\brequires AVA_", "setup reminder (let the code raise instead)"),
    # Storage / backend internals (observed leaks from the 2026-06-10 sweep).
    (r"\bagents_meta\b", "agents_meta (db table name)"),
    (r"\bmilvus\b", "milvus (vector-store backend)"),
    (r"\bwire[- ]encoded\b", "wire-encoded (transport detail)"),
    # Presentation-layer detail — the agent doesn't reason over UI surfaces.
    (r"\bpopover\b", "popover (presentation detail)"),
    (r"\bfrontend\b", "frontend (presentation detail)"),
    # Reverse references: where a value comes from is the producing function's
    # return type, not the type's business to restate.
    (r"\breturned by\b", "reverse reference ('returned by X' belongs to X's signature)"),
    # Don't advertise stdlib alternatives in SDK docstrings — that's directly
    # telling the agent to bypass the function we're documenting.
    (
        r"`Path\(p\)\.",
        "stdlib advertising (`Path(p).X(...)`) — don't recommend pathlib in SDK docs",
    ),
    (
        r"use `subprocess\.",
        "stdlib advertising (`use \\`subprocess.X\\``) — don't recommend it as escape hatch",
    ),
]

# Files whose docstrings ARE NOT agent-facing.
# - `ava/_*.py` private modules (per AGENTS.md SDK docstring discipline exception).
# - `ava/hooks.py` (plugin author audience, also per AGENTS.md exception).
# - `plugins/*/plugin.py` module-level top-of-file docstring describes the
#   plugin to devs, not agents (the agent sees registered namespaces, not
#   the plugin module itself). Functions inside plugin.py ARE still scanned
#   because they may be wrap targets (e.g. `_wrapped_read` replaces
#   `ava.files.read` and shows up in the prompt under that name).
_PLUGIN_MODULE_DOCSTRING_EXEMPT = True


def _discover_plugin_namespace_modules(repo_root: Path) -> set[Path]:
    """Find plugin module files registered as `ava.X` namespaces.

    Scans every `plugins/*/plugin.py` for `ava.register_namespace("X", <module>)`
    calls and returns the set of `<module>` file paths. Those modules are
    agent-facing (their docstrings + public function docstrings land in
    `help(ava.X)`); other helper files in the same plugin dir aren't.
    """
    namespace_files: set[Path] = set()
    for plugin_py in repo_root.glob("ava_builtins/plugins/*/plugin.py"):
        try:
            tree = ast.parse(plugin_py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        # Track `from . import <name> as <alias>` to resolve module references.
        imported_modules: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                for alias in node.names:
                    imported_modules[alias.asname or alias.name] = alias.name
        # Find `ava.register_namespace("X", <expr>)` calls.
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "register_namespace"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Name)
            ):
                module_alias = node.args[1].id
                module_name = imported_modules.get(module_alias, module_alias)
                candidate = plugin_py.parent / f"{module_name}.py"
                if candidate.exists():
                    namespace_files.add(candidate.resolve())
    return namespace_files


def _is_in_scope(path: Path, plugin_namespace_files: set[Path]) -> bool:
    """True if file's docstrings need linting."""
    rel = str(path).replace("\\", "/")
    if rel.startswith("ava/"):
        # Any underscore-prefixed path segment marks a framework-private
        # module or package (top-level `_*.py`, or a package like
        # `ava/_exports/`) — its docstrings are dev-facing, never rendered
        # into the agent's view, so the `_*.py` exemption applies by segment
        # rather than by bare filename.
        if any(part.startswith("_") for part in rel[len("ava/") :].split("/")):
            return False
        return path.name != "hooks.py"
    if rel.startswith("ava_builtins/plugins/") and path.suffix == ".py":
        # plugin.py: scanned for wrap targets (see _check_file).
        if path.name == "plugin.py":
            return True
        # Other plugin modules: only the ones bound to a namespace are
        # agent-facing. Internal helpers (e.g. `_walk.py`) skipped.
        return path.resolve() in plugin_namespace_files
    return False


# SDK docstrings must not name skills (layering: SDK below, skills above —
# the skills index section owns skill discovery). `ava/skills.py` is the one
# module whose subject IS skills, so it is exempt.
_SKILL_REF_RE = re.compile(r"\bskills?\b", re.IGNORECASE)


def _docstring_violations(
    node: ast.AST,
    source_lines: list[str],
    *,
    extra: list[tuple[re.Pattern[str], str]] | None = None,
) -> list[tuple[int, str]]:
    """Inspect a docstring node, return list of (line_number, reason)."""
    # ast.get_docstring returns the cleaned string; we want raw text and
    # exact line numbers, so pull from the Expr node directly.
    if not isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return []
    if not node.body:
        return []
    first = node.body[0]
    if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)):
        return []
    value = first.value.value
    if not isinstance(value, str):
        return []

    violations: list[tuple[int, str]] = []
    base_line = first.lineno  # 1-indexed line of opening triple-quote
    for offset, line in enumerate(value.splitlines()):
        line_no = base_line + offset
        # Check for inline exemption on this source line.
        if 0 < line_no <= len(source_lines) and "lint-docstring: ok" in source_lines[line_no - 1]:
            continue
        if _CJK_RE.search(line):
            violations.append((line_no, "Chinese characters in agent-facing docstring"))
        if _MD_EMPHASIS_RE.search(line):
            violations.append(
                (line_no, "Markdown emphasis (**bold**) — docstrings are plain Python prose")
            )
        for pattern, reason in _IMPL_KEYWORDS:
            if re.search(pattern, line, re.IGNORECASE):
                violations.append((line_no, f"impl-detail leak: {reason}"))
        for compiled, reason in extra or []:
            if compiled.search(line):
                violations.append((line_no, reason))
    return violations


# Backtick spans inside a docstring line — the only place a sibling-name
# mention counts (prose using a child's name as a plain English word, e.g.
# "semantic search", is never flagged).
_CODE_SPAN_RE = re.compile(r"`[^`]+`")


def _module_doc_child_reference_violations(
    tree: ast.Module, source_lines: list[str]
) -> list[tuple[int, str]]:
    """A module docstring must not restate its own children — they render
    directly below it, each carrying its own contract. A child's name inside
    a backtick code span is the zero-false-positive core of that rule."""
    if not tree.body:
        return []
    first = tree.body[0]
    if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)):
        return []
    value = first.value.value
    if not isinstance(value, str):
        return []

    child_names = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and not node.name.startswith("_")
    }
    if not child_names:
        return []

    violations: list[tuple[int, str]] = []
    base_line = first.lineno
    for offset, line in enumerate(value.splitlines()):
        line_no = base_line + offset
        if 0 < line_no <= len(source_lines) and "lint-docstring: ok" in source_lines[line_no - 1]:
            continue
        for span in _CODE_SPAN_RE.findall(line):
            hit = next((n for n in child_names if re.search(rf"\b{re.escape(n)}\b", span)), None)
            if hit is not None:
                violations.append(
                    (
                        line_no,
                        f"module docstring restates child `{hit}` — children render "
                        "right below with their own contracts",
                    )
                )
    return violations


def _agent_visible_names(tree: ast.Module) -> set[str] | None:
    """Mirror `ava/_exports/discovery.py:agent_visible_names` discovery, statically.

    Returns the set of names whose docstrings appear in agent-visible
    `help()` output for this module. `None` means "no `__all_for_ava__`
    declared, use non-underscore default".
    """
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "__all_for_ava__":
            value = node.value
            if isinstance(value, ast.List):
                return {
                    elt.value
                    for elt in value.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                }
    return None


def _wrap_targets(tree: ast.Module) -> set[str]:
    """Find wrapper functions that take over public SDK entries at module top level.

    Two shapes: the registration primitive `ava.extend.wrap("files.read",
    _wrapped_read)` (the current form) and a legacy bare `ava.X.Y = _wrapped`
    reassignment. Either way the wrapper's docstring becomes the docstring of
    the SDK name it replaces, so it's agent-visible even when underscore-prefixed.
    """
    targets: set[str] = set()
    for node in tree.body:
        # `ava.extend.wrap("target", <name>)` — wrapper is the 2nd positional arg
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            func = call.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "wrap"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "extend"
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "ava"
                and len(call.args) >= 2
                and isinstance(call.args[1], ast.Name)
            ):
                targets.add(call.args[1].id)
            continue
        # Legacy `ava.X.Y = <name>` reassignment
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Attribute)
                    and isinstance(target.value.value, ast.Name)
                    and target.value.value.id == "ava"
                    and isinstance(node.value, ast.Name)
                ):
                    targets.add(node.value.id)
    return targets


def _is_visible(name: str, all_names: set[str] | None) -> bool:
    if all_names is not None:
        return name in all_names
    return not name.startswith("_")


def _check_file(path: Path) -> list[tuple[Path, int, str]]:
    source = path.read_text(encoding="utf-8")
    source_lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        return [(path, e.lineno or 0, f"syntax error: {e.msg}")]

    out: list[tuple[Path, int, str]] = []

    # ava/skills.py is the one module whose subject IS skills; everywhere else
    # an SDK docstring naming a skill couples the layers (skill discovery is
    # the skills index section's job).
    extra: list[tuple[re.Pattern[str], str]] = []
    if path.name != "skills.py":
        extra.append(
            (
                _SKILL_REF_RE,
                "SDK<->skill coupling: docstrings must not reference skills "
                "(discovery belongs to the skills index)",
            )
        )

    # Module docstring — skip for plugins/*/plugin.py per
    # `_PLUGIN_MODULE_DOCSTRING_EXEMPT`.
    rel = str(path).replace("\\", "/")
    is_plugin_module = rel.endswith("/plugin.py")
    skip_module_doc = _PLUGIN_MODULE_DOCSTRING_EXEMPT and is_plugin_module
    if not skip_module_doc:
        for line_no, reason in _docstring_violations(tree, source_lines, extra=extra):
            out.append((path, line_no, reason))
        for line_no, reason in _module_doc_child_reference_violations(tree, source_lines):
            out.append((path, line_no, reason))

    all_names = _agent_visible_names(tree)

    # Walk top-level functions/classes only. Nested defs (closures, methods)
    # are not directly agent-visible from `help(ava.X)`.
    if is_plugin_module:
        # plugin.py is dev-facing. Only wrap targets (`ava.X.Y = <name>`)
        # leak their docstrings into the agent prompt.
        wrap_targets = _wrap_targets(tree)
        for node in tree.body:
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name in wrap_targets
            ):
                for line_no, reason in _docstring_violations(node, source_lines, extra=extra):
                    out.append((path, line_no, reason))
    else:
        # ava/*.py + plugins/*/_*.py: lint everything agent-visible per
        # `help()` discovery (`__all_for_ava__` whitelist, else non-underscore).
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                if not _is_visible(node.name, all_names):
                    continue
                for line_no, reason in _docstring_violations(node, source_lines, extra=extra):
                    out.append((path, line_no, reason))

    return out


def _check_agents_md(repo_root: Path) -> list[tuple[Path, int, str]]:
    """Check AGENTS.md for CJK characters — the project mandates English as primary."""
    agents_md = repo_root / "AGENTS.md"
    if not agents_md.exists():
        return []
    violations: list[tuple[Path, int, str]] = []
    for line_no, line in enumerate(agents_md.read_text(encoding="utf-8").splitlines(), 1):
        if _CJK_RE.search(line):
            violations.append(
                (agents_md, line_no, "Chinese characters in AGENTS.md (English as primary)")
            )
    return violations


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    plugin_namespace_files = _discover_plugin_namespace_modules(repo_root)
    files = sorted(
        p
        for p in repo_root.rglob("*.py")
        if ".venv" not in p.parts and _is_in_scope(p.relative_to(repo_root), plugin_namespace_files)
    )

    violations: list[tuple[Path, int, str]] = []
    for f in files:
        violations.extend(_check_file(f))
    violations.extend(_check_agents_md(repo_root))

    if not violations:
        return 0

    for path, line_no, reason in violations:
        rel = path.relative_to(repo_root)
        sys.stderr.write(f"{rel}:{line_no}: {reason}\n")

    sys.stderr.write(
        f"\n{len(violations)} violation(s) found in agent-visible docstrings.\n"
        "These docstrings end up in the LLM system prompt — keep them in English\n"
        "and free of framework implementation details. See AGENTS.md "
        '"SDK docstring discipline" for the rules. Inline exemption: append\n'
        "`# lint-docstring: ok <reason>` to the offending source line.\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
