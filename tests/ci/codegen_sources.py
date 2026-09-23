"""Trace schema definitions through source imports without booting the gateway."""

import ast
from collections import defaultdict
from pathlib import Path


class SourceGraph:
    def __init__(self, root):
        self.root = Path(root)
        self.visited = set()
        self.classes = set()
        self.paths = {}
        self.trees = {}
        self.module_bindings = {}

    def path(self, module):
        if module not in self.paths:
            base = self.root.joinpath(*module.split("."))
            self.paths[module] = next(
                (p for p in (base.with_suffix(".py"), base / "__init__.py") if p.is_file()), None
            )
        return self.paths[module]

    def nodes(self, module):
        if module not in self.trees:
            path = self.path(module)
            self.trees[module] = ast.parse(path.read_text()).body if path else []
        return self.trees[module]

    def import_target(self, module, node):
        if not node.level:
            assert node.module is not None, "absolute from-import without a module"
            return node.module
        package = module if self.path(module).name == "__init__.py" else module.rpartition(".")[0]
        parts = package.split(".")
        return ".".join(
            parts[: len(parts) - node.level + 1] + ([node.module] if node.module else [])
        )

    def bindings(self, module):
        if module not in self.module_bindings:
            self.module_bindings[module] = self.read_bindings(module)
        return self.module_bindings[module]

    def read_bindings(self, module):
        result = {}
        for node in self.nodes(module):
            if isinstance(node, (ast.ImportFrom, ast.Import)):
                result.update(self.import_bindings(module, node))
            elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                result[node.name] = ("node", node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        result[target.id] = ("node", node)
            elif isinstance(
                node, (ast.If, ast.Try, ast.TryStar, ast.With, ast.For, ast.While, ast.Match)
            ):
                result.update(self.conditional_bindings(module, node))
        return result

    def conditional_bindings(self, module, node):
        """Track uncertain globals; reject only when schema traversal uses one."""
        result = {}
        pending = [node]
        while pending:
            current = pending.pop()
            names = []
            if isinstance(current, (ast.Import, ast.ImportFrom)):
                names = self.import_bindings(module, current)
            elif isinstance(current, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names = [current.name]  # Do not descend into local scopes.
            elif isinstance(current, (ast.Assign, ast.AnnAssign)):
                targets = current.targets if isinstance(current, ast.Assign) else [current.target]
                names = [target.id for target in targets if isinstance(target, ast.Name)]
            else:
                pending.extend(ast.iter_child_nodes(current))
            if names:
                result.update(dict.fromkeys(names, ("conditional", current.lineno)))
        return result

    def binding(self, module, name):
        info = self.bindings(module).get(name)
        assert not info or info[0] != "conditional", (
            f"Unsupported conditional schema binding: {module}.{name}"
        )
        return info

    def import_bindings(self, module, node):
        if isinstance(node, ast.ImportFrom):
            target = self.import_target(module, node)
            assert all(alias.name != "*" for alias in node.names), (
                f"Unsupported star import: {module}"
            )
            return {
                alias.asname or alias.name: ("import", target, alias.name) for alias in node.names
            }
        return {
            alias.asname or alias.name.split(".")[0]: (
                "module",
                alias.name if alias.asname else alias.name.split(".")[0],
            )
            for alias in node.names
        }

    def symbol(self, module, name):
        key = module, name
        if key in self.visited:
            return
        self.visited.add(key)
        info = self.binding(module, name)
        if not info:
            return
        if info[0] == "import":
            self.symbol(info[1], info[2])
            return
        if info[0] == "module":
            return
        node = info[1]
        if isinstance(node, ast.ClassDef):
            self.classes.add(key)
            for base in node.bases:
                self.expression(module, base)
            for field in node.body:
                if isinstance(field, ast.AnnAssign):
                    self.expression(module, field.annotation)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value:
            self.expression(module, node.value)

    def qualified(self, module, node):
        if isinstance(node, ast.Name):
            binding = self.binding(module, node.id)
            if binding and binding[0] == "module":
                return binding[1]
            if binding and binding[0] == "import":
                return self.imported_module(binding[1], binding[2], set())
        if isinstance(node, ast.Attribute):
            value = self.qualified(module, node.value)
            if value:
                return self.imported_module(value, node.attr, set())
        return None

    def imported_module(self, module, name, seen):
        """Resolve package module aliases before interpreting an attribute."""
        key = module, name
        assert key not in seen, f"Cyclic module re-export: {key}"
        seen.add(key)
        binding = self.binding(module, name)
        if binding and binding[0] == "module":
            return binding[1]
        if binding and binding[0] == "import" and (binding[1], binding[2]) != key:
            return self.imported_module(binding[1], binding[2], seen)
        candidate = f"{module}.{name}"
        return candidate if self.path(candidate) else None

    def expression(self, module, node):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                self.symbol(module, sub.id)
            elif isinstance(sub, ast.Attribute):
                target = self.qualified(module, sub.value)
                if target:
                    self.symbol(target, sub.attr)
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                try:
                    quoted = ast.parse(sub.value, mode="eval")
                except SyntaxError:
                    continue
                # Literal strings such as "queued" may parse as names, but
                # only actual bindings can resolve to classes.
                if not isinstance(quoted.body, ast.Constant):
                    self.expression(module, quoted)

    @staticmethod
    def route_decorators(func):
        methods = {"get", "post", "put", "delete", "patch", "head", "options", "api_route"}
        return [
            decorator
            for decorator in func.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr in methods
        ]

    def route_annotations(self, func):
        decorators = self.route_decorators(func)
        if not decorators:
            return []
        annotations = [
            keyword.value
            for decorator in decorators
            for keyword in decorator.keywords
            if keyword.arg in {"response_model", "responses"}
        ]
        if func.returns:
            annotations.append(func.returns)
        annotations.extend(
            arg.annotation
            for arg in func.args.posonlyargs + func.args.args + func.args.kwonlyargs
            if arg.annotation
        )
        return annotations

    def route_classes(self):
        for path in (self.root / "gateway/routers").rglob("*.py"):
            module = ".".join(path.relative_to(self.root).with_suffix("").parts)
            for func in self.nodes(module):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for annotation in self.route_annotations(func):
                    self.expression(module, annotation)
        return self.classes

    def schema_sources(self, components, generated):
        origins = defaultdict(set)
        for module, name in self.route_classes():
            origins[name].add(module)
        # Name indexes are used only after reachability has established
        # actual defining module identities. Never search all repository
        # classes by name. Ambiguities fail rather than guessing a source.
        unknown = set(components) - origins.keys() - set(generated)
        ambiguous = {name: sorted(origins[name]) for name in components if len(origins[name]) > 1}
        assert not unknown, f"Unresolved components: {sorted(unknown)}"
        assert not ambiguous, f"Ambiguous reachable components: {ambiguous}"
        # Include newly added models and inherited fields before regeneration.
        return {str(self.path(module).relative_to(self.root)) for module, _name in self.classes}

    def imported_sources(self, start):
        seen = set()
        pending = [start]
        while pending:
            module = pending.pop()
            if module in seen or self.path(module) is None:
                continue
            seen.add(module)
            for node in ast.walk(ast.Module(body=self.nodes(module), type_ignores=[])):
                if isinstance(node, ast.Import):
                    pending.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    target = self.import_target(module, node)
                    pending.append(target)
                    pending.extend(target + "." + alias.name for alias in node.names)
        return {str(self.path(module).relative_to(self.root)) for module in seen}
