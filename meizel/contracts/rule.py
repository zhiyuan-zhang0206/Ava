"""The rule: project a fact edge onto its lowest common directory and judge it."""

from __future__ import annotations

from dataclasses import dataclass

from meizel.contracts.debt import edge_key
from meizel.contracts.manifest import Manifest, manifest_path
from meizel.graph.graphs import reachable
from meizel.graph.model import Code, DebtKey, Diagnostic, Fact, FactKind

__all__ = ["Projection", "cycle_edge", "is_private", "judge", "project", "relative"]


@dataclass(frozen=True, slots=True)
class Projection:
    """A fact edge seen from `lca`, the deepest directory containing both ends."""

    fact: Fact
    lca: str
    cf: str
    ct: str
    steps: tuple[tuple[str, str], ...]

    @property
    def local_edge(self) -> tuple[str, str, str]:
        return (self.lca, self.cf, self.ct)

    @property
    def exempt(self) -> bool:
        """An ancestor conftest is a lexical scope of the tests below it."""
        fact = self.fact
        if fact.kind is not FactKind.FIXTURE or not fact.dst.endswith("conftest.py"):
            return False
        owner = fact.dst.rpartition("/")[0]
        return not owner or fact.src.startswith(f"{owner}/")


def project(fact: Fact) -> Projection | None:
    """None when the edge stays inside one node (a file, or a directory it loads from)."""
    src_parts = fact.src.split("/")
    dst_parts = fact.dst.split("/")
    depth = 0
    while (
        depth < len(src_parts) - 1
        and depth < len(dst_parts) - 1
        and src_parts[depth] == dst_parts[depth]
    ):
        depth += 1
    if src_parts[depth] == dst_parts[depth]:
        return None
    steps = tuple(
        ("/".join(dst_parts[:index]), dst_parts[index])
        for index in range(depth + 1, len(dst_parts))
    )
    return Projection(fact, "/".join(src_parts[:depth]), src_parts[depth], dst_parts[depth], steps)


def is_private(name: str) -> bool:
    """A leading underscore marks a name private to its defining file; dunders are protocol."""
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _site(fact: Fact) -> tuple[str, int]:
    """Where to report a fact: its source line, or the manifest line that declares a load."""
    if fact.kind is FactKind.LOAD:
        return manifest_path(fact.src.rpartition("/")[0]), fact.line
    return fact.src, fact.line


def relative(proj: Projection) -> tuple[str, str]:
    """The fact's source and target, relative to the directory that judges it."""
    cut = len(proj.lca) + 1 if proj.lca else 0
    return proj.fact.src[cut:], proj.fact.dst[cut:]


def _where(directory: str) -> str:
    return directory or "."


def _debt(proj: Projection, name: str | None = None) -> DebtKey:
    """The debt a violation of this projection would be recorded as, owned by its `lca`."""
    fact = proj.fact
    return DebtKey(proj.lca, edge_key(proj.lca, fact.src, fact.dst, name))


def _undeclared(proj: Projection, manifests: dict[str, Manifest]) -> Diagnostic:
    fact, lca = proj.fact, proj.lca
    graph = manifests[lca].graph() if lca in manifests else {}
    hints = [
        f"L = {_where(lca)}; local edge {proj.cf} -> {proj.ct}",
        f'to declare it, add to {manifest_path(lca)}:  [uses]  "{proj.cf}" = [..., "{proj.ct}"]',
    ]
    narrowed = manifests[lca].narrowed(proj.cf, proj.ct) if lca in manifests else []
    if narrowed:
        listing = ", ".join(f"{s} -> {d}" for s, d in narrowed)
        hints.append(
            f"{proj.cf} -> {proj.ct} is declared only for narrowed endpoints ({listing}); "
            "this fact is outside them: fix the code, or ask for the narrowing to be widened"
        )
    if proj.cf in reachable(graph, proj.ct):
        hints.append(
            f"declaring it closes a cycle ({proj.ct} already reaches {proj.cf}); "
            "use a public door or inject the collaborator instead"
        )
    return Diagnostic(
        *_site(fact),
        Code.UNDECLARED_EDGE,
        f"{fact.edge()} ({fact.detail}) is not declared",
        tuple(hints),
        _debt(proj),
    )


def _blocked(proj: Projection, manifests: dict[str, Manifest]) -> Diagnostic | None:
    fact = proj.fact
    last = len(proj.steps) - 1
    for position, (directory, child) in enumerate(proj.steps):
        if child == "__init__.py" and position == last:
            continue
        if child.startswith("_"):
            hint = f"{child!r} is private to {_where(directory)} and cannot be exported; import its door `{_where(directory)}/__init__.py` or a sibling that is exported"
            return Diagnostic(
                *_site(fact),
                Code.PRIVATE_MODULE,
                f"{fact.edge()} ({fact.detail}) goes through the underscore child {child!r}",
                (hint,),
                _debt(proj),
            )
        exported = manifests[directory].exports if directory in manifests else ()
        if child not in exported:
            hint = f'add to {manifest_path(directory)}:  exports = [..., "{child}"]; or use the door `{_where(directory)}/__init__.py`'
            return Diagnostic(
                *_site(fact),
                Code.NOT_EXPORTED,
                f"{fact.edge()} ({fact.detail}) enters {_where(directory)} through {child!r}, which is not exported",
                (hint,),
                _debt(proj),
            )
    return None


def cycle_edge(proj: Projection) -> Diagnostic:
    """A fact whose local edge is a recorded back-edge: it closes a cycle in `lca`."""
    fact = proj.fact
    return Diagnostic(
        *_site(fact),
        Code.CYCLE_EDGE,
        f"{fact.edge()} ({fact.detail}) closes a cycle: {proj.cf} -> {proj.ct} is a back-edge "
        f"of {_where(proj.lca)}",
        (
            "break the cycle: use a public door, inject the collaborator, or move the code so "
            "the dependency points the other way",
        ),
        _debt(proj),
    )


def judge(proj: Projection, manifests: dict[str, Manifest]) -> list[Diagnostic]:
    """Every way one fact edge violates the three conditions of the rule."""
    fact = proj.fact
    found: list[Diagnostic] = []
    if not proj.exempt:
        manifest = manifests.get(proj.lca)
        if manifest is None or not manifest.covering(*relative(proj)):
            found.append(_undeclared(proj, manifests))
        blocked = _blocked(proj, manifests)
        if blocked is not None:
            found.append(blocked)
    manifest = manifests.get(proj.lca)
    if not proj.exempt and manifest is not None and (proj.cf, proj.ct) in manifest.back:
        found.append(cycle_edge(proj))
    found.extend(
        Diagnostic(
            *_site(fact),
            Code.PRIVATE_NAME,
            f"{fact.edge()} uses {name!r}, which is private to {fact.dst}",
            (
                "underscore names belong to the file that defines them, tests included; use a public name",
            ),
            _debt(proj, name),
        )
        for name in fact.names
        if is_private(name)
    )
    return found
