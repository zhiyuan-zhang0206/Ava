import ast
from collections import Counter


class ScopeBindings(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: Counter[str] = Counter()
        self.attribute_writes: set[str] = set()
        self.star_import = False
        self.native_fixture_mutated = False
        self.globals: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store | ast.Del):
            self.names[node.id] += 1

    def visit_Global(self, node: ast.Global) -> None:
        self.globals.update(node.names)

    def visit_Import(self, node: ast.Import) -> None:
        self.names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.star_import |= any(alias.name == "*" for alias in node.names)
        self.names.update(alias.asname or alias.name for alias in node.names)

    def function_header(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.names[node.name] += 1
        self.visit(node.args)
        for decorator in node.decorator_list:
            self.visit(decorator)
        if node.returns is not None:
            self.visit(node.returns)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.function_header(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.function_header(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names[node.name] += 1
        for expression in [
            *node.bases,
            *node.decorator_list,
            *(keyword.value for keyword in node.keywords),
        ]:
            self.visit(expression)
        nested = scope_bindings(node.body)
        self.globals.update(nested.globals)
        self.names.update({name: nested.names[name] for name in nested.globals})
        self.attribute_writes.update(nested.attribute_writes)
        imports = {
            name: kind
            for statement in node.body
            for name, kind in pytest_imports(statement).items()
        }
        self.native_fixture_mutated |= nested.native_fixture_mutated or any(
            imports.get(name) == "module" for name in nested.attribute_writes
        )

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self.names[node.name] += 1
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name is not None:
            self.names[node.name] += 1
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name is not None:
            self.names[node.name] += 1

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest is not None:
            self.names[node.rest] += 1
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            isinstance(node.ctx, ast.Store | ast.Del)
            and isinstance(node.value, ast.Name)
            and node.attr == "fixture"
        ):
            self.attribute_writes.add(node.value.id)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in {"setattr", "delattr"}
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            self.attribute_writes.add(node.args[0].id)
        self.generic_visit(node)


def scope_bindings(body: list[ast.stmt]) -> ScopeBindings:
    bindings = ScopeBindings()
    for node in body:
        bindings.visit(node)
    return bindings


def pytest_imports(node: ast.stmt) -> dict[str, str]:
    if isinstance(node, ast.Import):
        return {
            alias.asname or alias.name: "module" for alias in node.names if alias.name == "pytest"
        }
    if isinstance(node, ast.ImportFrom) and node.module == "pytest" and node.level == 0:
        return {
            alias.asname or alias.name: "fixture" for alias in node.names if alias.name == "fixture"
        }
    return {}


def imported_fixture_names(
    body: list[ast.stmt], inherited: dict[str, str], bindings: ScopeBindings
) -> dict[str, str]:
    if bindings.star_import or bindings.native_fixture_mutated:
        return {}
    imports = {name: kind for name, kind in inherited.items() if name not in bindings.names}
    for node in body:
        imports.update(
            (name, kind) for name, kind in pytest_imports(node).items() if bindings.names[name] == 1
        )
    if any(imports.get(name) == "module" for name in bindings.attribute_writes):
        return {}
    return imports


def fixture_decorator(node: ast.expr, imports: dict[str, str]) -> bool:
    if isinstance(node, ast.Call):
        if node.args:
            return False
        node = node.func
    if isinstance(node, ast.Name):
        return imports.get(node.id) == "fixture"
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "fixture"
        and isinstance(node.value, ast.Name)
        and imports.get(node.value.id) == "module"
    )


def fixture_function(node: ast.stmt, imports: dict[str, str], bindings: ScopeBindings) -> bool:
    return (
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and bindings.names[node.name] == 1
        and len(node.decorator_list) == 1
        and fixture_decorator(node.decorator_list[0], imports)
    )


def fixtures_in_scope(
    body: list[ast.stmt], inherited: dict[str, str], *, classes: bool
) -> set[str]:
    bindings = scope_bindings(body)
    allowed = imported_fixture_names(body, inherited, bindings)
    available = {name: kind for name, kind in inherited.items() if name in allowed}
    owners: set[str] = set()
    for node in body:
        available.update(
            (name, kind) for name, kind in pytest_imports(node).items() if name in allowed
        )
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and fixture_function(
            node, available, bindings
        ):
            owners.add(node.name)
        if (
            classes
            and isinstance(node, ast.ClassDef)
            and not node.decorator_list
            and bindings.names[node.name] == 1
        ):
            owners.update(
                f"{node.name}.{name}"
                for name in fixtures_in_scope(node.body, available, classes=False)
            )
    return owners


# meiva:ref scripts/content_lint/code_comments/docs/fixtures.meiva.okf.md#native-fixture-help
def fixture_owners(tree: ast.Module) -> frozenset[str]:
    return frozenset(fixtures_in_scope(tree.body, {}, classes=True))
