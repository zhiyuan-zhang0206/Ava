import tokenize
from dataclasses import dataclass, field
from pathlib import Path

from scripts.codegen.sdk_surface.contracts import MemberProof, query
from scripts.content_lint.code_comments.directives import reference_key
from scripts.content_lint.code_comments.model import Contract, Finding, Kind, Note, NoteKind, Source
from scripts.content_lint.code_comments.python_source import parse_python
from scripts.content_lint.code_comments.records import canonical_path, parse_document
from scripts.structure.placement import ModuleIndex


def test_anchor(anchor: str) -> tuple[str, str]:
    pieces = anchor.split("::")
    if len(pieces) >= 2 and not pieces[0].endswith(".py"):
        return pieces[0], "test:" + "::".join(pieces[1:])
    if len(pieces) < 2 or any(not name.isidentifier() for name in pieces[1:]):
        raise ValueError(f"not an exact pytest definition: {anchor}")
    path, name = pieces[0], ".".join(pieces[1:])
    if not pieces[-1].startswith("test_") or not Path(path).name.startswith("test_"):
        raise ValueError(f"not a pytest test definition: {anchor}")
    if any(not part.startswith("Test") for part in pieces[1:-1]):
        raise ValueError(f"not a pytest test class: {anchor}")
    return path, name


def code_anchor(anchor: str) -> tuple[str, str]:
    path, separator, name = anchor.partition(":")
    if not separator or (
        name != "<module>" and any(not part.isidentifier() for part in name.split("."))
    ):
        raise ValueError(f"not an exact qualified symbol: {anchor}")
    return path, name


@dataclass
class Repository:
    root: Path
    files: frozenset[str]
    sources: dict[str, Source] = field(default_factory=dict[str, Source])

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

    def resolve(self, anchor: str, *, test: bool = False) -> tuple[str, str]:
        path, name = test_anchor(anchor) if test else code_anchor(anchor)
        symbol = self.source(path).symbols.get(name)
        if symbol is None or symbol.ambiguous or (test and not symbol.callable):
            raise ValueError(f"missing or ambiguous symbol: {anchor}")
        return path, name


@dataclass
class Result:
    contracts: dict[str, Contract]
    findings: list[Finding]
    checked_sources: int


def load_contracts(repo: Repository) -> tuple[dict[str, Contract], list[Finding]]:
    contracts: dict[str, Contract] = {}
    findings: list[Finding] = []
    for path in sorted(repo.files):
        if not path.endswith(".md"):
            continue
        parsed, problems = parse_document(path, repo.text(path))
        findings.extend(problems)
        for contract in parsed:
            if contract.key in contracts:
                findings.append(Finding(path, contract.line, "contract-duplicate", contract.key))
            else:
                contracts[contract.key] = contract
    return contracts, findings


def check_contract(repo: Repository, contract: Contract, *, projection: bool = True) -> None:
    for anchor in contract.code:
        path, _ = repo.resolve(anchor)
        if contract.owner and not path.startswith(f"{contract.owner}/"):
            raise ValueError(f"{anchor} is outside the document owner {contract.owner}")
    for test in contract.tests:
        repo.resolve(test, test=True)
    if contract.decision:
        repo.text(contract.decision)
    if contract.kind == Kind.API:
        check_api(repo, contract, projection=projection)


# meiva:ref scripts/content_lint/code_comments/docs/code_comments.meiva.okf.md#api-help-owner
def check_api(repo: Repository, contract: Contract, *, projection: bool) -> None:
    match query(ModuleIndex(repo.root), contract.api):
        case MemberProof() as proof:
            pass
        case unknown:
            raise ValueError(f"API export cannot be proved: {contract.api}: {unknown.reason}")
    definition_name = proof.documentation_name or "<module>"
    anchor = f"{proof.source_path}:{definition_name}"
    if anchor != contract.code[0]:
        raise ValueError(f"API {contract.api} belongs to {anchor}, not {contract.code[0]}")
    source = repo.source(proof.source_path)
    docs = [
        note
        for note in source.notes
        if note.kind == NoteKind.DOCSTRING and note.owner == definition_name
    ]
    if projection and (len(docs) != 1 or docs[0].text != contract.statement):
        raise ValueError(f"API docstring differs from {contract.key}; regenerate from its owner")


def check_reference(
    path: str, owner: str, line: int, key: str, contracts: dict[str, Contract]
) -> Finding | None:
    contract = contracts.get(key)
    if contract is None:
        return Finding(path, line, "comment-target", f"unknown contract: {key}")
    if f"{path}:{owner}" not in contract.code:
        return Finding(path, line, "comment-owner", f"{key} does not bind {owner}")
    return None


def rationale_owners(source: Source, contracts: dict[str, Contract]) -> set[str]:
    return {
        note.owner
        for note in source.notes
        if note.kind == NoteKind.COMMENT
        and (key := reference_key(note.text, python=source.path.endswith(".py"))) is not None
        and check_reference(source.path, note.owner, note.line, key, contracts) is None
    }


def unreferenced_note(
    path: str, note: Note, rationales: set[str], *, strict: bool
) -> Finding | None:
    if note.kind in {NoteKind.MACHINE, NoteKind.FIXTURE_HELP}:
        return None
    if note.kind == NoteKind.LINT_DIRECTIVE:
        if strict and note.owner not in rationales:
            return Finding(
                path,
                note.line,
                "directive-owner",
                "line directive needs a valid typed rationale bound to its definition",
            )
        return None
    if strict or (note.kind == NoteKind.COMMENT and "meiva:ref" in note.text):
        return Finding(
            path,
            note.line,
            "free-comment",
            f"{note.kind}: remove redundant prose or move the fact to a typed document contract",
        )
    return None


# meiva:ref scripts/content_lint/code_comments/docs/code_comments.meiva.okf.md#python-machine-directives
def check_notes(
    source: Source,
    contracts: dict[str, Contract],
    *,
    strict: bool,
    strict_notes: frozenset[int] | None = None,
) -> tuple[list[Finding], set[str]]:
    findings: list[Finding] = []
    used: set[str] = set()
    api_by_symbol = {
        anchor: contract
        for contract in contracts.values()
        if contract.kind == Kind.API
        for anchor in contract.code
    }
    rationales = rationale_owners(source, contracts)
    for index, note in enumerate(source.notes):
        if note.kind == NoteKind.DOCSTRING:
            contract = api_by_symbol.get(f"{source.path}:{note.owner}")
            if contract is not None and note.text == contract.statement:
                used.add(contract.key)
                continue
        key = (
            reference_key(note.text, python=source.path.endswith(".py"))
            if note.kind == NoteKind.COMMENT
            else None
        )
        if key is not None:
            problem = check_reference(source.path, note.owner, note.line, key, contracts)
            if problem is not None:
                findings.append(problem)
            else:
                used.add(key)
        elif (
            problem := unreferenced_note(
                source.path,
                note,
                rationales,
                strict=strict and (strict_notes is None or index in strict_notes),
            )
        ) is not None:
            findings.append(problem)
    return findings, used


def validated_contracts(repo: Repository) -> tuple[dict[str, Contract], list[Finding]]:
    contracts, findings = load_contracts(repo)
    seen_symbols: dict[str, str] = {}
    for contract in contracts.values():
        try:
            check_contract(repo, contract)
        except (ValueError, SyntaxError, OSError, tokenize.TokenError) as error:
            findings.append(
                Finding(contract.document, contract.line, "contract-binding", str(error))
            )
        if contract.kind == Kind.API:
            for symbol in contract.code:
                previous = seen_symbols.setdefault(symbol, contract.key)
                if previous != contract.key:
                    findings.append(
                        Finding(
                            contract.document,
                            contract.line,
                            "api-owner",
                            f"{symbol} already belongs to {previous}",
                        )
                    )
    return contracts, findings


def check(
    repo: Repository,
    *,
    strict_paths: frozenset[str] | None = None,
    strict_notes: dict[str, frozenset[int]] | None = None,
) -> Result:
    contracts, findings = validated_contracts(repo)
    used: set[str] = set()
    for path in sorted(repo.files):
        if not path.endswith((".py", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")):
            continue
        strict = strict_paths is not None and path in strict_paths
        try:
            if not strict and path not in repo.sources and "meiva:ref" not in repo.text(path):
                continue
            source = repo.source(path)
            problems, references = check_notes(
                source,
                contracts,
                strict=strict,
                strict_notes=None if strict_notes is None else strict_notes.get(path),
            )
            findings.extend(problems)
            used.update(references)
        except (ValueError, SyntaxError, OSError, tokenize.TokenError) as error:
            findings.append(Finding(path, 1, "source-parse", str(error)))
    for key, contract in contracts.items():
        if key not in used:
            findings.append(
                Finding(
                    contract.document,
                    contract.line,
                    "contract-unused",
                    f"no bound source reference or generated API docstring uses {key}",
                )
            )
    return Result(contracts, findings, len(repo.sources))
