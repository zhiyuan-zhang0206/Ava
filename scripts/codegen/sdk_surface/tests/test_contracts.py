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
            "meiva/__init__.py": """
            from . import files
            __all_for_meiva__ = ["files"]
            raise RuntimeError("Source must not be executed")
        """,
            "meiva/files.py": """
            from .implementation import read as imported
            read = imported
            __all_for_meiva__ = ["read"]
        """,
            "meiva/implementation.py": """
            def read():
                return "contents"
        """,
        },
    )
    proof = query(index, "meiva.files.read")
    assert isinstance(proof, MemberProof)
    assert proof.exposed_path == "meiva.files.read"
    assert (proof.definition_module, proof.definition_name) == ("meiva.implementation", "read")
    assert (proof.source_path, proof.source_line) == ("meiva/implementation.py", 1)
    assert (proof.declaration.path, proof.declaration.line, proof.declaration.plugin) == (
        "meiva/files.py",
        3,
        None,
    )
    assert proof.availability is Availability.STATIC


@pytest.mark.parametrize(
    "marker",
    [
        "",
        "__all_for_meiva__ = make_surface()",
        "__all_for_meiva__ = ('read',)",
        "__all_for_meiva__ = [read]",
        "__all_for_meiva__ = ['read']; __all_for_meiva__ += ['extra']",
        "__all_for_meiva__ = ['read']; __all_for_meiva__.append('extra')",
        "__all_for_meiva__ = ['read']; __all_for_meiva__[0] = 'extra'",
        "__all_for_meiva__ = ['read']; surface = __all_for_meiva__; surface.append('extra')",
        "__all_for_meiva__ = ['read']; del __all_for_meiva__",
        "__all_for_meiva__ = ['read']; __all_for_meiva__ = []",
        "__all_for_meiva__ = ['bad.name', 'read']",
        "__all_for_meiva__ = ['read']\nif condition: __all_for_meiva__ = []",
    ],
)
def test_missing_opaque_or_mutated_markers_do_not_grant_fallback(
    tmp_path: Path, marker: str
) -> None:
    index = _sources(
        tmp_path,
        {
            "meiva/__init__.py": "from . import files\n__all_for_meiva__ = ['files']",
            "meiva/files.py": f"def read(): pass\n{marker}",
        },
    )
    result = query(index, "meiva.files.read")
    assert isinstance(result, Unknown)
    assert result.path == "meiva/files.py"
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
    index = _sources(tmp_path, {"meiva/__init__.py": f"{definition}\n__all_for_meiva__ = ['read']"})
    result = query(index, "meiva.read")
    assert isinstance(result, Unknown)
    assert "definition owner" in result.reason


def test_private_marker_names_stay_hidden(tmp_path: Path) -> None:
    tree = ast.parse("__all_for_meiva__: list[str] = ['read', '_hidden']")
    assert declared_names(tree) == ("read",)
    index = _sources(
        tmp_path, {"meiva/__init__.py": "def _hidden(): pass\n__all_for_meiva__ = ['_hidden']"}
    )
    assert isinstance(query(index, "meiva._hidden"), Unknown)


def test_plugin_namespace_children_must_be_in_the_actual_marker(tmp_path: Path) -> None:
    index = _sources(
        tmp_path,
        {
            "meiva/__init__.py": "__all_for_meiva__ = []",
            "meiva_builtins/plugins/sample/plugin.py": _plugin(),
            "meiva_builtins/plugins/sample/sdk.py": """
            __all_for_meiva__ = ["read"]
            def read(): pass
            def hidden_public_spelling(): pass
        """,
        },
    )
    proof = query(index, "meiva.notes.read")
    assert isinstance(proof, MemberProof)
    assert (proof.definition_module, proof.definition_name) == (
        "meiva_builtins.plugins.sample.sdk",
        "read",
    )
    assert proof.availability is Availability.PLUGIN
    assert proof.declaration.plugin == "meiva_builtins.plugins.sample.plugin"
    assert proof.declaration.path == "meiva_builtins/plugins/sample/plugin.py"
    assert proof.declaration.line == 4
    hidden = query(index, "meiva.notes.hidden_public_spelling")
    assert isinstance(hidden, Unknown)
    assert hidden.path == "meiva_builtins/plugins/sample/sdk.py"
    assert "absent from the canonical" in hidden.reason


@pytest.mark.parametrize("marker", ["", "__all__ = []", "__all_for_meiva__ = build()"])
def test_plugin_namespace_cannot_bypass_the_root_marker(tmp_path: Path, marker: str) -> None:
    index = _sources(
        tmp_path,
        {
            "meiva/__init__.py": marker,
            "meiva_builtins/plugins/sample/plugin.py": _plugin(),
            "meiva_builtins/plugins/sample/sdk.py": "def read(): pass\n__all_for_meiva__ = ['read']",
        },
    )
    for exposed in ("meiva.notes", "meiva.notes.read"):
        result = query(index, exposed)
        assert isinstance(result, Unknown)
        assert result.path == "meiva/__init__.py"


@pytest.mark.parametrize("marker", ["", "__all_for_meiva__ = build_surface()"])
def test_plugin_namespace_without_a_literal_marker_is_unknown(tmp_path: Path, marker: str) -> None:
    index = _sources(
        tmp_path,
        {
            "meiva/__init__.py": "__all_for_meiva__ = []",
            "meiva_builtins/plugins/sample/plugin.py": _plugin(),
            "meiva_builtins/plugins/sample/sdk.py": f"def read(): pass\n{marker}",
        },
    )
    assert isinstance(query(index, "meiva.notes.read"), Unknown)
    assert isinstance(query(index, "meiva.notes"), Unknown)


@pytest.mark.parametrize("nested_marker", ["['read']", "[]"])
def test_plugin_namespace_checks_every_nested_marker(tmp_path: Path, nested_marker: str) -> None:
    index = _sources(
        tmp_path,
        {
            "meiva/__init__.py": "__all_for_meiva__ = []",
            "meiva_builtins/plugins/sample/plugin.py": _plugin(),
            "meiva_builtins/plugins/sample/sdk.py": "from . import child\n__all_for_meiva__ = ['child']",
            "meiva_builtins/plugins/sample/child.py": f"def read(): pass\n__all_for_meiva__ = {nested_marker}",
        },
    )
    result = query(index, "meiva.notes.child.read")
    if nested_marker == "[]":
        assert isinstance(result, Unknown)
        assert result.path == "meiva_builtins/plugins/sample/child.py"
    else:
        assert isinstance(result, MemberProof)
        assert result.definition_module == "meiva_builtins.plugins.sample.child"


@pytest.mark.parametrize("target", ["factory()", "sdk.missing", "unknown"])
def test_plugin_namespace_targets_are_not_guessed(tmp_path: Path, target: str) -> None:
    index = _sources(
        tmp_path,
        {
            "meiva/__init__.py": "__all_for_meiva__ = []",
            "meiva_builtins/plugins/sample/plugin.py": _plugin(target=target),
            "meiva_builtins/plugins/sample/sdk.py": "def read(): pass\n__all_for_meiva__ = ['read']",
        },
    )
    result = query(index, "meiva.notes.read")
    assert isinstance(result, Unknown)
    assert result.path == "meiva_builtins/plugins/sample/plugin.py"


def test_plugin_member_declaration_proves_an_imported_callable_owner(tmp_path: Path) -> None:
    index = _sources(
        tmp_path,
        {
            "meiva/__init__.py": "from . import ui\n__all_for_meiva__ = ['ui']",
            "meiva/ui.py": "__all_for_meiva__ = []",
            "meiva_builtins/plugins/sample/plugin.py": """
            from base.packages.plugins.extensions import PluginContributions as Contributions, SdkMember as Member
            from .implementation import _notify as notify
            def contribute():
                return Contributions(sdk_members=(Member(namespace="ui", name="notify", fn=notify),))
        """,
            "meiva_builtins/plugins/sample/implementation.py": "def _notify(): pass",
        },
    )
    proof = query(index, "meiva.ui.notify")
    assert isinstance(proof, MemberProof)
    assert (proof.definition_module, proof.definition_name) == (
        "meiva_builtins.plugins.sample.implementation",
        "_notify",
    )
    assert proof.source_line == 1
    assert proof.availability is Availability.PLUGIN
    assert proof.declaration.line == 4
    assert isinstance(query(index, "meiva.ui.notify.child"), Unknown)


@pytest.mark.parametrize(
    "target,host",
    [
        ("unknown", "__all_for_meiva__ = []"),
        ("42", "__all_for_meiva__ = []"),
        ("factory()", "__all_for_meiva__ = []"),
        ("notify", ""),
    ],
)
def test_plugin_members_need_a_callable_owner_and_canonical_host(
    tmp_path: Path, target: str, host: str
) -> None:
    index = _sources(
        tmp_path,
        {
            "meiva/__init__.py": "from . import ui\n__all_for_meiva__ = ['ui']",
            "meiva/ui.py": host,
            "meiva_builtins/plugins/sample/plugin.py": f"""
            from base.packages.plugins.extensions import PluginContributions, SdkMember
            def notify(): pass
            def contribute():
                return PluginContributions(sdk_members=(SdkMember("ui", "notify", {target}),))
        """,
        },
    )
    assert isinstance(query(index, "meiva.ui.notify"), Unknown)


def test_duplicate_plugin_declarations_are_unknown(tmp_path: Path) -> None:
    index = _sources(
        tmp_path,
        {
            "meiva/__init__.py": "__all_for_meiva__ = []",
            "meiva_builtins/plugins/one/plugin.py": _plugin(),
            "meiva_builtins/plugins/one/sdk.py": "def read(): pass\n__all_for_meiva__ = ['read']",
            "meiva_builtins/plugins/two/plugin.py": _plugin(),
            "meiva_builtins/plugins/two/sdk.py": "def read(): pass\n__all_for_meiva__ = ['read']",
        },
    )
    result = query(index, "meiva.notes.read")
    assert isinstance(result, Unknown)
    assert "Multiple plugins" in result.reason


def test_an_unrelated_constructor_cannot_claim_sdk_authority(tmp_path: Path) -> None:
    index = _sources(
        tmp_path,
        {
            "meiva/__init__.py": "__all_for_meiva__ = []",
            "meiva_builtins/plugins/sample/plugin.py": _plugin().replace(
                "base.packages.plugins.extensions", "unrelated"
            ),
            "meiva_builtins/plugins/sample/sdk.py": "def read(): pass\n__all_for_meiva__ = ['read']",
        },
    )
    assert isinstance(query(index, "meiva.notes.read"), Unknown)


def test_module_property_provenance_does_not_grant_dynamic_children(tmp_path: Path) -> None:
    index = _sources(
        tmp_path,
        {
            "meiva/__init__.py": """
        import sys
        from types import ModuleType
        __all_for_meiva__ = ["context"]
        class Surface(ModuleType):
            @property
            def context(self): return obtain_context()
        sys.modules[__name__].__class__ = Surface
    """
        },
    )
    proof = query(index, "meiva.context")
    assert isinstance(proof, MemberProof)
    assert (proof.definition_module, proof.definition_name, proof.source_line) == (
        "meiva",
        "Surface.context",
        6,
    )
    assert isinstance(query(index, "meiva.context.identity"), Unknown)
    assert proof.documentation_name == "Surface.context"


def _property_source(declaration: str, replacement: str = "") -> str:
    return "\n".join(
        [
            "import sys",
            "from types import ModuleType",
            "__all_for_meiva__ = ['context']",
            declaration,
            "class Surface(ModuleType):",
            "    @property",
            "    def context(self): return obtain_context()",
            "    @context.setter",
            "    def context(self, value): bind_context(value)",
            "    @context.deleter",
            "    def context(self): unbind_context()",
            replacement,
            "sys.modules[__name__].__class__ = Surface",
        ]
    )


def test_property_annotation_owns_help_without_replacing_getter_provenance(tmp_path: Path) -> None:
    index = _sources(
        tmp_path,
        {"meiva/__init__.py": _property_source('context: object\n"Execution context."')},
    )
    proof = query(index, "meiva.context")
    assert isinstance(proof, MemberProof)
    assert (proof.definition_module, proof.definition_name) == ("meiva", "Surface.context")
    assert (proof.source_path, proof.source_line) == ("meiva/__init__.py", 8)
    assert proof.documentation_name == "context"
    assert isinstance(query(index, "meiva.context.identity"), Unknown)


@pytest.mark.parametrize(
    "declaration,replacement",
    [
        ("context: object\ncontext: object", ""),
        ("context: object\nif condition: context = replacement", ""),
        ("context: object", "    context = replacement"),
        ("context: object", "    def context(self): return replacement"),
        ("context: object", "    property = replacement"),
    ],
)
def test_annotation_does_not_repair_an_unproved_property(
    tmp_path: Path, declaration: str, replacement: str
) -> None:
    index = _sources(tmp_path, {"meiva/__init__.py": _property_source(declaration, replacement)})
    assert isinstance(query(index, "meiva.context"), Unknown)


def test_current_context_help_is_owned_by_the_module_annotation() -> None:
    index = ModuleIndex(Path(__file__).resolve().parents[4])
    proof = query(index, "meiva.context")
    assert isinstance(proof, MemberProof)
    assert (proof.definition_name, proof.documentation_name) == ("_SdkModule.context", "context")
    assert proof.source_path == "meiva/__init__.py"


@pytest.mark.parametrize(
    "exposed,module,name,availability",
    [
        ("meiva.help", "meiva.sdk_surface.help", "help", Availability.STATIC),
        ("meiva.self.attach", "meiva.self", "attach", Availability.STATIC),
        (
            "meiva.cwd.get",
            "meiva_builtins.plugins.meiva_code._code_namespace",
            "get",
            Availability.PLUGIN,
        ),
        (
            "meiva.memory.search",
            "meiva_builtins.plugins.meiva_memory.sdk",
            "search_unwrapped",
            Availability.PLUGIN,
        ),
        (
            "meiva.tasks.create",
            "meiva_builtins.plugins.meiva_fleet.task_registry",
            "create",
            Availability.PLUGIN,
        ),
        (
            "meiva.self.set_label",
            "meiva_builtins.plugins.meiva_fleet.plugin",
            "set_label",
            Availability.PLUGIN,
        ),
    ],
)
def test_current_checkout_provenance(
    exposed: str, module: str, name: str, availability: Availability
) -> None:
    index = ModuleIndex(Path(__file__).resolve().parents[4])
    proof = query(index, exposed)
    assert isinstance(proof, MemberProof), proof
    assert (proof.definition_module, proof.definition_name) == (module, name)
    assert proof.availability is availability
    assert proof.source_line > 0


@pytest.mark.parametrize(
    "exposed",
    [
        "meiva.skills.dynamic_skill",
        "meiva.mcps.dynamic_server.tool",
        "meiva.self.AGENT_ID",
        "meiva.memory.PATH",
    ],
)
def test_current_runtime_only_surfaces_remain_unknown(exposed: str) -> None:
    assert isinstance(query(ModuleIndex(Path(__file__).resolve().parents[4]), exposed), Unknown)


@pytest.mark.parametrize("exposed", ["", "other.read", "meiva..read", "meiva.read()"])
def test_query_rejects_invalid_paths(tmp_path: Path, exposed: str) -> None:
    with pytest.raises(ValueError, match="exact meiva namespace or member path"):
        query(ModuleIndex(tmp_path), exposed)


def test_root_namespace_proof_is_the_actual_canonical_marker(tmp_path: Path) -> None:
    index = _sources(
        tmp_path,
        {
            "meiva/__init__.py": "__all_for_meiva__ = []\nraise RuntimeError('Do not execute source')\n"
        },
    )
    proof = query(index, "meiva")
    assert isinstance(proof, MemberProof)
    assert (proof.definition_module, proof.definition_name, proof.source_line) == ("meiva", "", 1)
    assert (proof.declaration.path, proof.declaration.line) == ("meiva/__init__.py", 1)
    assert proof.availability is Availability.STATIC
    assert isinstance(query(index, "meiva.missing"), Unknown)


@pytest.mark.parametrize(
    "source",
    [
        "",
        "__all__ = []",
        "__all_for_meiva__ = build()",
        "__all_for_meiva__ = []\n__all_for_meiva__.append('read')",
    ],
)
def test_root_namespace_needs_a_literal_unmodified_sdk_marker(tmp_path: Path, source: str) -> None:
    assert isinstance(query(_sources(tmp_path, {"meiva/__init__.py": source}), "meiva"), Unknown)


def test_missing_root_source_retains_unknown(tmp_path: Path) -> None:
    assert isinstance(query(ModuleIndex(tmp_path), "meiva"), Unknown)


@pytest.mark.parametrize(
    "plugin,core",
    [
        (_plugin(namespace="ui"), "from . import ui\n__all_for_meiva__ = ['ui']"),
        (
            """
        from base.packages.plugins.extensions import PluginContributions, SdkMember
        def notify(): pass
        def contribute():
            return PluginContributions(sdk_members=(SdkMember("ui", "notify", notify),))
    """,
            "from . import ui\n__all_for_meiva__ = ['ui']",
        ),
    ],
)
def test_definite_installation_conflicts_are_not_public_proofs(
    tmp_path: Path, plugin: str, core: str
) -> None:
    index = _sources(
        tmp_path,
        {
            "meiva/__init__.py": core,
            "meiva/ui.py": "def notify(): pass\n__all_for_meiva__ = ['notify']",
            "meiva_builtins/plugins/sample/plugin.py": plugin,
            "meiva_builtins/plugins/sample/sdk.py": "def notify(): pass\n__all_for_meiva__ = ['notify']",
        },
    )
    result = query(index, "meiva.ui.notify")
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
            "__all_for_meiva__ = ['context']",
            "class Surface(ModuleType):",
            "    @property",
            "    def context(self): return obtain_context()",
            f"    {change}",
            "sys.modules[__name__].__class__ = Surface",
        ]
    )
    assert isinstance(
        query(_sources(tmp_path, {"meiva/__init__.py": source}), "meiva.context"), Unknown
    )


def test_invalid_source_retains_a_location(tmp_path: Path) -> None:
    index = _sources(tmp_path, {"meiva/__init__.py": "def read(\n__all_for_meiva__ = ['read']"})
    result = query(index, "meiva.read")
    assert isinstance(result, Unknown)
    assert (result.path, result.line) == ("meiva/__init__.py", 1)
    assert "invalid Python syntax" in result.reason


def test_a_package_attribute_does_not_become_a_module_because_source_exists(tmp_path: Path) -> None:
    index = _sources(
        tmp_path,
        {
            "meiva/__init__.py": "def child(): pass\nfrom . import child as files\n__all_for_meiva__ = ['files']",
            "meiva/child.py": "def read(): pass\n__all_for_meiva__ = ['read']",
        },
    )
    proof = query(index, "meiva.files")
    assert isinstance(proof, MemberProof)
    assert (proof.definition_module, proof.definition_name) == ("meiva", "child")
    assert isinstance(query(index, "meiva.files.read"), Unknown)
