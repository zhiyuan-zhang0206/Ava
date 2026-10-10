"""Canonical SDK markers prove declarations without executing application source."""

from __future__ import annotations

import ast
from pathlib import Path
from textwrap import dedent

import pytest

from scripts.codegen.sdk_surface.contracts import (
    Availability,
    MemberProof,
    Unknown,
    declared_names,
    query,
)
from scripts.structure.placement import ModuleIndex


def _sources(root: Path, sources: dict[str, str]) -> ModuleIndex:
    for relative, text in sources.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dedent(text).lstrip(), encoding="utf-8")
    return ModuleIndex(root)


def _plugin(namespace: str = "notes", target: str = "sdk") -> str:
    return f"""
        from base.packages.plugins.extensions import PluginContributions, SdkNamespace
        from . import sdk
        def contribute():
            return PluginContributions(sdk_namespaces=(SdkNamespace("{namespace}", {target}),))
    """


def test_aliases_preserve_the_actual_definition_and_marker_locations(tmp_path: Path) -> None:
    index = _sources(
        tmp_path,
        {
            "ava/__init__.py": """
            from . import files
            __all_for_ava__ = ["files"]
            raise RuntimeError("Source must not be executed")
        """,
            "ava/files.py": """
            from .implementation import read as imported
            read = imported
            __all_for_ava__ = ["read"]
        """,
            "ava/implementation.py": """
            def read():
                return "contents"
        """,
        },
    )
    proof = query(index, "ava.files.read")
    assert isinstance(proof, MemberProof)
    assert proof.exposed_path == "ava.files.read"
    assert (proof.definition_module, proof.definition_name) == ("ava.implementation", "read")
    assert (proof.source_path, proof.source_line) == ("ava/implementation.py", 1)
    assert (proof.declaration.path, proof.declaration.line, proof.declaration.plugin) == (
        "ava/files.py",
        3,
        None,
    )
    assert proof.availability is Availability.STATIC


@pytest.mark.parametrize(
    "marker",
    [
        "",
        "__all_for_ava__ = make_surface()",
        "__all_for_ava__ = ('read',)",
        "__all_for_ava__ = [read]",
        "__all_for_ava__ = ['read']; __all_for_ava__ += ['extra']",
        "__all_for_ava__ = ['read']; __all_for_ava__.append('extra')",
        "__all_for_ava__ = ['read']; __all_for_ava__[0] = 'extra'",
        "__all_for_ava__ = ['read']; surface = __all_for_ava__; surface.append('extra')",
        "__all_for_ava__ = ['read']; del __all_for_ava__",
        "__all_for_ava__ = ['read']; __all_for_ava__ = []",
        "__all_for_ava__ = ['bad.name', 'read']",
        "__all_for_ava__ = ['read']\nif condition: __all_for_ava__ = []",
    ],
)
def test_missing_opaque_or_mutated_markers_do_not_grant_fallback(
    tmp_path: Path, marker: str
) -> None:
    index = _sources(
        tmp_path,
        {
            "ava/__init__.py": "from . import files\n__all_for_ava__ = ['files']",
            "ava/files.py": f"def read(): pass\n{marker}",
        },
    )
    result = query(index, "ava.files.read")
    assert isinstance(result, Unknown)
    assert result.path == "ava/files.py"
    assert result.availability is Availability.UNKNOWN


@pytest.mark.parametrize(
    "definition",
    [
        "from missing.module import read",
        "read = second\nsecond = read",
        "def read(): pass\nread = factory()",
        "if condition:\n    def read(): pass",
        "def read(): pass\ndel read",
    ],
)
def test_declared_names_require_an_unambiguous_definition(tmp_path: Path, definition: str) -> None:
    index = _sources(tmp_path, {"ava/__init__.py": f"{definition}\n__all_for_ava__ = ['read']"})
    result = query(index, "ava.read")
    assert isinstance(result, Unknown)
    assert "definition owner" in result.reason


def test_private_marker_names_stay_hidden(tmp_path: Path) -> None:
    tree = ast.parse("__all_for_ava__: list[str] = ['read', '_hidden']")
    assert declared_names(tree) == ("read",)
    index = _sources(
        tmp_path, {"ava/__init__.py": "def _hidden(): pass\n__all_for_ava__ = ['_hidden']"}
    )
    assert isinstance(query(index, "ava._hidden"), Unknown)


def test_plugin_namespace_children_must_be_in_the_actual_marker(tmp_path: Path) -> None:
    index = _sources(
        tmp_path,
        {
            "ava/__init__.py": "__all_for_ava__ = []",
            "ava_builtins/plugins/sample/plugin.py": _plugin(),
            "ava_builtins/plugins/sample/sdk.py": """
            __all_for_ava__ = ["read"]
            def read(): pass
            def hidden_public_spelling(): pass
        """,
        },
    )
    proof = query(index, "ava.notes.read")
    assert isinstance(proof, MemberProof)
    assert (proof.definition_module, proof.definition_name) == (
        "ava_builtins.plugins.sample.sdk",
        "read",
    )
    assert proof.availability is Availability.PLUGIN
    assert proof.declaration.plugin == "ava_builtins.plugins.sample.plugin"
    assert proof.declaration.path == "ava_builtins/plugins/sample/plugin.py"
    assert proof.declaration.line == 4
    hidden = query(index, "ava.notes.hidden_public_spelling")
    assert isinstance(hidden, Unknown)
    assert hidden.path == "ava_builtins/plugins/sample/sdk.py"
    assert "absent from the canonical" in hidden.reason


@pytest.mark.parametrize("marker", ["", "__all_for_ava__ = build_surface()"])
def test_plugin_namespace_without_a_literal_marker_is_unknown(tmp_path: Path, marker: str) -> None:
    index = _sources(
        tmp_path,
        {
            "ava/__init__.py": "__all_for_ava__ = []",
            "ava_builtins/plugins/sample/plugin.py": _plugin(),
            "ava_builtins/plugins/sample/sdk.py": f"def read(): pass\n{marker}",
        },
    )
    assert isinstance(query(index, "ava.notes.read"), Unknown)
    assert isinstance(query(index, "ava.notes"), Unknown)


@pytest.mark.parametrize("nested_marker", ["['read']", "[]"])
def test_plugin_namespace_checks_every_nested_marker(tmp_path: Path, nested_marker: str) -> None:
    index = _sources(
        tmp_path,
        {
            "ava/__init__.py": "__all_for_ava__ = []",
            "ava_builtins/plugins/sample/plugin.py": _plugin(),
            "ava_builtins/plugins/sample/sdk.py": "from . import child\n__all_for_ava__ = ['child']",
            "ava_builtins/plugins/sample/child.py": f"def read(): pass\n__all_for_ava__ = {nested_marker}",
        },
    )
    result = query(index, "ava.notes.child.read")
    if nested_marker == "[]":
        assert isinstance(result, Unknown)
        assert result.path == "ava_builtins/plugins/sample/child.py"
    else:
        assert isinstance(result, MemberProof)
        assert result.definition_module == "ava_builtins.plugins.sample.child"


@pytest.mark.parametrize("target", ["factory()", "sdk.missing", "unknown"])
def test_plugin_namespace_targets_are_not_guessed(tmp_path: Path, target: str) -> None:
    index = _sources(
        tmp_path,
        {
            "ava/__init__.py": "__all_for_ava__ = []",
            "ava_builtins/plugins/sample/plugin.py": _plugin(target=target),
            "ava_builtins/plugins/sample/sdk.py": "def read(): pass\n__all_for_ava__ = ['read']",
        },
    )
    result = query(index, "ava.notes.read")
    assert isinstance(result, Unknown)
    assert result.path == "ava_builtins/plugins/sample/plugin.py"


def test_plugin_member_declaration_proves_an_imported_callable_owner(tmp_path: Path) -> None:
    index = _sources(
        tmp_path,
        {
            "ava/__init__.py": "from . import ui\n__all_for_ava__ = ['ui']",
            "ava/ui.py": "__all_for_ava__ = []",
            "ava_builtins/plugins/sample/plugin.py": """
            from base.packages.plugins.extensions import PluginContributions as Contributions, SdkMember as Member
            from .implementation import _notify as notify
            def contribute():
                return Contributions(sdk_members=(Member(namespace="ui", name="notify", fn=notify),))
        """,
            "ava_builtins/plugins/sample/implementation.py": "def _notify(): pass",
        },
    )
    proof = query(index, "ava.ui.notify")
    assert isinstance(proof, MemberProof)
    assert (proof.definition_module, proof.definition_name) == (
        "ava_builtins.plugins.sample.implementation",
        "_notify",
    )
    assert proof.source_line == 1
    assert proof.availability is Availability.PLUGIN
    assert proof.declaration.line == 4
    assert isinstance(query(index, "ava.ui.notify.child"), Unknown)


@pytest.mark.parametrize(
    "target,host",
    [
        ("unknown", "__all_for_ava__ = []"),
        ("42", "__all_for_ava__ = []"),
        ("factory()", "__all_for_ava__ = []"),
        ("notify", ""),
    ],
)
def test_plugin_members_need_a_callable_owner_and_canonical_host(
    tmp_path: Path, target: str, host: str
) -> None:
    index = _sources(
        tmp_path,
        {
            "ava/__init__.py": "from . import ui\n__all_for_ava__ = ['ui']",
            "ava/ui.py": host,
            "ava_builtins/plugins/sample/plugin.py": f"""
            from base.packages.plugins.extensions import PluginContributions, SdkMember
            def notify(): pass
            def contribute():
                return PluginContributions(sdk_members=(SdkMember("ui", "notify", {target}),))
        """,
        },
    )
    assert isinstance(query(index, "ava.ui.notify"), Unknown)


def test_duplicate_plugin_declarations_are_unknown(tmp_path: Path) -> None:
    index = _sources(
        tmp_path,
        {
            "ava/__init__.py": "__all_for_ava__ = []",
            "ava_builtins/plugins/one/plugin.py": _plugin(),
            "ava_builtins/plugins/one/sdk.py": "def read(): pass\n__all_for_ava__ = ['read']",
            "ava_builtins/plugins/two/plugin.py": _plugin(),
            "ava_builtins/plugins/two/sdk.py": "def read(): pass\n__all_for_ava__ = ['read']",
        },
    )
    result = query(index, "ava.notes.read")
    assert isinstance(result, Unknown)
    assert "Multiple plugins" in result.reason


def test_an_unrelated_constructor_cannot_claim_sdk_authority(tmp_path: Path) -> None:
    index = _sources(
        tmp_path,
        {
            "ava/__init__.py": "__all_for_ava__ = []",
            "ava_builtins/plugins/sample/plugin.py": _plugin().replace(
                "base.packages.plugins.extensions", "unrelated"
            ),
            "ava_builtins/plugins/sample/sdk.py": "def read(): pass\n__all_for_ava__ = ['read']",
        },
    )
    assert isinstance(query(index, "ava.notes.read"), Unknown)


def test_module_property_provenance_does_not_grant_dynamic_children(tmp_path: Path) -> None:
    index = _sources(
        tmp_path,
        {
            "ava/__init__.py": """
        import sys
        from types import ModuleType
        __all_for_ava__ = ["context"]
        class Surface(ModuleType):
            @property
            def context(self): return obtain_context()
        sys.modules[__name__].__class__ = Surface
    """
        },
    )
    proof = query(index, "ava.context")
    assert isinstance(proof, MemberProof)
    assert (proof.definition_module, proof.definition_name, proof.source_line) == (
        "ava",
        "Surface.context",
        6,
    )
    assert isinstance(query(index, "ava.context.identity"), Unknown)


@pytest.mark.parametrize(
    "exposed,module,name,availability",
    [
        ("ava.help", "ava.sdk_surface.help", "help", Availability.STATIC),
        ("ava.self.attach", "ava.sdk_surface.attachment_transport", "attach", Availability.STATIC),
        (
            "ava.cwd.get",
            "ava_builtins.plugins.ava_code._code_namespace",
            "get",
            Availability.PLUGIN,
        ),
        (
            "ava.memory.search",
            "ava_builtins.plugins.ava_memory.sdk",
            "_search",
            Availability.PLUGIN,
        ),
        (
            "ava.tasks.create",
            "ava_builtins.plugins.ava_fleet.task_registry",
            "create",
            Availability.PLUGIN,
        ),
        (
            "ava.self.set_label",
            "ava_builtins.plugins.ava_fleet.plugin",
            "set_label",
            Availability.PLUGIN,
        ),
    ],
)
def test_current_checkout_provenance(
    exposed: str, module: str, name: str, availability: Availability
) -> None:
    index = ModuleIndex(Path(__file__).resolve().parents[3])
    proof = query(index, exposed)
    assert isinstance(proof, MemberProof), proof
    assert (proof.definition_module, proof.definition_name) == (module, name)
    assert proof.availability is availability
    assert proof.source_line > 0


@pytest.mark.parametrize(
    "exposed",
    [
        "ava.skills.dynamic_skill",
        "ava.mcps.dynamic_server.tool",
        "ava.self.AGENT_ID",
        "ava.memory.PATH",
    ],
)
def test_current_runtime_only_surfaces_remain_unknown(exposed: str) -> None:
    assert isinstance(query(ModuleIndex(Path(__file__).resolve().parents[3]), exposed), Unknown)


@pytest.mark.parametrize("exposed", ["ava", "other.read", "ava..read", "ava.read()"])
def test_query_rejects_invalid_paths(tmp_path: Path, exposed: str) -> None:
    with pytest.raises(ValueError, match="exact ava member path"):
        query(ModuleIndex(tmp_path), exposed)


@pytest.mark.parametrize(
    "plugin,core",
    [
        (_plugin(namespace="ui"), "from . import ui\n__all_for_ava__ = ['ui']"),
        (
            """
        from base.packages.plugins.extensions import PluginContributions, SdkMember
        def notify(): pass
        def contribute():
            return PluginContributions(sdk_members=(SdkMember("ui", "notify", notify),))
    """,
            "from . import ui\n__all_for_ava__ = ['ui']",
        ),
    ],
)
def test_definite_installation_conflicts_are_not_public_proofs(
    tmp_path: Path, plugin: str, core: str
) -> None:
    index = _sources(
        tmp_path,
        {
            "ava/__init__.py": core,
            "ava/ui.py": "def notify(): pass\n__all_for_ava__ = ['notify']",
            "ava_builtins/plugins/sample/plugin.py": plugin,
            "ava_builtins/plugins/sample/sdk.py": "def notify(): pass\n__all_for_ava__ = ['notify']",
        },
    )
    result = query(index, "ava.ui.notify")
    assert isinstance(result, Unknown)
    assert "conflicts" in result.reason


@pytest.mark.parametrize(
    "change",
    [
        "context = 42",
        "def context(self): return 42",
        "property = factory()",
    ],
)
def test_replaced_property_getters_do_not_prove_an_owner(tmp_path: Path, change: str) -> None:
    source = "\n".join(
        [
            "import sys",
            "from types import ModuleType",
            "__all_for_ava__ = ['context']",
            "class Surface(ModuleType):",
            "    @property",
            "    def context(self): return obtain_context()",
            f"    {change}",
            "sys.modules[__name__].__class__ = Surface",
        ]
    )
    assert isinstance(
        query(_sources(tmp_path, {"ava/__init__.py": source}), "ava.context"), Unknown
    )


def test_invalid_source_retains_a_location(tmp_path: Path) -> None:
    index = _sources(tmp_path, {"ava/__init__.py": "def read(\n__all_for_ava__ = ['read']"})
    result = query(index, "ava.read")
    assert isinstance(result, Unknown)
    assert (result.path, result.line) == ("ava/__init__.py", 1)
    assert "invalid Python syntax" in result.reason


def test_a_package_attribute_does_not_become_a_module_because_source_exists(tmp_path: Path) -> None:
    index = _sources(
        tmp_path,
        {
            "ava/__init__.py": "def child(): pass\nfrom . import child as files\n__all_for_ava__ = ['files']",
            "ava/child.py": "def read(): pass\n__all_for_ava__ = ['read']",
        },
    )
    proof = query(index, "ava.files")
    assert isinstance(proof, MemberProof)
    assert (proof.definition_module, proof.definition_name) == ("ava", "child")
    assert isinstance(query(index, "ava.files.read"), Unknown)
