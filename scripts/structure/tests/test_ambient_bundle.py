"""The bundle-leak rule (scripts/structure/ambient_state/bundle.py): a `@root_bundle` class never
leaves the module that defines it."""

from __future__ import annotations

import ast
import pathlib
import textwrap

from scripts.structure import ambient_state
from scripts.structure.ambient_state import bundle

_BUNDLE_SOURCE = """
    from base.wiring import root_bundle

    @root_bundle
    class GatewayWiring:
        db: object
"""


def _sites(
    root: pathlib.Path, source: str, rel: str = "gateway/routers/thing.py"
) -> dict[str, int]:
    bundle._bundles_defined_in.cache_clear()
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    measured = ambient_state.measure(tree, rel, root)
    return {key.split("::", 1)[1]: len(lines) for key, lines in measured.items()}


def _define_bundle(root: pathlib.Path) -> None:
    path = root / "gateway/wiring.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(_BUNDLE_SOURCE), encoding="utf-8")


def test_a_parameter_annotation_naming_a_bundle_is_a_site(tmp_path: pathlib.Path) -> None:
    _define_bundle(tmp_path)
    source = "from gateway.wiring import GatewayWiring\n\ndef f(w: GatewayWiring) -> None: ...\n"
    assert _sites(tmp_path, source) == {"bundle-leak:GatewayWiring": 1}


def test_a_return_attribute_and_nested_annotation_count_too(tmp_path: pathlib.Path) -> None:
    _define_bundle(tmp_path)
    source = """
        from gateway.wiring import GatewayWiring as W

        def make() -> W: ...

        class Holder:
            wiring: list[W]
    """
    assert _sites(tmp_path, source) == {"bundle-leak:W": 2}


def test_the_defining_module_may_use_its_own_bundle(tmp_path: pathlib.Path) -> None:
    _define_bundle(tmp_path)
    source = textwrap.dedent(_BUNDLE_SOURCE) + "\n\ndef build() -> GatewayWiring: ...\n"
    assert _sites(tmp_path, source, "gateway/wiring.py") == {}


def test_importing_a_bundle_to_unpack_it_is_not_an_annotation(tmp_path: pathlib.Path) -> None:
    _define_bundle(tmp_path)
    source = "from gateway.wiring import GatewayWiring\n\n\ndef f():\n    return GatewayWiring(db=None)\n"
    assert _sites(tmp_path, source) == {}


def test_a_class_without_the_marker_is_not_a_bundle(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "gateway/plain.py"
    path.parent.mkdir(parents=True)
    path.write_text("class Plain:\n    pass\n", encoding="utf-8")
    source = "from gateway.plain import Plain\n\ndef f(p: Plain) -> None: ...\n"
    assert _sites(tmp_path, source) == {}
