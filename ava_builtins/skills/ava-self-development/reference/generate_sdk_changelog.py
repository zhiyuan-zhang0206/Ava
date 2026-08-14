#!/usr/bin/env python3
"""Generate the SDK Changelog entry for a version range.

Detects agent-visible `ava.*` API changes between two git refs using two
complementary strategies:

1. **AST surface diff** — statically parses every public module under ``ava/``
   at each ref (via ``git show``, no checkout required) and diffs the exported
   symbols: added, removed, renamed (heuristic), and caller-breaking signature
   changes (required param added, param removed).
2. **Conventional-commit parsing** — scans ``git log`` for commits touching
   ``ava/`` whose subject carries the ``!`` breaking-change marker or whose body
   has a ``BREAKING CHANGE:`` trailer.

Output is a Keep a Changelog block written to ``SDK_CHANGELOG.md`` as the
newest entry; re-running for the same version label replaces that label's
previous block instead of duplicating it. The file is created on first use.

Usage::

    # Generate the entry since the latest dated tag through HEAD and update SDK_CHANGELOG.md.
    .venv/bin/python reference/generate_sdk_changelog.py

    # Specify the range explicitly.
    .venv/bin/python reference/generate_sdk_changelog.py --since v0.10.0 --until HEAD

    # Print to stdout instead of updating the file.
    .venv/bin/python reference/generate_sdk_changelog.py --stdout

    # Label the entry with a custom version string.
    .venv/bin/python reference/generate_sdk_changelog.py --version v0.11.0-20260626
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

# ── git plumbing ──────────────────────────────────────────────────────────


def _git(args: list[str], *, check: bool = True) -> str | None:
    proc = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        if check:
            sys.exit(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return None
    return proc.stdout


def _read_blob(ref: str, path: str) -> str | None:
    return _git(["show", f"{ref}:{path}"], check=False)


def _sdk_paths(ref: str) -> list[str]:
    out = _git(["ls-tree", "-r", "--name-only", ref, "--", "ava"])
    paths: list[str] = []
    for line in (out or "").splitlines():
        if not line.endswith(".py"):
            continue
        base = line.rsplit("/", 1)[-1]
        if base == "hooks.py":
            continue
        if base.startswith("_") and base != "__init__.py":
            continue
        paths.append(line)
    return paths


def _path_to_module(path: str) -> str:
    stem = path[:-3] if path.endswith(".py") else path
    if stem.endswith("/__init__"):
        stem = stem[: -len("/__init__")]
    return stem.replace("/", ".")


def _latest_dated_tag() -> str | None:
    tag_pat = re.compile(r"^v(\d+)\.(\d+)\.(\d+)-(\d{8})(?:\d{2})?$")
    latest: tuple[tuple[int, int, int], date] | None = None
    tag: str | None = None
    for line in (_git(["tag", "--list"]) or "").splitlines():
        m = tag_pat.match(line.strip())
        if not m:
            continue
        version = (int(m[1]), int(m[2]), int(m[3]))
        if latest is None or version > latest[0]:
            latest = (version, date(int(m[4][:4]), int(m[4][4:6]), int(m[4][6:8])))
            tag = line.strip()
    return tag


# ── data types ────────────────────────────────────────────────────────────

BreakingKind = Literal["removed", "renamed", "signature_changed", "behavior_changed", "deprecated"]

_DEFAULT_KIND: BreakingKind = "behavior_changed"
_KIND_KEYWORDS: list[tuple[str, BreakingKind]] = [
    ("deprecat", "deprecated"),
    ("renam", "renamed"),
    ("remov", "removed"),
    ("drop", "removed"),
    ("delete", "removed"),
]


@dataclass(frozen=True)
class APISymbol:
    module: str
    name: str
    kind: Literal["function", "class", "constant", "attribute"]
    signature: str | None = None
    summary: str | None = None
    params: tuple[str, ...] = ()
    required: frozenset[str] = field(default_factory=frozenset)

    @property
    def qualname(self) -> str:
        return f"{self.module}.{self.name}"


@dataclass(frozen=True)
class BreakingChange:
    kind: BreakingKind
    module: str
    name: str
    description: str
    commit_sha: str
    pr_number: int | None = None
    old_signature: str | None = None
    new_signature: str | None = None
    migration_guide: str | None = None


@dataclass
class ApiDiff:
    added: list[APISymbol] = field(default_factory=list)
    removed: list[APISymbol] = field(default_factory=list)
    changed: list[tuple[APISymbol, str, str, str, bool]] = field(
        default_factory=list
    )  # (sym, old_sig, new_sig, detail, breaking)
    renamed: list[tuple[APISymbol, APISymbol]] = field(default_factory=list)


# ── AST helpers ───────────────────────────────────────────────────────────


def _func_params(args: ast.arguments) -> tuple[tuple[str, ...], frozenset[str]]:
    positional = list(args.posonlyargs) + list(args.args)
    n_defaults = len(args.defaults)
    required: set[str] = set()
    for i, a in enumerate(positional):
        if i < len(positional) - n_defaults:
            required.add(a.arg)
    for a, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        if default is None:
            required.add(a.arg)
    names = tuple(a.arg for a in positional) + tuple(a.arg for a in args.kwonlyargs)
    return names, frozenset(required)


def _func_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    sig = f"({ast.unparse(node.args)})"
    if node.returns is not None:
        sig += f" -> {ast.unparse(node.returns)}"
    return sig


def _summary(node: ast.AST) -> str | None:
    doc = (
        ast.get_docstring(node)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        else None
    )
    return doc.strip().splitlines()[0] if doc else None


def _declared_agent_surface(tree: ast.Module) -> list[str] | None:
    """The module's declared agent surface (`__all_for_ava__`), or None."""
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id == "__all_for_ava__"
            and isinstance(node.value, ast.List)
        ):
            return [
                e.value
                for e in node.value.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
    return None


def _top_level_public_names(tree: ast.Module) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        if not name.startswith("_") and name not in seen:
            seen.add(name)
            names.append(name)

    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            add(node.target.id)
        elif isinstance(node, ast.ImportFrom) and node.level >= 1:
            for alias in node.names:
                add(alias.asname or alias.name)
    return names


def _import_origin(tree: ast.Module, name: str) -> tuple[str, str] | None:
    for node in tree.body:
        if not (isinstance(node, ast.ImportFrom) and node.level == 1):
            continue
        for alias in node.names:
            if (alias.asname or alias.name) == name:
                return (node.module or "", alias.name)
    return None


def _resolve_in_tree(node_tree: ast.Module, name: str) -> ast.AST | None:
    for node in node_tree.body:
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and node.name == name
        ):
            return node
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            return node
    return None


def _symbol_from_node(module: str, name: str, node: ast.AST) -> APISymbol:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        params, required = _func_params(node.args)
        return APISymbol(
            module, name, "function", _func_signature(node), _summary(node), params, required
        )
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(ast.unparse(b) for b in node.bases)
        return APISymbol(module, name, "class", f"({bases})" if bases else None, _summary(node))
    return APISymbol(module, name, "constant")


def _is_submodule(package_dir: str, name: str, tree_paths: set[str]) -> bool:
    return (
        f"{package_dir}/{name}.py" in tree_paths
        or f"{package_dir}/{name}/__init__.py" in tree_paths
    )


# ── surface extraction ────────────────────────────────────────────────────


def extract_api_surface(ref: str) -> dict[str, list[APISymbol]]:
    tree_paths = set(_sdk_paths(ref))
    all_paths = set((_git(["ls-tree", "-r", "--name-only", ref, "--", "ava"]) or "").splitlines())
    blobs: dict[str, ast.Module | None] = {}

    def parse(path: str) -> ast.Module | None:
        if path not in blobs:
            src = _read_blob(ref, path) if path in all_paths else None
            try:
                blobs[path] = ast.parse(src) if src is not None else None
            except SyntaxError:
                blobs[path] = None
        return blobs[path]

    surface: dict[str, list[APISymbol]] = {}
    for path in sorted(tree_paths):
        tree = parse(path)
        if tree is None:
            continue
        module = _path_to_module(path)
        package_dir = path.rsplit("/", 1)[0] if "/" in path else "ava"
        declared = _declared_agent_surface(tree)
        names = declared if declared is not None else _top_level_public_names(tree)

        symbols: list[APISymbol] = []
        for name in names:
            if _is_submodule(package_dir, name, tree_paths):
                continue
            local = _resolve_in_tree(tree, name)
            if local is not None:
                symbols.append(_symbol_from_node(module, name, local))
                continue
            origin = _import_origin(tree, name)
            if origin is not None and origin[0]:
                src_path = f"{package_dir}/{origin[0].lstrip('.')}.py"
                src_tree = parse(src_path)
                resolved = _resolve_in_tree(src_tree, origin[1]) if src_tree is not None else None
                if resolved is not None:
                    symbols.append(_symbol_from_node(module, name, resolved))
                    continue
            symbols.append(APISymbol(module, name, "attribute"))
        if symbols:
            surface[module] = symbols
    return surface


# ── surface diff ──────────────────────────────────────────────────────────


def diff_api_surface(old: dict[str, list[APISymbol]], new: dict[str, list[APISymbol]]) -> ApiDiff:
    diff = ApiDiff()
    for module in sorted(set(old) | set(new)):
        old_syms = {s.name: s for s in old.get(module, [])}
        new_syms = {s.name: s for s in new.get(module, [])}

        removed = [old_syms[n] for n in old_syms if n not in new_syms]
        added = [new_syms[n] for n in new_syms if n not in old_syms]

        for old_sym in list(removed):
            if old_sym.kind != "function":
                continue
            match = next(
                (a for a in added if a.kind == "function" and a.signature == old_sym.signature),
                None,
            )
            if match is not None:
                diff.renamed.append((old_sym, match))
                removed.remove(old_sym)
                added.remove(match)

        diff.removed.extend(removed)
        diff.added.extend(added)

        for name in old_syms.keys() & new_syms.keys():
            old_sym, new_sym = old_syms[name], new_syms[name]
            if (
                old_sym.kind == new_sym.kind == "function"
                and old_sym.signature != new_sym.signature
            ):
                removed_params = [p for p in old_sym.params if p not in new_sym.params]
                now_required = [p for p in new_sym.required if p not in old_sym.required]
                bits = (
                    [f"removed param(s) {', '.join(removed_params)}"] if removed_params else []
                ) + ([f"now requires {', '.join(now_required)}"] if now_required else [])
                detail = "; ".join(bits) if bits else "annotation change"
                breaking = bool(removed_params) or bool(now_required)
                diff.changed.append(
                    (new_sym, old_sym.signature or "", new_sym.signature or "", detail, breaking)
                )
    return diff


# ── conventional-commit detection ─────────────────────────────────────────


_SEP_REC = "\x1e"
_SEP_FLD = "\x1f"
_COMMIT_SUBJECT = re.compile(
    r"^(?P<type>\w+)(?:\((?P<scope>[^)]+)\))?(?P<bang>!)?:\s*(?P<summary>.*)$"
)
_PR_REF = re.compile(r"\(#(\d+)\)")
_BREAKING_TRAILER = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)


def parse_breaking_commits(since_ref: str, until_ref: str) -> list[BreakingChange]:
    out = _git(
        [
            "log",
            f"--format=%H{_SEP_FLD}%s{_SEP_FLD}%b{_SEP_REC}",
            f"{since_ref}..{until_ref}",
            "--",
            "ava",
        ]
    )
    changes: list[BreakingChange] = []
    for raw in (out or "").split(_SEP_REC):
        record = raw.strip("\n")
        if not record:
            continue
        sha, subject, body = (*record.split(_SEP_FLD, 2), "", "")[:3]
        m = _COMMIT_SUBJECT.match(subject)
        if m is None:
            continue
        if not m.group("bang") and not _BREAKING_TRAILER.search(body):
            continue
        summary = m.group("summary").strip()
        pr = _PR_REF.search(subject)
        scope = m.group("scope")
        module = f"ava.{scope}" if scope and scope not in {"sdk", "ava"} else "ava"
        kind: BreakingKind = _DEFAULT_KIND
        low = summary.lower()
        for keyword, mapped in _KIND_KEYWORDS:
            if keyword in low:
                kind = mapped
                break
        trailer = _BREAKING_TRAILER.search(body)
        guide = body[trailer.end() :].strip() or None if trailer else None
        changes.append(
            BreakingChange(
                kind=kind,
                module=module,
                name="",
                description=summary,
                commit_sha=sha,
                pr_number=int(pr.group(1)) if pr else None,
                migration_guide=guide,
            )
        )
    return changes


def _attribute_pr(name: str, since_ref: str, until_ref: str) -> tuple[str, int | None]:
    out = _git(
        [
            "log",
            "-1",
            f"--format=%H{_SEP_FLD}%s",
            f"-S{name}",
            f"{since_ref}..{until_ref}",
            "--",
            "ava",
        ],
        check=False,
    )
    if not out or not out.strip():
        return "", None
    sha, _, subject = out.strip().partition(_SEP_FLD)
    pr = _PR_REF.search(subject)
    return sha, int(pr.group(1)) if pr else None


# ── changelog rendering ───────────────────────────────────────────────────


def _fmt_pr(pr: int | None) -> str:
    return f" (#{pr})" if pr else ""


def _render_added(sym: APISymbol, pr: int | None) -> str:
    sig = sym.signature if sym.kind == "function" and sym.signature else ""
    head = f"**{sym.module}.{sym.name}{sig}**"
    return f"- {head}{f' — {sym.summary}' if sym.summary else ''}{_fmt_pr(pr)}"


_SDK_CHANGELOG = Path("SDK_CHANGELOG.md")

_SDK_HEADER = (
    "# SDK Changelog\n\n"
    "Public `ava` SDK API changes, newest first. Generated by\n"
    "`python skills/ava-self-development/reference/generate_sdk_changelog.py`; "
    "format follows\n"
    "[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This tracks the\n"
    "agent-visible `ava.*` surface only; project-wide release notes live in\n"
    "CHANGELOG.md.\n"
)


def _append_section(lines: list[str], title: str, body: list[str]) -> None:
    if not body:
        return
    lines.append(f"### {title}")
    lines.extend(body)
    lines.append("")


def generate_entry(
    since_ref: str,
    until_ref: str,
    *,
    version: str | None = None,
    when: date | None = None,
) -> str:
    when = when or datetime.now(UTC).date()
    label = version or f"{since_ref}..{until_ref}"

    old = extract_api_surface(since_ref)
    new = extract_api_surface(until_ref)
    diff = diff_api_surface(old, new)
    declared = parse_breaking_commits(since_ref, until_ref)

    deprecated = [c for c in declared if c.kind == "deprecated"]
    declared_breaking = [c for c in declared if c.kind != "deprecated"]

    # Structural breaks
    structural: list[BreakingChange] = []
    for sym in diff.removed:
        sha, pr = _attribute_pr(sym.name, since_ref, until_ref)
        structural.append(
            BreakingChange(
                "removed",
                sym.module,
                sym.name,
                sym.summary or "removed",
                sha,
                pr,
                old_signature=sym.signature,
            )
        )
    for old_sym, new_sym in diff.renamed:
        sha, pr = _attribute_pr(new_sym.name, since_ref, until_ref)
        structural.append(
            BreakingChange(
                "renamed",
                new_sym.module,
                new_sym.name,
                f"renamed from {old_sym.name}",
                sha,
                pr,
                old_signature=old_sym.signature,
                new_signature=new_sym.signature,
            )
        )
    for new_sym, old_sig, new_sig, detail, breaking in diff.changed:
        if not breaking:
            continue
        sha, pr = _attribute_pr(new_sym.name, since_ref, until_ref)
        structural.append(
            BreakingChange(
                "signature_changed",
                new_sym.module,
                new_sym.name,
                detail,
                sha,
                pr,
                old_signature=old_sig,
                new_signature=new_sig,
            )
        )

    lines = [f"## [{label}] — {when.isoformat()}", ""]

    breaking_lines: list[str] = []
    for c in structural + declared_breaking:
        target = f"{c.module}.{c.name}" if c.name else c.module
        if c.kind == "signature_changed":
            text = f"signature changed — {c.description}"
        elif c.kind == "renamed":
            text = c.description
        elif c.kind == "removed":
            text = "removed"
        else:
            text = c.description
        breaking_lines.append(f"- **{target}**: {text}{_fmt_pr(c.pr_number)}")
    _append_section(lines, "Breaking Changes", breaking_lines)

    added_lines = [
        _render_added(s, _attribute_pr(s.name, since_ref, until_ref)[1])
        for s in sorted(diff.added, key=lambda s: s.qualname)
    ]
    _append_section(lines, "Added", added_lines)

    changed_lines = [
        f"- **{sym.qualname}**: {detail}"
        for sym, _old_sig, _new_sig, detail, _breaking in sorted(
            (c for c in diff.changed if not c[4]),
            key=lambda c: c[0].qualname,
        )
    ]
    _append_section(lines, "Changed", changed_lines)

    removed_lines = [
        f"- **{s.qualname}** — removed{_fmt_pr(_attribute_pr(s.name, since_ref, until_ref)[1])}"
        for s in sorted(diff.removed, key=lambda s: s.qualname)
    ]
    _append_section(lines, "Removed", removed_lines)

    deprecated_lines = [
        f"- **{c.module}**: {c.description}{_fmt_pr(c.pr_number)}" for c in deprecated
    ]
    _append_section(lines, "Deprecated", deprecated_lines)

    if len(lines) == 2:
        lines.append("_No SDK-visible changes._")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _entry_label(block: str) -> str | None:
    m = re.match(r"## \[([^\]]+)\]", block.strip())
    return m.group(1) if m else None


def _splice_entry(existing: str, entry: str) -> str:
    idx = existing.find("## [")
    head, tail = (existing[:idx], existing[idx:]) if idx != -1 else (existing, "")
    label = _entry_label(entry)
    if label is not None and tail.strip():
        blocks = re.split(r"(?m)^(?=## \[)", tail)
        tail = "".join(b for b in blocks if _entry_label(b) != label)
    parts = [head.rstrip(), entry.rstrip()]
    if tail.strip():
        parts.append(tail.rstrip())
    return "\n\n".join(parts) + "\n"


def update_sdk_changelog(since_ref: str, until_ref: str, version: str | None = None) -> str:
    entry = generate_entry(since_ref, until_ref, version=version)
    existing = (
        _SDK_CHANGELOG.read_text(encoding="utf-8") if _SDK_CHANGELOG.exists() else _SDK_HEADER
    )
    _SDK_CHANGELOG.write_text(_splice_entry(existing, entry), encoding="utf-8")
    return entry


# ── CLI ───────────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--since", help="start ref (default: latest dated tag, or first commit)")
    ap.add_argument("--until", default="HEAD", help="end ref (default: HEAD)")
    ap.add_argument("--version", help="version label for the entry heading (default: auto)")
    ap.add_argument(
        "--stdout", action="store_true", help="print to stdout instead of updating SDK_CHANGELOG.md"
    )
    args = ap.parse_args()

    root_commit = _git(["rev-list", "--max-parents=0", "HEAD"])
    since = args.since or _latest_dated_tag() or (root_commit.split()[0] if root_commit else "HEAD")
    until = args.until

    if args.stdout:
        print(generate_entry(since, until, version=args.version))
    else:
        entry = update_sdk_changelog(since, until, version=args.version)
        print(f"updated {_SDK_CHANGELOG.resolve()}")
        print()
        print(entry)


if __name__ == "__main__":
    main()
