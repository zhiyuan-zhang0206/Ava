# meiva:note
# Which module files carry agent-visible SDK help: the boundary the docstring rules apply to.
# Discovery is static — a `meiva/` module declaring `__all_for_meiva__`, the module or package
# `__init__` of a top-level namespace, or a plugin module bound to a namespace through
# `SdkNamespace("X", <module>)` — so the content lint and the source comment check read one owner.
import ast
from pathlib import Path


# meiva:note
# The module assigns, or serves as a property, `__all_for_meiva__`.
def declares_agent_surface(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "__all_for_meiva__":
            return True
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if any(isinstance(t, ast.Name) and t.id == "__all_for_meiva__" for t in targets):
            return True
    return False


# meiva:note
# The names in `meiva/__init__.py`'s `__all_for_meiva__` list literal.
def top_level_surface(repo_root: Path) -> list[str]:
    tree = ast.parse((repo_root / "meiva/__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__all_for_meiva__" for t in node.targets)
            and isinstance(node.value, ast.List)
        ):
            return [
                e.value
                for e in node.value.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
    raise ValueError("meiva/__init__.py declares no __all_for_meiva__ list literal")


# meiva:note
# `meiva/` modules on the agent surface: a module declaring `__all_for_meiva__`, or the module or
# package `__init__` of a top-level namespace. Everything else under `meiva/` is framework code,
# whatever its name. An unparseable module is skipped here: it cannot be imported, so it carries no
# SDK help, and the source check reports its own parse failure.
def discover_agent_surface_modules(repo_root: Path) -> set[Path]:
    namespaces = {
        path
        for name in top_level_surface(repo_root)
        for path in (repo_root / f"meiva/{name}.py", repo_root / f"meiva/{name}/__init__.py")
        if path.is_file()
    }
    surface: set[Path] = set()
    for path in repo_root.glob("meiva/**/*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        if path in namespaces or declares_agent_surface(tree):
            surface.add(path.resolve())
    return surface


# meiva:note
# The module files a plugin declares as `meiva.X` namespaces (`SdkNamespace`).
def registered_namespace_files(plugin_py: Path, tree: ast.Module) -> set[Path]:
    files: set[Path] = set()
    imported_modules: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            for alias in node.names:
                imported_modules[alias.asname or alias.name] = alias.name
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "SdkNamespace"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Name)
        ):
            module_name = imported_modules.get(node.args[1].id, node.args[1].id)
            candidate = plugin_py.parent / f"{module_name}.py"
            if candidate.exists():
                files.add(candidate.resolve())
    return files


# meiva:note
# Plugin module files declared as `meiva.X` namespaces: those are agent-facing, other helper files
# in the same plugin directory are not.
def discover_plugin_namespace_modules(repo_root: Path) -> set[Path]:
    namespace_files: set[Path] = set()
    for plugin_py in repo_root.glob("meiva_builtins/plugins/*/plugin.py"):
        try:
            tree = ast.parse(plugin_py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        namespace_files.update(registered_namespace_files(plugin_py, tree))
    return namespace_files
