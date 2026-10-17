import ast
import tokenize
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from scripts.agent_surface import (
    declares_agent_surface,
    discover_agent_surface_modules,
    discover_plugin_namespace_modules,
)
from scripts.content_lint.code_comments.model import Finding, Note, NoteKind, Source
from scripts.content_lint.code_comments.python_source import parse_python

SOURCE_SUFFIXES = (".py", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


def canonical_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and not path.is_absolute()
        and ".." not in path.parts
        and str(path) == value
        and "\\" not in value
    )


@dataclass
class Repository:
    root: Path
    files: frozenset[str]
    sources: dict[str, Source] = field(default_factory=dict[str, Source])
    surfaces: dict[str, bool] = field(default_factory=dict[str, bool])
    surface_modules: set[Path] | None = None
    plugin_modules: set[Path] | None = None

    def text(self, path: str) -> str:
        if not canonical_path(path) or path not in self.files:
            raise ValueError(f"not an exact tracked path: {path}")
        target = self.root / path
        if (
            target.is_symlink()
            or not target.is_file()
            or target.resolve() != self.root.resolve() / path
        ):
            raise ValueError(f"not a regular in-repository file: {path}")
        return target.read_bytes().decode("utf-8")

    def source(self, path: str) -> Source:
        if path not in self.sources:
            if not path.endswith(".py"):
                raise ValueError(
                    f"no parsed source for {path}; run the TypeScript parser for JavaScript/TypeScript bindings"
                )
            self.sources[path] = parse_python(path, self.text(path))
        return self.sources[path]

    # meiva:note
    # Whether this module's docstrings are the text `meiva.help` renders. A test module never is;
    # otherwise the module must declare the surface in code (assign `__all_for_meiva__`, or be the
    # module a plugin binds to a namespace), not merely mention those names.
    def agent_surface(self, path: str) -> bool:
        if path not in self.surfaces:
            self.surfaces[path] = self._surface(path)
        return self.surfaces[path]

    def _surface(self, path: str) -> bool:
        if "/tests/" in path or path.startswith("tests/"):
            return False
        resolved = (self.root / path).resolve()
        if resolved in self._namespace_modules() or resolved in self._plugin_modules():
            return True
        try:
            return declares_agent_surface(ast.parse(self.text(path)))
        except SyntaxError:
            return False

    def _namespace_modules(self) -> set[Path]:
        if self.surface_modules is None:
            try:
                self.surface_modules = discover_agent_surface_modules(self.root)
            except (OSError, ValueError, SyntaxError):
                self.surface_modules = set()
        return self.surface_modules

    def _plugin_modules(self) -> set[Path]:
        if self.plugin_modules is None:
            self.plugin_modules = discover_plugin_namespace_modules(self.root)
        return self.plugin_modules


@dataclass
class Result:
    notes: int
    findings: list[Finding]


def check_links(repo: Repository, path: str, note: Note) -> list[Finding]:
    findings: list[Finding] = []
    for link in note.links:
        try:
            repo.text(link)
        except ValueError as error:
            findings.append(Finding(path, note.line, "note-link", str(error)))
    if note.kind == NoteKind.NOTE and not note.text.strip() and not note.links:
        findings.append(
            Finding(path, note.line, "empty-note", "a meiva:note needs text or at least one link")
        )
    return findings


def _note_findings(
    repo: Repository,
    source: Source,
    note: Note,
    *,
    index: int,
    rationales: set[str],
    strict: bool,
    strict_notes: frozenset[int] | None,
    surface: bool,
) -> list[Finding]:
    if note.kind == NoteKind.NOTE:
        return check_links(repo, source.path, note)
    if note.kind in {NoteKind.MACHINE, NoteKind.FIXTURE_HELP}:
        return []
    if note.kind == NoteKind.DOCSTRING and surface:
        return []
    selected = strict and (strict_notes is None or index in strict_notes)
    if note.kind == NoteKind.LINT_DIRECTIVE:
        if not selected or note.lexical_owner in rationales:
            return []
        return [
            Finding(
                source.path,
                note.line,
                "directive-owner",
                "a machine directive needs a meiva:note owned by the same definition",
            )
        ]
    if not selected:
        return []
    return [
        Finding(
            source.path,
            note.line,
            "free-comment",
            f"{note.kind}: write a meiva:note block or move the fact to documentation",
        )
    ]


def check_notes(
    repo: Repository,
    source: Source,
    *,
    strict: bool,
    strict_notes: frozenset[int] | None = None,
    surface: bool = False,
) -> list[Finding]:
    rationales = {
        note.lexical_owner
        for note in source.notes
        if note.kind == NoteKind.NOTE and (note.text.strip() or note.links)
    }
    findings: list[Finding] = []
    for index, note in enumerate(source.notes):
        findings.extend(
            _note_findings(
                repo,
                source,
                note,
                index=index,
                rationales=rationales,
                strict=strict,
                strict_notes=strict_notes,
                surface=surface,
            )
        )
    return findings


def check(
    repo: Repository,
    *,
    strict_paths: frozenset[str] | None = None,
    strict_notes: dict[str, frozenset[int]] | None = None,
) -> Result:
    findings: list[Finding] = []
    notes = 0
    for path in sorted(repo.files):
        if not path.endswith(SOURCE_SUFFIXES):
            continue
        strict = strict_paths is not None and path in strict_paths
        try:
            if not strict and path not in repo.sources and "meiva:" not in repo.text(path):
                continue
            source = repo.source(path)
        except (ValueError, SyntaxError, OSError, tokenize.TokenError) as error:
            findings.append(Finding(path, 1, "source-parse", str(error)))
            continue
        findings.extend(
            check_notes(
                repo,
                source,
                strict=strict,
                strict_notes=None if strict_notes is None else strict_notes.get(path),
                surface=repo.agent_surface(path),
            )
        )
        notes += sum(1 for note in source.notes if note.kind == NoteKind.NOTE)
    return Result(notes, findings)
